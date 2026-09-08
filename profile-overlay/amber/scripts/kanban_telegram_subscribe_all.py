#!/usr/bin/env python3
"""Reconcile the existing native Telegram destination to Amber.

The script is a gated candidate for the existing one-minute job. It never
changes task lifecycle state, discovers a fallback board, or removes a human
subscription. Existing rows are transferred with the native CAS primitive and
all rows in one batch share one outer transaction.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

# Do not let the caller's cwd or HERMES_KANBAN_* scope choose the runtime.
# The installed overlay runs against the explicitly pinned runtime tree.
_RUNTIME_ROOT = Path(os.environ.get("HERMES_AGENT_RUNTIME", "/mnt/usb-ext4/hermes-agent-runtime"))
if _RUNTIME_ROOT.is_dir() and str(_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_ROOT))
from hermes_cli import kanban_db as kb  # noqa: E402


BOARD = "iron-rod"
SOURCE_PROFILE = "forge"
PROFILE = "amber"
DB_PATH = Path("/home/rodrigue/.hermes/kanban/boards/iron-rod/kanban.db")
# Keep the historical lock name so a stale/manual invocation cannot overlap
# the existing job while this candidate is being exercised.
LOCK_PATH = Path("/home/rodrigue/.hermes/kanban/.forge-telegram-subscriptions.lock")
ACTIVE_STATUSES = {
    "triage",
    "todo",
    "ready",
    "scheduled",
    "running",
    "review",
    "blocked",
}
MAX_BATCH_SIZE = 50
_ALLOWED_OWNERS = {PROFILE, SOURCE_PROFILE}


def _value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _thread(row: dict[str, Any]) -> str:
    return str(row.get("thread_id") or "")


def _destination(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("chat_id")), _thread(row)


def _is_target(row: dict[str, Any], *, chat_id: str, thread_id: str) -> bool:
    return (
        row.get("platform") == "telegram"
        and str(row.get("chat_id")) == chat_id
        and _thread(row) == thread_id
    )


def _rows_for_task(
    subscriptions: Iterable[dict[str, Any]], task_id: str, *, chat_id: str, thread_id: str
) -> list[dict[str, Any]]:
    return [
        row
        for row in subscriptions
        if str(row.get("task_id")) == task_id
        and _is_target(row, chat_id=chat_id, thread_id=thread_id)
    ]


def plan(
    tasks: Iterable[Any], subscriptions: list[dict[str, Any]], limit: int = MAX_BATCH_SIZE
) -> tuple[set[str], dict[str, Any], dict[str, list[dict[str, Any]]], list[tuple[str, str]]]:
    """Build a fail-closed, deterministic migration plan without writing."""
    active = {
        str(task_id)
        for task in tasks
        if (task_id := _value(task, "id"))
        and _value(task, "status") in ACTIVE_STATUSES
    }
    candidates = [
        row
        for row in subscriptions
        if row.get("platform") == "telegram"
        and row.get("notifier_profile") in _ALLOWED_OWNERS
        and row.get("chat_type") == "dm"
        and row.get("chat_id")
    ]
    signatures = {
        (
            str(row["chat_id"]),
            _thread(row),
            row.get("user_id"),
            row.get("user_id_alt"),
            row.get("chat_type") or "dm",
        )
        for row in candidates
    }
    if len(signatures) != 1:
        raise RuntimeError("Missing or ambiguous existing Amber/Forge Telegram DM target")
    chat_id, thread_id, _, _, _ = next(iter(signatures))
    anchor = next(
        row for row in candidates if _destination(row) == (chat_id, thread_id)
    )

    targets: dict[str, list[dict[str, Any]]] = {}
    for row in subscriptions:
        task_id = row.get("task_id")
        if task_id and _is_target(row, chat_id=chat_id, thread_id=thread_id):
            targets.setdefault(str(task_id), []).append(row)

    actions: list[tuple[str, str]] = []
    for task_id in sorted(active):
        rows = targets.get(task_id, [])
        if len(rows) > 1:
            raise RuntimeError(f"Conflicting duplicate subscriptions for {task_id}")
        row = rows[0] if rows else None
        if row is None:
            actions.append((task_id, "create"))
        elif row.get("notifier_profile") == SOURCE_PROFILE:
            actions.append((task_id, "transfer"))
        elif row.get("notifier_profile") == PROFILE and row.get("delivery_mode") == "notify":
            actions.append((task_id, "repair"))
        elif row.get("notifier_profile") == PROFILE and row.get("delivery_mode") == "notify+wake":
            continue
        else:
            # Human, unknown, unowned, or malformed destinations are never
            # evicted to make the Amber count look healthy.
            raise RuntimeError(f"Unexpected subscription owner or mode for {task_id}")
    return active, anchor, targets, actions[:limit]


def _fetch_target(conn: sqlite3.Connection, task_id: str, anchor: dict[str, Any]) -> list[dict[str, Any]]:
    return _rows_for_task(
        kb.list_notify_subs(conn, task_id),
        task_id,
        chat_id=str(anchor["chat_id"]),
        thread_id=_thread(anchor),
    )


def _ensure_amber_notify_wake(
    conn: sqlite3.Connection, task_id: str, anchor: dict[str, Any], action: str
) -> None:
    """Apply one action inside the caller-owned transaction, preserving row data."""
    rows = _fetch_target(conn, task_id, anchor)
    if len(rows) > 1:
        raise RuntimeError(f"Conflicting duplicate subscriptions for {task_id}")
    row = rows[0] if rows else None
    key = {
        "task_id": task_id,
        "platform": "telegram",
        "chat_id": str(anchor["chat_id"]),
        "thread_id": _thread(anchor),
    }
    if row is None:
        if action not in {"create", "transfer", "repair"}:
            raise RuntimeError(f"Subscription disappeared for {task_id}")
        # New rows start caught up through add_notify_sub's native MAX(event)
        # snapshot. This avoids replaying history while covering new cards.
        metadata = anchor.get("delivery_metadata")
        if not isinstance(metadata, dict):
            metadata = kb._decode_notify_delivery_metadata(metadata)
        kb.add_notify_sub(
            conn,
            **key,
            user_id=anchor.get("user_id"),
            user_id_alt=anchor.get("user_id_alt"),
            chat_type="dm",
            notifier_profile=PROFILE,
            delivery_mode="notify+wake",
            delivery_metadata=metadata,
        )
    else:
        owner = row.get("notifier_profile")
        if owner == SOURCE_PROFILE:
            if not kb.transfer_notify_sub_owner(
                conn, **key, expected_owner=SOURCE_PROFILE, new_owner=PROFILE
            ):
                raise RuntimeError(f"Subscription owner changed for {task_id}")
        elif owner != PROFILE:
            raise RuntimeError(f"Unexpected subscription owner for {task_id}")
        # Explicit mode update is native and leaves the cursor, origin,
        # identifiers, metadata and created_at untouched on an existing row.
        kb.add_notify_sub(conn, **key, delivery_mode="notify+wake")

    verified = _fetch_target(conn, task_id, anchor)
    if len(verified) != 1:
        raise RuntimeError(f"Subscription read-back failed for {task_id}")
    current = verified[0]
    if current.get("notifier_profile") != PROFILE or current.get("delivery_mode") != "notify+wake":
        raise RuntimeError(f"Amber subscription read-back failed for {task_id}")


def reconcile(
    conn: sqlite3.Connection, *, dry_run: bool = False, limit: int = MAX_BATCH_SIZE
) -> dict[str, int]:
    tasks = kb.list_tasks(conn, include_archived=False, limit=None)
    subscriptions = kb.list_notify_subs(conn)
    active, anchor, targets, actions = plan(tasks, subscriptions, limit)
    already = sum(
        1
        for task_id in active
        for row in targets.get(task_id, [])
        if row.get("notifier_profile") == PROFILE and row.get("delivery_mode") == "notify+wake"
    )
    result = {
        "active": len(active),
        "already_subscribed": already,
        "missing": len(active) - already,
        "this_run": len(actions),
        "changed": 0,
    }
    if dry_run:
        return result

    # One outer transaction is deliberate: if any CAS/read-back fails, every
    # transfer, mode repair, and new row in this batch is rolled back.
    with kb.write_txn(conn):
        for task_id, action in actions:
            _ensure_amber_notify_wake(conn, task_id, anchor, action)
            result["changed"] += 1
        after = kb.list_notify_subs(conn)
        remaining = 0
        for task_id in active:
            rows = _rows_for_task(
                after,
                task_id,
                chat_id=str(anchor["chat_id"]),
                thread_id=_thread(anchor),
            )
            if not rows or rows[0].get("notifier_profile") != PROFILE or rows[0].get("delivery_mode") != "notify+wake":
                remaining += 1
        result["remaining"] = remaining
        expected = result["missing"] - result["changed"]
        if remaining != expected:
            raise RuntimeError("Amber subscription coverage read-back failed")
    return result


def bounded_limit(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MAX_BATCH_SIZE:
        raise argparse.ArgumentTypeError(f"limit must be between 1 and {MAX_BATCH_SIZE}")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=bounded_limit, default=MAX_BATCH_SIZE)
    args = parser.parse_args(argv)
    if not DB_PATH.is_file():
        print("kanban_subscription_error: canonical board missing; refusing fallback", file=sys.stderr)
        return 1
    try:
        with LOCK_PATH.open("a+") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("kanban_subscription_busy", file=sys.stderr)
                return 75
            if args.dry_run:
                with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
                    conn.row_factory = sqlite3.Row
                    result = reconcile(conn, dry_run=True, limit=args.limit)
            else:
                # Explicit db_path and board make inherited HERMES_KANBAN_*
                # variables irrelevant; no CLI fallback is possible here.
                with kb.connect(DB_PATH, board=BOARD) as conn:
                    result = reconcile(conn, dry_run=False, limit=args.limit)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as error:
        print(f"kanban_subscription_error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
