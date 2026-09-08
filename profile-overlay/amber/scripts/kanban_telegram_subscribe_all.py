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
import hashlib
import json
import os
import sqlite3
import stat
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

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
JOURNAL_ROOT = Path(
    os.environ.get(
        "HERMES_KANBAN_JOURNAL_ROOT",
        "/home/rodrigue/.hermes/profiles/amber/kanban-subscription-journals/iron-rod",
    )
)
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


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_directory(path: Path) -> None:
    """Create a private directory only through verified real ancestors."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    parts = absolute.parts[1:]
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            # The previous component has already been lstat-verified, so this
            # never follows an uninspected ancestor while creating a child.
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("journal path has a symlink or non-directory ancestor")
        if index == len(parts) - 1 and info.st_mode & 0o077:
            os.chmod(current, 0o700)
    _fsync_directory(path)


def _write_private_json(path: Path, payload: Mapping[str, Any], *, exclusive: bool) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _private_directory(path.parent)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RuntimeError("journal file path is unsafe")
    if exclusive and path.exists():
        raise FileExistsError(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("journal write made no progress")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if exclusive:
            # link() is an exclusive publish: unlike rename(), it never
            # replaces a prepared/committed record created by another writer.
            os.link(temporary, path)
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _image_matches(current: Optional[dict[str, Any]], image: Optional[Mapping[str, Any]]) -> bool:
    """Compare a journal image, permitting only an advanced notification cursor."""
    if image is None:
        return current is None
    if current is None:
        return False
    for key, expected in image.items():
        if key == "last_event_id":
            if int(current.get(key) or 0) < int(expected or 0):
                return False
        elif current.get(key) != expected:
            return False
    return True


class BatchJournal:
    """Crash-recoverable private journal for one mutative reconciliation batch."""

    def __init__(self, root: Path, *, phase: str = "forward"):
        _private_directory(root)
        self.root = root
        self.batch_id = uuid.uuid4().hex
        self.path = root / self.batch_id
        if self.path.exists():
            raise RuntimeError("journal batch id collision")
        self.path.mkdir(mode=0o700)
        _private_directory(self.path)
        if self.path.is_symlink() or not self.path.is_dir():
            raise RuntimeError("journal batch path is unsafe")
        os.chmod(self.path, 0o700)
        _fsync_directory(root)
        self.phase = phase
        self.prepared_path = self.path / "prepared.json"
        self.committed_path = self.path / "committed.json"

    def prepare(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        _write_private_json(
            self.prepared_path,
            {"schema_version": 1, "batch_id": self.batch_id, "phase": self.phase, "entries": entries},
            exclusive=True,
        )

    def mark_committed(self) -> None:
        if not self.prepared_path.is_file():
            raise RuntimeError("journal preparation is missing")
        digest = hashlib.sha256(self.prepared_path.read_bytes()).hexdigest()
        _write_private_json(
            self.committed_path,
            {"schema_version": 1, "batch_id": self.batch_id, "prepared_sha256": digest},
            exclusive=True,
        )

    @staticmethod
    def recover_pending(conn: sqlite3.Connection, root: Path) -> None:
        """Resolve an interrupted marker write conservatively before a new pass."""
        if not root.exists():
            return
        _private_directory(root)
        with kb.write_txn(conn):
            for candidate in sorted(root.iterdir()):
                if candidate.is_symlink() or not candidate.is_dir():
                    raise RuntimeError("unsafe journal batch entry")
                prepared = candidate / "prepared.json"
                committed = candidate / "committed.json"
                if committed.exists() or (candidate / "aborted.json").exists() or not prepared.exists():
                    continue
                data = json.loads(prepared.read_text(encoding="utf-8"))
                entries = data.get("entries")
                if not isinstance(entries, list) or not entries:
                    raise RuntimeError("journal preparation is malformed")
                post = pre = True
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise RuntimeError("journal entry is malformed")
                    image = entry.get("post_image")
                    if not isinstance(image, dict):
                        raise RuntimeError("journal post-image is missing")
                    rows = _fetch_target_raw(conn, image)
                    current = rows[0] if len(rows) == 1 else None
                    post = post and _image_matches(current, image)
                    pre = pre and _image_matches(current, entry.get("pre_image"))
                phase = data.get("phase", "forward")
                applied = post if phase == "forward" else pre
                unapplied = pre if phase == "forward" else post
                if applied:
                    journal = BatchJournal.__new__(BatchJournal)
                    journal.path, journal.prepared_path, journal.committed_path = candidate, prepared, committed
                    journal.batch_id = str(data.get("batch_id") or candidate.name)
                    journal.mark_committed()
                elif unapplied:
                    _write_private_json(candidate / "aborted.json", {"batch_id": data.get("batch_id")}, exclusive=True)
                else:
                    raise RuntimeError("journal recovery is ambiguous; refusing a new batch")


def _validate_export_path(path: Path) -> None:
    parent = path.parent
    if parent.exists() and not parent.is_dir():
        raise RuntimeError("journal parent is not a directory")
    parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RuntimeError("journal export path is unsafe")
    if path.exists():
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, list):
            raise RuntimeError("journal export is not a list")


def _append_export(path: Path, entries: list[dict[str, Any]]) -> None:
    """Compatibility export: preserve older entries and never replace on no-op."""
    if not entries:
        return
    _validate_export_path(path)
    old: list[Any] = []
    if path.exists():
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, list):
            raise RuntimeError("journal export is not a list")
        old = parsed
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    # The legacy interface is a JSON list; write it atomically after the
    # durable batch marker, so an export failure never erases recovery data.
    data = (json.dumps(old + entries, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


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
    # Destination identity and origin completeness are separate concerns.
    # Legacy rows may have NULL origin IDs while newer rows carry the same
    # known origin; that is compatible and must not block the transfer.
    destinations = {
        (str(row["chat_id"]), _thread(row), row.get("chat_type") or "dm")
        for row in candidates
    }
    if len(destinations) != 1:
        raise RuntimeError("Missing or ambiguous existing Amber/Forge Telegram DM target")
    chat_id, thread_id, chat_type = next(iter(destinations))
    known_user_ids = sorted({str(row["user_id"]) for row in candidates if row.get("user_id")})
    known_alt_ids = sorted({str(row["user_id_alt"]) for row in candidates if row.get("user_id_alt")})
    if len(known_user_ids) > 1 or len(known_alt_ids) > 1:
        raise RuntimeError("Conflicting existing Telegram DM origin")
    anchor = min(
        (row for row in candidates if _destination(row) == (chat_id, thread_id)),
        key=lambda row: (bool(row.get("user_id")), bool(row.get("user_id_alt")), str(row.get("task_id"))),
    )
    # The anchor remains an existing row.  IDs selected for future rows are
    # deterministic and never enrich or rewrite any existing subscription.
    anchor = dict(anchor)
    anchor["chat_type"] = chat_type
    anchor["user_id"] = known_user_ids[0] if known_user_ids else None
    anchor["user_id_alt"] = known_alt_ids[0] if known_alt_ids else None

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
    return _fetch_target_raw(
        conn,
        {
            "task_id": task_id,
            "platform": "telegram",
            "chat_id": str(anchor["chat_id"]),
            "thread_id": _thread(anchor),
        },
    )


def _fetch_target_raw(conn: sqlite3.Connection, key: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return storage-exact images for journal and guarded rollback use."""
    # Keep the canonical subscription reader within the same transaction used
    # for recovery. Its notifier projection is intentionally not used as the
    # image below because it normalizes NULL metadata, but this call preserves
    # its existing read instrumentation and transaction contract.
    kb.list_notify_subs(conn, str(key.get("task_id")))
    rows = conn.execute(
        "SELECT * FROM kanban_notify_subs WHERE task_id = ? AND platform = ? "
        "AND chat_id = ? AND thread_id = ?",
        (
            str(key.get("task_id")),
            str(key.get("platform", "telegram")),
            str(key.get("chat_id")),
            str(key.get("thread_id") or ""),
        ),
    ).fetchall()
    return [dict(row) for row in rows]


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


def rollback_journal(conn: sqlite3.Connection, journal: Iterable[Mapping[str, Any]]) -> int:
    """Reverse one committed journal without overwriting concurrent changes."""
    entries = list(journal)
    if not entries:
        return 0
    # The inverse itself is a durable operation.  Its prepared record is
    # written before the database transaction, so a crash after its commit is
    # classified conservatively by recover_pending on the next pass.
    inverse = BatchJournal(JOURNAL_ROOT, phase="inverse")
    inverse.prepare(entries)
    restored = 0
    with kb.write_txn(conn):
        for entry in reversed(entries):
            pre_image = entry.get("pre_image")
            post_image = entry.get("post_image")
            if not isinstance(post_image, dict):
                raise RuntimeError("rollback journal entry has no post-image")
            if not kb.restore_notify_sub_state(
                conn, pre_image=pre_image if isinstance(pre_image, dict) else None,
                post_image=post_image,
            ):
                key = ":".join(str(post_image.get(field, "")) for field in ("task_id", "chat_id", "thread_id"))
                raise RuntimeError(f"rollback conflict for {key}")
            restored += 1
    inverse.mark_committed()
    return restored


def reconcile(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    limit: int = MAX_BATCH_SIZE,
    journal: Optional[list[dict[str, Any]]] = None,
    prepare_journal: Optional[Callable[[list[dict[str, Any]]], None]] = None,
) -> dict[str, int]:
    if dry_run:
        tasks = kb.list_tasks(conn, include_archived=False, limit=None)
        subscriptions = kb.list_notify_subs(conn)
        active, _anchor, targets, actions = plan(tasks, subscriptions, limit)
        already = sum(
            1
            for task_id in active
            for row in targets.get(task_id, [])
            if row.get("notifier_profile") == PROFILE and row.get("delivery_mode") == "notify+wake"
        )
        return {
            "active": len(active),
            "already_subscribed": already,
            "missing": len(active) - already,
            "this_run": len(actions),
            "changed": 0,
        }

    pending_journal: list[dict[str, Any]] = []
    # One outer transaction is deliberate: if any CAS/read-back fails, every
    # transfer, mode repair, and new row in this batch is rolled back.  The
    # plan and every pre-image are captured only after BEGIN IMMEDIATE, so a
    # native writer cannot make a stale pre-image rollback its own update.
    with kb.write_txn(conn):
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
        for task_id, action in actions:
            before_rows = _fetch_target(conn, task_id, anchor)
            if len(before_rows) > 1:
                raise RuntimeError(f"Conflicting duplicate subscriptions for {task_id}")
            pre_image = dict(before_rows[0]) if before_rows else None
            _ensure_amber_notify_wake(conn, task_id, anchor, action)
            after_rows = _fetch_target(conn, task_id, anchor)
            if len(after_rows) != 1:
                raise RuntimeError(f"Journal read-back failed for {task_id}")
            pending_journal.append({
                "task_id": task_id,
                "action": action,
                "pre_image": pre_image,
                "post_image": dict(after_rows[0]),
            })
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
        # Persist exact images while the SQLite transaction is still open.
        # A failed fsync/write raises here and makes write_txn roll back; a
        # crash after DB commit but before the committed marker is resolved by
        # BatchJournal.recover_pending using these complete images.
        if pending_journal and prepare_journal is not None:
            prepare_journal(pending_journal)
    if journal is not None:
        journal.extend(pending_journal)
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
    parser.add_argument(
        "--journal",
        type=Path,
        help="write the committed batch's exact pre/post-images to this JSON path",
    )
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
                if args.journal is not None:
                    # Validate before the transaction: a bad compatibility
                    # export cannot turn a committed batch into an
                    # unjournaled batch. The durable private journal below is
                    # still authoritative for recovery.
                    _validate_export_path(args.journal)
                committed_journal: list[dict[str, Any]] = []
                with kb.connect(DB_PATH, board=BOARD) as conn:
                    BatchJournal.recover_pending(conn, JOURNAL_ROOT)
                    batch: list[BatchJournal] = []

                    def prepare(entries: list[dict[str, Any]]) -> None:
                        current = BatchJournal(JOURNAL_ROOT)
                        current.prepare(entries)
                        batch.append(current)

                    result = reconcile(
                        conn,
                        dry_run=False,
                        limit=args.limit,
                        journal=committed_journal,
                        prepare_journal=prepare,
                    )
                    if batch:
                        batch[0].mark_committed()
                if args.journal is not None:
                    _append_export(args.journal, committed_journal)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as error:
        print(f"kanban_subscription_error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
