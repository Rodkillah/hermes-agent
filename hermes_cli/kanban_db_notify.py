"""Notification subscriptions consumed by the gateway kanban-notifier: per-(task, platform, chat, thread) rows with delivery metadata, unseen-event cursors and purge of stale done-task subs.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from typing import Iterable
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Event


# Notifier reaction to a terminal event: "notify" = passive adapter.send only
# (default); "notify+wake" = send AND wake the destination agent; "wake" = wake only.
_NOTIFY_DELIVERY_MODES = ("notify", "notify+wake", "wake")
_V2_DEFAULT_EVENT_KINDS = (
    "completed", "blocked", "gave_up", "crashed", "timed_out", "status",
    "archived", "unblocked", "block_loop_detected", "block_loop_resolved",
    "review_requested", "changes_requested", "production_promoted",
)

_SCALAR_TYPES = (str, int, float, bool)

# Subscription primary key predicate; every per-row statement below binds
# ``(task_id, platform, chat_id, thread_id or "")`` against it.
_SUB_KEY_WHERE = "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?"


def _sub_key(task_id: str, platform: str, chat_id: str, thread_id: Optional[str]) -> tuple:
    return (task_id, platform, chat_id, thread_id or "")


def _lease_guard(subscription_id: Optional[str]) -> tuple[str, tuple]:
    if not subscription_id:
        return "", ()
    return " AND subscription_id = ?", (subscription_id,)


def _encode_notify_delivery_metadata(metadata: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Serialize platform send metadata stored on notification subscriptions."""
    if not isinstance(metadata, Mapping):
        return None
    clean = {
        str(key): value
        for key, value in metadata.items()
        if value is not None and isinstance(value, _SCALAR_TYPES)
    }
    if not clean:
        return None
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def _decode_notify_delivery_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, _SCALAR_TYPES)}


def add_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_id_alt: Optional[str] = None,
    chat_type: Optional[str] = None,
    notifier_profile: Optional[str] = None,
    delivery_mode: Optional[str] = None,
    delivery_metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    """Register a gateway source wanting terminal-state notifications for
    ``task_id``; idempotent on (task, platform, chat, thread).

    ``user_id_alt`` (Signal UUID, Feishu union_id, ...) and ``chat_type`` are
    replayed on active wake: ``build_session_key`` prefers the alt id, so
    omitting it would key the wake into a different session. ``None`` keeps an
    existing row's value. ``delivery_mode``: ``None`` leaves an existing row
    untouched, an explicit valid value is last-write-wins, unknown falls back
    to ``"notify"``. ``delivery_metadata`` merges supplied routing anchors
    into an existing row so re-subscribing never discards them. New subs start
    caught up (``last_event_id`` =
    ``MAX(task_events.id)``) so the notifier never replays history at boot.
    """
    valid_mode = delivery_mode if delivery_mode in _NOTIFY_DELIVERY_MODES else None
    # api_server is stateless: the adapter has no send(), the wake self-post IS
    # the delivery. A plain 'notify' default would leave those subs with no
    # delivery mechanism at all. Explicit modes still win.
    insert_mode = valid_mode or ("notify+wake" if platform == "api_server" else "notify")
    key = _sub_key(task_id, platform, chat_id, thread_id)
    subscription_id = uuid.uuid4().hex
    with _kb.write_txn(conn):
        # Merge the supplied routing anchors over whatever the row already
        # carries: re-subscribing must never discard an anchor written by an
        # earlier call (upstream fix for lost Discord thread routes).
        existing = conn.execute(
            "SELECT delivery_metadata FROM kanban_notify_subs " + _SUB_KEY_WHERE,
            key,
        ).fetchone()
        existing_metadata = _decode_notify_delivery_metadata(existing["delivery_metadata"]) if existing else {}
        merged_metadata = dict(existing_metadata)
        if delivery_metadata:
            merged_metadata.update(delivery_metadata)
        metadata_json = _encode_notify_delivery_metadata(merged_metadata) if merged_metadata else None
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO kanban_notify_subs
                (task_id, platform, chat_id, thread_id, user_id, user_id_alt,
                 chat_type, notifier_profile, delivery_mode, delivery_metadata,
                 subscription_id, created_at, last_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = ?), 0))
            """,
            (
                *key, user_id, user_id_alt, chat_type or "dm", notifier_profile,
                insert_mode, metadata_json, subscription_id, int(time.time()), task_id,
            ),
        ).rowcount > 0
        # chat_type / delivery_mode / delivery_metadata are last-write-wins;
        # user_id_alt and notifier_profile only self-heal legacy rows lacking one.
        changed = False
        for column, value, fill_only in (
            ("chat_type", chat_type, False),
            ("user_id_alt", user_id_alt, True),
            ("notifier_profile", notifier_profile, True),
            ("delivery_mode", valid_mode, False),
            ("delivery_metadata", metadata_json, False),
        ):
            if not value:
                continue
            guard = f" AND ({column} IS NULL OR {column} = '')" if fill_only else f" AND {column} IS NOT ?"
            cur = conn.execute(
                f"UPDATE kanban_notify_subs SET {column} = ? " + _SUB_KEY_WHERE + guard,
                (value, *key) if fill_only else (value, *key, value),
            )
            changed = changed or cur.rowcount > 0
        # A real rebind starts a new durable lease so in-flight delivery under
        # the prior authority fails closed. A byte-for-byte idempotent call must
        # preserve the lease or it can strand an event already claimed by it.
        if changed and not inserted:
            conn.execute(
                "UPDATE kanban_notify_subs SET subscription_id = ? " + _SUB_KEY_WHERE,
                (subscription_id, *key),
            )


# --- v2 physical routes + explicit bot/runtime authorities ---

_V2_SOURCE_KINDS = ("default", "creator", "inherited", "manual", "legacy")
_V2_ROUTE_WHERE = "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?"
_V2_AUTHORITY_WHERE = (
    _V2_ROUTE_WHERE + " AND bot_profile = ? AND notifier_profile = ?"
)


def _v2_route_key(
    task_id: str, platform: str, chat_id: str, thread_id: Optional[str]
) -> tuple[str, str, str, str]:
    return task_id, platform, chat_id, thread_id or ""


def _normalize_v2_platform(platform: Any, delivery_mode: str) -> str:
    try:
        from gateway.config import Platform
        normalized = Platform(str(platform).strip().lower()).value
    except Exception:
        raise ValueError("notification authority has unsupported platform") from None
    if normalized == Platform.API_SERVER.value and delivery_mode != "wake":
        raise ValueError("notification authority platform cannot push notifications")
    return normalized


def _validate_v2_authority(
    *, platform: Any, bot_profile: Any, notifier_profile: Any,
    delivery_mode: Any, ping_priority: Any, source_kind: Any,
) -> tuple[str, str, str, str, int, str]:
    mode = str(delivery_mode or "").strip()
    if mode not in _NOTIFY_DELIVERY_MODES:
        raise ValueError("notification authority has unsupported delivery_mode")
    normalized_platform = _normalize_v2_platform(platform, mode)
    bot = str(bot_profile or "").strip()
    runtime = str(notifier_profile or "").strip()
    if not bot or not runtime:
        raise ValueError("notification authority requires bot_profile and notifier_profile")
    if isinstance(ping_priority, bool):
        raise ValueError("notification authority ping_priority must be an integer")
    try:
        priority = int(ping_priority)
    except (TypeError, ValueError):
        raise ValueError("notification authority ping_priority must be an integer") from None
    source = str(source_kind or "").strip()
    if source not in _V2_SOURCE_KINDS:
        raise ValueError("notification authority has unsupported source_kind")
    return normalized_platform, bot, runtime, mode, priority, source


def _current_task_event_id(conn: sqlite3.Connection, task_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS cursor FROM task_events WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row["cursor"] if row is not None else 0)


def _refresh_ping_election(conn: sqlite3.Connection, route_key: tuple) -> None:
    route = conn.execute(
        "SELECT route_id, ping_subscription_id FROM kanban_notify_routes "
        + _V2_ROUTE_WHERE,
        route_key,
    ).fetchone()
    if route is None:
        return
    candidates = conn.execute(
        "SELECT subscription_id, ping_priority FROM kanban_notify_authorities "
        + _V2_ROUTE_WHERE
        + " AND delivery_mode IN ('notify', 'notify+wake') "
        "ORDER BY ping_priority DESC",
        route_key,
    ).fetchall()
    elected = None
    if candidates:
        highest = int(candidates[0]["ping_priority"])
        winners = [row for row in candidates if int(row["ping_priority"]) == highest]
        if len(winners) == 1:
            elected = winners[0]["subscription_id"]
    if route["ping_subscription_id"] == elected:
        return
    conn.execute(
        "UPDATE kanban_notify_routes SET route_id = ?, ping_subscription_id = ?, "
        "claim_event_id = NULL, claim_token = NULL, claim_expires_at = NULL "
        + _V2_ROUTE_WHERE,
        (uuid.uuid4().hex, elected, *route_key),
    )


def _assert_v2_authority_constraints(
    conn: sqlite3.Connection,
    *,
    route_key: tuple[str, str, str, str],
    bot_profile: str,
    notifier_profile: str,
    delivery_mode: str,
    ping_priority: int,
) -> None:
    """Validate the proposed authority against the route's future state."""
    others = conn.execute(
        "SELECT bot_profile, notifier_profile, delivery_mode, ping_priority "
        "FROM kanban_notify_authorities " + _V2_ROUTE_WHERE
        + " AND NOT (bot_profile = ? AND notifier_profile = ?)",
        (*route_key, bot_profile, notifier_profile),
    ).fetchall()
    if delivery_mode in ("wake", "notify+wake") and any(
        row["notifier_profile"] == notifier_profile
        and row["delivery_mode"] in ("wake", "notify+wake")
        for row in others
    ):
        raise ValueError("notification wake runtime already has an authority on this route")

    priorities = [
        int(row["ping_priority"])
        for row in others
        if row["delivery_mode"] in ("notify", "notify+wake")
    ]
    if delivery_mode in ("notify", "notify+wake"):
        priorities.append(ping_priority)
    if priorities:
        highest = max(priorities)
        if priorities.count(highest) > 1:
            raise ValueError("notification route has ambiguous ping priority")


def add_notify_authority(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    bot_profile: str,
    notifier_profile: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_id_alt: Optional[str] = None,
    chat_type: Optional[str] = None,
    delivery_mode: str = "notify",
    delivery_metadata: Optional[Mapping[str, Any]] = None,
    ping_priority: int = 0,
    source_kind: str = "manual",
    legacy_subscription_id: Optional[str] = None,
    start_ping_cursor: Optional[int] = None,
    start_wake_cursor: Optional[int] = None,
) -> str:
    """Create or update one explicit v2 authority and return its durable lease.

    The route remains unique at the physical destination.  A byte-identical call
    preserves both route and authority leases; a durable authority change rotates
    the authority lease and, when election changes, the route lease.  A legacy
    row on the same route must be explicitly linked before v2 can share it.
    """
    platform, bot_profile, notifier_profile, delivery_mode, ping_priority, source_kind = (
        _validate_v2_authority(
            platform=platform, bot_profile=bot_profile,
            notifier_profile=notifier_profile, delivery_mode=delivery_mode,
            ping_priority=ping_priority, source_kind=source_kind,
        )
    )
    chat_id = str(chat_id or "").strip()
    if not task_id or not chat_id:
        raise ValueError("notification authority requires task_id and chat_id")
    route_key = _v2_route_key(task_id, platform, chat_id, thread_id)
    authority_key = (*route_key, bot_profile, notifier_profile)
    metadata_json = _encode_notify_delivery_metadata(delivery_metadata)
    legacy_subscription_id = str(legacy_subscription_id or "").strip() or None
    now = int(time.time())

    with _kb.write_txn(conn, allow_nested=True):
        unlinked = conn.execute(
            "SELECT s.subscription_id FROM kanban_notify_subs s "
            + _V2_ROUTE_WHERE.replace("WHERE", "WHERE s.", 1)
            .replace(" AND ", " AND s.")
            + " AND NOT EXISTS (SELECT 1 FROM kanban_notify_authorities a "
            "WHERE a.legacy_subscription_id = s.subscription_id)",
            route_key,
        ).fetchone()
        if unlinked is not None and unlinked["subscription_id"] != legacy_subscription_id:
            raise ValueError("notification route has an unlinked legacy subscription")
        _assert_v2_authority_constraints(
            conn,
            route_key=route_key,
            bot_profile=bot_profile,
            notifier_profile=notifier_profile,
            delivery_mode=delivery_mode,
            ping_priority=ping_priority,
        )

        current_cursor = _current_task_event_id(conn, task_id)
        ping_cursor = current_cursor if start_ping_cursor is None else int(start_ping_cursor)
        wake_cursor = current_cursor if start_wake_cursor is None else int(start_wake_cursor)
        conn.execute(
            "INSERT OR IGNORE INTO kanban_notify_routes "
            "(task_id, platform, chat_id, thread_id, route_id, created_at, last_ping_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*route_key, uuid.uuid4().hex, now, ping_cursor),
        )
        if start_ping_cursor is not None:
            conn.execute(
                "UPDATE kanban_notify_routes SET last_ping_event_id = "
                "MAX(last_ping_event_id, ?) " + _V2_ROUTE_WHERE,
                (ping_cursor, *route_key),
            )

        existing = conn.execute(
            "SELECT * FROM kanban_notify_authorities " + _V2_AUTHORITY_WHERE,
            authority_key,
        ).fetchone()
        durable = (
            delivery_mode, ping_priority, source_kind, user_id, user_id_alt,
            chat_type or "dm", metadata_json, legacy_subscription_id,
        )
        if existing is None:
            subscription_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO kanban_notify_authorities "
                "(task_id, platform, chat_id, thread_id, bot_profile, notifier_profile, "
                "subscription_id, legacy_subscription_id, delivery_mode, ping_priority, "
                "source_kind, user_id, user_id_alt, chat_type, delivery_metadata, "
                "last_wake_event_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*authority_key, subscription_id, legacy_subscription_id, delivery_mode,
                 ping_priority, source_kind, user_id, user_id_alt, chat_type or "dm",
                 metadata_json, wake_cursor, now),
            )
        else:
            old_durable = (
                existing["delivery_mode"], int(existing["ping_priority"]),
                existing["source_kind"], existing["user_id"], existing["user_id_alt"],
                existing["chat_type"], existing["delivery_metadata"],
                existing["legacy_subscription_id"],
            )
            subscription_id = existing["subscription_id"]
            if old_durable != durable:
                subscription_id = uuid.uuid4().hex
                conn.execute(
                    "UPDATE kanban_notify_authorities SET subscription_id = ?, "
                    "legacy_subscription_id = ?, delivery_mode = ?, ping_priority = ?, "
                    "source_kind = ?, user_id = ?, user_id_alt = ?, chat_type = ?, "
                    "delivery_metadata = ?, claim_event_id = NULL, claim_token = NULL, "
                    "claim_expires_at = NULL " + _V2_AUTHORITY_WHERE,
                    (subscription_id, legacy_subscription_id, delivery_mode, ping_priority,
                     source_kind, user_id, user_id_alt, chat_type or "dm", metadata_json,
                     *authority_key),
                )
        _refresh_ping_election(conn, route_key)
        return str(subscription_id)


def list_notify_routes(
    conn: sqlite3.Connection, task_id: Optional[str] = None
) -> list[dict]:
    sql = "SELECT * FROM kanban_notify_routes"
    params: tuple = ()
    if task_id is not None:
        sql += " WHERE task_id = ?"
        params = (task_id,)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def list_notify_authorities(
    conn: sqlite3.Connection, task_id: Optional[str] = None
) -> list[dict]:
    sql = "SELECT * FROM kanban_notify_authorities"
    params: tuple = ()
    if task_id is not None:
        sql += " WHERE task_id = ?"
        params = (task_id,)
    out = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        item["delivery_metadata"] = _decode_notify_delivery_metadata(
            item.get("delivery_metadata")
        )
        out.append(item)
    return out


def remove_notify_authority(
    conn: sqlite3.Connection, *, subscription_id: str
) -> bool:
    """Remove exactly one current authority lease and collect an empty route."""
    with _kb.write_txn(conn, allow_nested=True):
        row = conn.execute(
            "SELECT * FROM kanban_notify_authorities WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()
        if row is None:
            return False
        route_key = _v2_route_key(
            row["task_id"], row["platform"], row["chat_id"], row["thread_id"]
        )
        cur = conn.execute(
            "DELETE FROM kanban_notify_authorities WHERE subscription_id = ?",
            (subscription_id,),
        )
        legacy_id = row["legacy_subscription_id"]
        if legacy_id:
            conn.execute(
                "DELETE FROM kanban_notify_subs WHERE subscription_id = ?",
                (legacy_id,),
            )
        remaining = conn.execute(
            "SELECT 1 FROM kanban_notify_authorities " + _V2_ROUTE_WHERE + " LIMIT 1",
            route_key,
        ).fetchone()
        if remaining is None:
            conn.execute(
                "DELETE FROM kanban_notify_routes " + _V2_ROUTE_WHERE,
                route_key,
            )
        else:
            _refresh_ping_election(conn, route_key)
        return bool(cur.rowcount)


def remove_notify_authority_by_identity(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    bot_profile: str,
    notifier_profile: str,
    thread_id: Optional[str] = None,
) -> bool:
    """Remove one v2 authority by its complete public identity."""
    platform, bot_profile, notifier_profile, _mode, _priority, _source = (
        _validate_v2_authority(
            platform=platform,
            bot_profile=bot_profile,
            notifier_profile=notifier_profile,
            delivery_mode="wake",
            ping_priority=0,
            source_kind="manual",
        )
    )
    key = (
        *_v2_route_key(task_id, platform, str(chat_id or "").strip(), thread_id),
        bot_profile,
        notifier_profile,
    )
    with _kb.write_txn(conn, allow_nested=True):
        row = conn.execute(
            "SELECT subscription_id FROM kanban_notify_authorities "
            + _V2_AUTHORITY_WHERE,
            key,
        ).fetchone()
        if row is None:
            return False
        return remove_notify_authority(
            conn, subscription_id=str(row["subscription_id"])
        )


def _next_v2_event(
    conn: sqlite3.Connection, *, task_id: str, after_id: int,
    event_kinds: Optional[Iterable[str]] = None,
) -> Optional[dict]:
    kinds = tuple(event_kinds or _V2_DEFAULT_EVENT_KINDS)
    if not kinds:
        return None
    placeholders = ",".join("?" for _ in kinds)
    row = conn.execute(
        "SELECT id, task_id, kind, payload, created_at FROM task_events "
        f"WHERE task_id = ? AND id > ? AND kind IN ({placeholders}) "
        "ORDER BY id ASC LIMIT 1",
        (task_id, int(after_id), *kinds),
    ).fetchone()
    if row is None:
        return None
    event = dict(row)
    try:
        event["payload"] = json.loads(event["payload"] or "{}")
    except (TypeError, ValueError):
        event["payload"] = {}
    return event


def claim_notify_ping(
    conn: sqlite3.Connection,
    *,
    route_id: str,
    event_kinds: Optional[Iterable[str]] = None,
    now: Optional[int] = None,
    lease_seconds: int = 60,
) -> Optional[dict]:
    """Atomically claim the next event for one elected physical ping route."""
    claimed_at = int(time.time()) if now is None else int(now)
    expires_at = claimed_at + max(1, int(lease_seconds))
    with _kb.write_txn(conn, allow_nested=True):
        row = conn.execute(
            "SELECT r.*, a.bot_profile, a.notifier_profile, a.delivery_mode, "
            "a.subscription_id, a.user_id, a.user_id_alt, a.chat_type, "
            "a.delivery_metadata FROM kanban_notify_routes r "
            "JOIN kanban_notify_authorities a "
            "ON a.subscription_id = r.ping_subscription_id "
            "WHERE r.route_id = ? AND a.delivery_mode IN ('notify', 'notify+wake')",
            (route_id,),
        ).fetchone()
        if row is None:
            return None
        if row["claim_token"] and int(row["claim_expires_at"] or 0) > claimed_at:
            return None
        event = _next_v2_event(
            conn,
            task_id=row["task_id"],
            after_id=int(row["last_ping_event_id"] or 0),
            event_kinds=event_kinds,
        )
        if event is None:
            return None
        token = uuid.uuid4().hex
        cur = conn.execute(
            "UPDATE kanban_notify_routes SET claim_event_id = ?, claim_token = ?, "
            "claim_expires_at = ? WHERE route_id = ? "
            "AND ping_subscription_id = ? "
            "AND (claim_token IS NULL OR claim_expires_at <= ?)",
            (event["id"], token, expires_at, route_id,
             row["ping_subscription_id"], claimed_at),
        )
        if cur.rowcount != 1:
            return None
        claim = dict(row)
        claim["delivery_metadata"] = _decode_notify_delivery_metadata(
            claim.get("delivery_metadata")
        )
        claim.update({"flow": "ping", "claim_token": token, "event": event})
        return claim


def claim_notify_wake(
    conn: sqlite3.Connection,
    *,
    subscription_id: str,
    event_kinds: Optional[Iterable[str]] = None,
    now: Optional[int] = None,
    lease_seconds: int = 60,
) -> Optional[dict]:
    """Atomically claim the next event for one runtime wake authority."""
    claimed_at = int(time.time()) if now is None else int(now)
    expires_at = claimed_at + max(1, int(lease_seconds))
    with _kb.write_txn(conn, allow_nested=True):
        row = conn.execute(
            "SELECT a.*, r.route_id FROM kanban_notify_authorities a "
            "JOIN kanban_notify_routes r USING (task_id, platform, chat_id, thread_id) "
            "WHERE a.subscription_id = ? "
            "AND a.delivery_mode IN ('wake', 'notify+wake')",
            (subscription_id,),
        ).fetchone()
        if row is None:
            return None
        if row["claim_token"] and int(row["claim_expires_at"] or 0) > claimed_at:
            return None
        event = _next_v2_event(
            conn,
            task_id=row["task_id"],
            after_id=int(row["last_wake_event_id"] or 0),
            event_kinds=event_kinds,
        )
        if event is None:
            return None
        token = uuid.uuid4().hex
        cur = conn.execute(
            "UPDATE kanban_notify_authorities SET claim_event_id = ?, "
            "claim_token = ?, claim_expires_at = ? WHERE subscription_id = ? "
            "AND (claim_token IS NULL OR claim_expires_at <= ?)",
            (event["id"], token, expires_at, subscription_id, claimed_at),
        )
        if cur.rowcount != 1:
            return None
        claim = dict(row)
        claim["delivery_metadata"] = _decode_notify_delivery_metadata(
            claim.get("delivery_metadata")
        )
        claim.update({"flow": "wake", "claim_token": token, "event": event})
        return claim


def notify_v2_claim_is_current(
    conn: sqlite3.Connection,
    *,
    flow: str,
    lease_id: str,
    claim_token: str,
    event_id: int,
    now: Optional[int] = None,
) -> bool:
    checked_at = int(time.time()) if now is None else int(now)
    if flow == "ping":
        table, lease_column = "kanban_notify_routes", "route_id"
    elif flow == "wake":
        table, lease_column = "kanban_notify_authorities", "subscription_id"
    else:
        return False
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE {lease_column} = ? "
        "AND claim_token = ? AND claim_event_id = ? AND claim_expires_at > ?",
        (lease_id, claim_token, int(event_id), checked_at),
    ).fetchone()
    return row is not None


def _settle_v2_claim(
    conn: sqlite3.Connection, *, flow: str, lease_id: str,
    claim_token: str, event_id: int,
) -> bool:
    if flow == "ping":
        table, lease_column, cursor = (
            "kanban_notify_routes", "route_id", "last_ping_event_id"
        )
    elif flow == "wake":
        table, lease_column, cursor = (
            "kanban_notify_authorities", "subscription_id", "last_wake_event_id"
        )
    else:
        return False
    with _kb.write_txn(conn, allow_nested=True):
        cur = conn.execute(
            f"UPDATE {table} SET {cursor} = MAX({cursor}, ?), "
            "claim_event_id = NULL, claim_token = NULL, claim_expires_at = NULL "
            f"WHERE {lease_column} = ? AND claim_token = ? AND claim_event_id = ?",
            (int(event_id), lease_id, claim_token, int(event_id)),
        )
    return cur.rowcount == 1


def _release_v2_claim(
    conn: sqlite3.Connection, *, flow: str, lease_id: str,
    claim_token: str, event_id: int,
) -> bool:
    if flow == "ping":
        table, lease_column = "kanban_notify_routes", "route_id"
    elif flow == "wake":
        table, lease_column = "kanban_notify_authorities", "subscription_id"
    else:
        return False
    with _kb.write_txn(conn, allow_nested=True):
        cur = conn.execute(
            f"UPDATE {table} SET claim_event_id = NULL, claim_token = NULL, "
            f"claim_expires_at = NULL WHERE {lease_column} = ? "
            "AND claim_token = ? AND claim_event_id = ?",
            (lease_id, claim_token, int(event_id)),
        )
    return cur.rowcount == 1


def settle_notify_ping(
    conn: sqlite3.Connection, *, route_id: str, claim_token: str, event_id: int
) -> bool:
    return _settle_v2_claim(
        conn, flow="ping", lease_id=route_id,
        claim_token=claim_token, event_id=event_id,
    )


def settle_notify_wake(
    conn: sqlite3.Connection, *, subscription_id: str,
    claim_token: str, event_id: int,
) -> bool:
    return _settle_v2_claim(
        conn, flow="wake", lease_id=subscription_id,
        claim_token=claim_token, event_id=event_id,
    )


def release_notify_ping(
    conn: sqlite3.Connection, *, route_id: str, claim_token: str, event_id: int
) -> bool:
    return _release_v2_claim(
        conn, flow="ping", lease_id=route_id,
        claim_token=claim_token, event_id=event_id,
    )


def release_notify_wake(
    conn: sqlite3.Connection, *, subscription_id: str,
    claim_token: str, event_id: int,
) -> bool:
    return _release_v2_claim(
        conn, flow="wake", lease_id=subscription_id,
        claim_token=claim_token, event_id=event_id,
    )


def inherit_notify_authorities(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    creator_task_id: Optional[str] = None,
    parent_ids: Iterable[str] = (),
) -> int:
    """Copy explicit authorities from creator then parents without overwriting.

    Each copied authority receives a child-local lease and starts after the
    child's current event cursor. Repeated source identities are first-wins, so
    creator, parent, and later configured defaults cannot replace each other.
    """
    sources: list[tuple[str, str]] = []
    if creator_task_id:
        sources.append((creator_task_id, "inherited"))
    sources.extend((parent_id, "inherited") for parent_id in parent_ids)
    seen_sources: set[str] = set()
    seen_keys: set[tuple] = set()
    inserted = 0
    cursor = _current_task_event_id(conn, task_id)
    for source_task_id, source_kind in sources:
        if not source_task_id or source_task_id in seen_sources:
            continue
        seen_sources.add(source_task_id)
        for authority in list_notify_authorities(conn, source_task_id):
            key = (
                task_id, authority["platform"], authority["chat_id"],
                authority["thread_id"] or "", authority["bot_profile"],
                authority["notifier_profile"],
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if conn.execute(
                "SELECT 1 FROM kanban_notify_authorities " + _V2_AUTHORITY_WHERE,
                key,
            ).fetchone() is not None:
                continue
            add_notify_authority(
                conn,
                task_id=task_id,
                platform=authority["platform"],
                chat_id=authority["chat_id"],
                thread_id=authority["thread_id"],
                user_id=authority["user_id"],
                user_id_alt=authority["user_id_alt"],
                chat_type=authority["chat_type"],
                bot_profile=authority["bot_profile"],
                notifier_profile=authority["notifier_profile"],
                delivery_mode=authority["delivery_mode"],
                delivery_metadata=authority["delivery_metadata"],
                ping_priority=int(authority["ping_priority"]),
                source_kind=source_kind,
                start_ping_cursor=cursor,
                start_wake_cursor=cursor,
            )
            inserted += 1
    return inserted


def migrate_notify_authorities(
    conn: sqlite3.Connection, *, mappings: Iterable[Mapping[str, Any]], apply: bool = False
) -> dict[str, int]:
    """Plan or atomically link every unlinked legacy row to an explicit v2 authority.

    ``mappings`` contains only ``subscription_id``, ``bot_profile`` and, for an
    ownerless legacy row, ``notifier_profile``.  Counts are intentionally the
    only result so private route identities never reach dry-run output or logs.
    Missing or duplicate candidates make the whole apply a no-op.
    """
    by_subscription: dict[str, list[Mapping[str, Any]]] = {}
    for raw in mappings:
        if not isinstance(raw, Mapping):
            continue
        subscription_id = str(raw.get("subscription_id") or "").strip()
        if subscription_id:
            by_subscription.setdefault(subscription_id, []).append(raw)

    rows = conn.execute(
        "SELECT s.* FROM kanban_notify_subs s ORDER BY s.subscription_id"
    ).fetchall()
    planned: list[tuple[sqlite3.Row, Mapping[str, Any], str, str]] = []
    result = {
        "mappable": 0,
        "unmapped": 0,
        "ambiguous": 0,
        "already_migrated": 0,
        "applied": 0,
    }
    for row in rows:
        subscription_id = str(row["subscription_id"] or "")
        linked = conn.execute(
            "SELECT subscription_id FROM kanban_notify_authorities "
            "WHERE legacy_subscription_id = ?",
            (subscription_id,),
        ).fetchone()
        if linked is not None:
            result["already_migrated"] += 1
            continue
        candidates = by_subscription.get(subscription_id, [])
        if len(candidates) > 1:
            result["ambiguous"] += 1
            continue
        if not candidates:
            result["unmapped"] += 1
            continue
        candidate = candidates[0]
        bot_profile = str(candidate.get("bot_profile") or "").strip()
        notifier_profile = str(
            row["notifier_profile"] or candidate.get("notifier_profile") or ""
        ).strip()
        if not bot_profile or not notifier_profile:
            result["unmapped"] += 1
            continue
        planned.append((row, candidate, bot_profile, notifier_profile))
        result["mappable"] += 1

    if not apply or result["unmapped"] or result["ambiguous"]:
        return result

    with _kb.write_txn(conn):
        for row, _candidate, bot_profile, notifier_profile in planned:
            mode = row["delivery_mode"] or "notify"
            add_notify_authority(
                conn,
                task_id=row["task_id"],
                platform=row["platform"],
                chat_id=row["chat_id"],
                thread_id=row["thread_id"] or "",
                user_id=row["user_id"],
                user_id_alt=row["user_id_alt"],
                chat_type=row["chat_type"],
                bot_profile=bot_profile,
                notifier_profile=notifier_profile,
                delivery_mode=mode,
                delivery_metadata=_decode_notify_delivery_metadata(
                    row["delivery_metadata"]
                ),
                ping_priority=0,
                source_kind="legacy",
                legacy_subscription_id=row["subscription_id"],
                start_ping_cursor=int(row["last_ping_event_id"] or 0),
                start_wake_cursor=(
                    int(row["last_event_id"] or 0)
                    if mode in ("wake", "notify+wake") else 0
                ),
            )
        result["applied"] = len(planned)
    return result


def project_notify_authority_to_legacy(
    conn: sqlite3.Connection, *, subscription_id: str
) -> str:
    """Project one explicitly selected v2 authority for a legacy rollback."""
    row = conn.execute(
        "SELECT a.*, r.last_ping_event_id FROM kanban_notify_authorities a "
        "JOIN kanban_notify_routes r USING (task_id, platform, chat_id, thread_id) "
        "WHERE a.subscription_id = ?",
        (subscription_id,),
    ).fetchone()
    if row is None:
        raise ValueError("notification authority lease is not current")
    mode = str(row["delivery_mode"])
    ping_cursor = int(row["last_ping_event_id"] or 0)
    wake_cursor = int(row["last_wake_event_id"] or 0)
    if mode == "notify+wake":
        event_cursor = min(ping_cursor, wake_cursor)
    elif mode == "wake":
        event_cursor = wake_cursor
    else:
        event_cursor = ping_cursor
    existing = conn.execute(
        "SELECT subscription_id FROM kanban_notify_subs " + _SUB_KEY_WHERE,
        _v2_route_key(row["task_id"], row["platform"], row["chat_id"], row["thread_id"]),
    ).fetchone()
    legacy_id = (
        str(row["legacy_subscription_id"] or "")
        or (str(existing["subscription_id"]) if existing is not None else uuid.uuid4().hex)
    )
    now = int(time.time())
    with _kb.write_txn(conn, allow_nested=True):
        conn.execute(
            "INSERT INTO kanban_notify_subs "
            "(task_id, platform, chat_id, thread_id, user_id, user_id_alt, chat_type, "
            "notifier_profile, delivery_mode, delivery_metadata, last_event_id, "
            "last_ping_event_id, subscription_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id, platform, chat_id, thread_id) DO UPDATE SET "
            "user_id=excluded.user_id, user_id_alt=excluded.user_id_alt, "
            "chat_type=excluded.chat_type, notifier_profile=excluded.notifier_profile, "
            "delivery_mode=excluded.delivery_mode, delivery_metadata=excluded.delivery_metadata, "
            "last_event_id=excluded.last_event_id, "
            "last_ping_event_id=excluded.last_ping_event_id, "
            "subscription_id=excluded.subscription_id",
            (
                row["task_id"], row["platform"], row["chat_id"], row["thread_id"],
                row["user_id"], row["user_id_alt"], row["chat_type"],
                row["notifier_profile"], mode, row["delivery_metadata"],
                event_cursor, ping_cursor, legacy_id, now,
            ),
        )
        conn.execute(
            "UPDATE kanban_notify_authorities SET legacy_subscription_id = ? "
            "WHERE subscription_id = ?",
            (legacy_id, subscription_id),
        )
    return legacy_id


def reconcile_notify_authority_from_legacy(
    conn: sqlite3.Connection, *, subscription_id: str
) -> bool:
    """Catch v2 cursors up from its rollback projection without rewinding."""
    authority = conn.execute(
        "SELECT * FROM kanban_notify_authorities WHERE subscription_id = ?",
        (subscription_id,),
    ).fetchone()
    if authority is None:
        return False
    legacy_id = str(authority["legacy_subscription_id"] or "")
    legacy = conn.execute(
        "SELECT * FROM kanban_notify_subs WHERE subscription_id = ?",
        (legacy_id,),
    ).fetchone() if legacy_id else None
    if legacy is None:
        project_notify_authority_to_legacy(conn, subscription_id=subscription_id)
        return True
    route_key = _v2_route_key(
        authority["task_id"], authority["platform"],
        authority["chat_id"], authority["thread_id"],
    )
    mode = str(authority["delivery_mode"])
    with _kb.write_txn(conn, allow_nested=True):
        if mode in ("notify", "notify+wake"):
            conn.execute(
                "UPDATE kanban_notify_routes SET last_ping_event_id = "
                "MAX(last_ping_event_id, ?), claim_event_id=NULL, claim_token=NULL, "
                "claim_expires_at=NULL " + _V2_ROUTE_WHERE,
                (int(legacy["last_ping_event_id"] or 0), *route_key),
            )
        if mode in ("wake", "notify+wake"):
            conn.execute(
                "UPDATE kanban_notify_authorities SET last_wake_event_id = "
                "MAX(last_wake_event_id, ?), claim_event_id=NULL, claim_token=NULL, "
                "claim_expires_at=NULL WHERE subscription_id = ?",
                (int(legacy["last_event_id"] or 0), subscription_id),
            )
    return True


# --- Configured default notify targets (kanban.default_notify_targets) ---

_DEFAULT_TARGET_REQUIRED = ("board", "platform", "chat_id", "delivery_mode")
_DEFAULT_TARGET_OPTIONAL = (
    "thread_id", "chat_type", "user_id", "user_id_alt",
    "bot_profile", "notifier_profile", "delivery_metadata",
    "ping_priority", "source_kind",
)


def normalize_default_notify_targets(raw: Any) -> list[dict]:
    """Validate/normalize ``kanban.default_notify_targets`` into a list of
    ``add_notify_sub``-shaped target dicts. ``None``/``[]`` -> ``[]`` (historical
    behaviour: no implicit destination). A non-empty invalid value raises
    :class:`ValueError` whose message never contains ``chat_id``/``thread_id``/
    user ids or any other private routing data — only indexes and key names.
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("kanban.default_notify_targets must be a list of target mappings")
    out: list[dict] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise ValueError(f"kanban.default_notify_targets[{idx}] must be a mapping")
        unknown = set(entry) - set(_DEFAULT_TARGET_REQUIRED) - set(_DEFAULT_TARGET_OPTIONAL)
        if unknown:
            raise ValueError(
                f"kanban.default_notify_targets[{idx}] has unknown key(s): "
                f"{', '.join(sorted(unknown))}")
        missing = [k for k in _DEFAULT_TARGET_REQUIRED if not entry.get(k)]
        if missing:
            raise ValueError(
                f"kanban.default_notify_targets[{idx}] missing required key(s): "
                f"{', '.join(missing)}")
        board = _kb._normalize_board_slug(entry["board"])
        platform = str(entry["platform"]).strip().lower()
        chat_id = str(entry["chat_id"]).strip()
        delivery_mode = str(entry["delivery_mode"]).strip()
        if not board or not platform or not chat_id:
            raise ValueError(
                f"kanban.default_notify_targets[{idx}] board/platform/chat_id must be non-empty")
        try:
            from gateway.config import Platform
            platform = Platform(platform).value
        except Exception:
            raise ValueError(
                f"kanban.default_notify_targets[{idx}] has unsupported platform") from None
        # The API server adapter is request/response only: send() deliberately
        # returns failure, so a persistent default target could never notify and
        # would retry forever. Direct ephemeral API wake subscriptions remain
        # supported; only persisted defaults fail closed here.
        if platform == Platform.API_SERVER.value:
            raise ValueError(
                f"kanban.default_notify_targets[{idx}] has unsupported platform")
        if delivery_mode not in _NOTIFY_DELIVERY_MODES:
            raise ValueError(
                f"kanban.default_notify_targets[{idx}] delivery_mode must be one of "
                f"{sorted(_NOTIFY_DELIVERY_MODES)}")
        notifier_profile = entry.get("notifier_profile")
        if notifier_profile is not None:
            notifier_profile = str(notifier_profile).strip() or None
        bot_profile = entry.get("bot_profile")
        if bot_profile is not None:
            bot_profile = str(bot_profile).strip() or None
        if bot_profile and not notifier_profile:
            raise ValueError(
                f"kanban.default_notify_targets[{idx}] notifier_profile is required "
                "with bot_profile")
        if delivery_mode in ("wake", "notify+wake") and not notifier_profile:
            raise ValueError(
                f"kanban.default_notify_targets[{idx}] notifier_profile is required for "
                f"delivery_mode={delivery_mode!r}")
        ping_priority = entry.get("ping_priority", 0)
        if isinstance(ping_priority, bool):
            raise ValueError(
                f"kanban.default_notify_targets[{idx}] ping_priority must be an integer")
        try:
            ping_priority = int(ping_priority)
        except (TypeError, ValueError):
            raise ValueError(
                f"kanban.default_notify_targets[{idx}] ping_priority must be an integer") from None
        source_kind = str(entry.get("source_kind") or "default").strip()
        if source_kind != "default":
            raise ValueError(
                f"kanban.default_notify_targets[{idx}] source_kind must be 'default'")
        thread_id = entry.get("thread_id")
        if thread_id is not None:
            thread_id = str(thread_id).strip() or None
        chat_type = entry.get("chat_type")
        if chat_type is not None:
            chat_type = str(chat_type).strip() or None
        user_id = entry.get("user_id")
        if user_id is not None:
            user_id = str(user_id).strip() or None
        user_id_alt = entry.get("user_id_alt")
        if user_id_alt is not None:
            user_id_alt = str(user_id_alt).strip() or None
        delivery_metadata = entry.get("delivery_metadata")
        if delivery_metadata is not None and not isinstance(delivery_metadata, Mapping):
            raise ValueError(
                f"kanban.default_notify_targets[{idx}] delivery_metadata must be a mapping")
        out.append({
            "board": board,
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "chat_type": chat_type,
            "user_id": user_id,
            "user_id_alt": user_id_alt,
            "bot_profile": bot_profile,
            "notifier_profile": notifier_profile,
            "delivery_mode": delivery_mode,
            "delivery_metadata": dict(delivery_metadata) if delivery_metadata else None,
            "ping_priority": ping_priority,
            "source_kind": source_kind,
        })
    return out


def apply_default_notify_targets(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    board: str,
    targets: list[dict],
) -> int:
    """Apply configured defaults after inherited authorities, atomically.

    Targets carrying ``bot_profile`` use the v2 route/authority model. Historical
    configurations without that field retain the legacy row shape until an
    explicit migration maps the owning credential.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS cursor FROM task_events WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    cursor = int(row["cursor"] if row is not None else 0)
    inserted = 0
    for target in targets:
        if target["board"] != board:
            continue
        if target.get("bot_profile"):
            authority_key = (
                task_id, target["platform"], target["chat_id"],
                target["thread_id"] or "", target["bot_profile"],
                target["notifier_profile"],
            )
            existed = conn.execute(
                "SELECT 1 FROM kanban_notify_authorities " + _V2_AUTHORITY_WHERE,
                authority_key,
            ).fetchone()
            if existed is not None:
                continue
            add_notify_authority(
                conn,
                task_id=task_id,
                platform=target["platform"],
                chat_id=target["chat_id"],
                thread_id=target["thread_id"],
                user_id=target["user_id"],
                user_id_alt=target["user_id_alt"],
                chat_type=target["chat_type"],
                bot_profile=target["bot_profile"],
                notifier_profile=target["notifier_profile"],
                delivery_mode=target["delivery_mode"],
                delivery_metadata=target["delivery_metadata"],
                ping_priority=target["ping_priority"],
                source_kind=target["source_kind"],
                start_ping_cursor=cursor,
                start_wake_cursor=cursor,
            )
            inserted += int(existed is None)
            continue
        key = _sub_key(task_id, target["platform"], target["chat_id"], target["thread_id"])
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO kanban_notify_subs
                (task_id, platform, chat_id, thread_id, user_id, user_id_alt,
                 chat_type, notifier_profile, delivery_mode, delivery_metadata,
                 subscription_id, created_at, last_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                *key,
                target["user_id"],
                target["user_id_alt"],
                target["chat_type"] or "dm",
                target["notifier_profile"],
                target["delivery_mode"],
                _encode_notify_delivery_metadata(target["delivery_metadata"]),
                uuid.uuid4().hex,
                int(time.time()),
                cursor,
            ),
        )
        inserted += int(cur.rowcount or 0)
    return inserted


def _notify_profile_filter(
    notifier_profiles: Optional[Iterable[str]],
    *,
    include_unowned: bool,
) -> tuple[str, list[str]]:
    """Build an optional SQL predicate for notification profile ownership."""
    if notifier_profiles is None:
        return "", []

    profiles = sorted({str(p).strip() for p in notifier_profiles if str(p).strip()})
    clauses: list[str] = []
    params: list[str] = []
    if profiles:
        clauses.append("notifier_profile IN (" + ",".join("?" for _ in profiles) + ")")
        params.extend(profiles)
    if include_unowned:
        clauses.append("notifier_profile IS NULL OR notifier_profile = ''")
    if not clauses:
        return "0", []
    return "(" + ") OR (".join(clauses) + ")", params


def list_notify_subs(
    conn: sqlite3.Connection,
    task_id: Optional[str] = None,
    *,
    notifier_profiles: Optional[Iterable[str]] = None,
    include_unowned: bool = False,
    include_linked: bool = False,
) -> list[dict]:
    """List subscriptions, optionally restricted to notifier profile owners.

    No ``notifier_profiles`` -> all subscriptions. Gateway notifiers pass the
    profiles they own so they cannot claim another gateway's events;
    ``include_unowned`` (dispatch owner) covers legacy rows without a stamp.
    """
    owner_where, owner_params = _notify_profile_filter(
        notifier_profiles, include_unowned=include_unowned,
    )
    where: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        where.append("task_id = ?")
        params.append(task_id)
    if owner_where:
        where.append(owner_where)
        params.extend(owner_params)
    if not include_linked:
        where.append(
            "NOT EXISTS (SELECT 1 FROM kanban_notify_authorities a "
            "WHERE a.legacy_subscription_id = kanban_notify_subs.subscription_id)"
        )
    sql = "SELECT * FROM kanban_notify_subs"
    if where:
        sql += " WHERE " + " AND ".join(f"({clause})" for clause in where)
    out: list[dict] = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        if "delivery_metadata" in item:
            item["delivery_metadata"] = _decode_notify_delivery_metadata(item.get("delivery_metadata"))
        out.append(item)
    return out


def count_notify_authorities(
    db_path: Optional[Path] = None, *, board: Optional[str] = None
) -> int:
    """Cheap read-only v2-authority probe used before the notifier write open."""
    path = db_path if db_path is not None else _kb.kanban_db_path(board=board)
    if not path.exists():
        return 0
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM kanban_notify_authorities"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise
        return int(row[0]) if row else 0
    finally:
        conn.close()


def count_notify_subs(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
    notifier_profiles: Optional[Iterable[str]] = None,
    include_unowned: bool = False,
    platform: Optional[str] = None,
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> int:
    """Count ``kanban_notify_subs`` rows via a read-only connection — the
    notifier's cheap zero-subscription early exit. Unlike :func:`connect` it
    never creates the file, runs init/migration or opens writable; WAL rows are
    still visible so a fresh sub is never missed. Missing DB / missing table
    counts as zero; platform matches case-insensitively (as notifier routing),
    chat/thread exactly. Raises :class:`sqlite3.Error` if the DB exists but is
    unreadable — callers pick their own fallback.
    """
    path = db_path if db_path is not None else _kb.kanban_db_path(board=board)
    if not path.exists():
        return 0
    owner_where, owner_params = _notify_profile_filter(
        notifier_profiles, include_unowned=include_unowned,
    )
    clauses: list[str] = []
    params: list[Any] = []
    if owner_where:
        clauses.append(f"({owner_where})")
        params.extend(owner_params)
    for clause, value in (
        ("LOWER(platform) = LOWER(?)", platform),
        ("chat_id = ?", chat_id),
        ("thread_id = ?", thread_id),
    ):
        if value is not None:
            clauses.append(clause)
            params.append(value)
    query = "SELECT COUNT(*) FROM kanban_notify_subs"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        try:
            row = conn.execute(query, params).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise
        return int(row[0]) if row else 0
    finally:
        conn.close()


def notify_subscription_is_current(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
) -> bool:
    """Whether the durable row still represents the caller's claimed lease."""
    guard, guard_params = _lease_guard(subscription_id)
    row = conn.execute(
        "SELECT 1 FROM kanban_notify_subs " + _SUB_KEY_WHERE + guard,
        (*_sub_key(task_id, platform, chat_id, thread_id), *guard_params),
    ).fetchone()
    return row is not None


def remove_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
) -> bool:
    guard, guard_params = _lease_guard(subscription_id)
    with _kb.write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs " + _SUB_KEY_WHERE + guard,
            (*_sub_key(task_id, platform, chat_id, thread_id), *guard_params),
        )
    return cur.rowcount > 0


def purge_stale_done_notify_subs(conn: sqlite3.Connection, *, max_age_days: int = 30) -> int:
    """Delete notify subs whose task sat in ``done``/``blocked`` untouched for
    longer than ``max_age_days`` (``<= 0`` disables); returns rows deleted.

    Subs survive ``done`` because a reopened task must still notify its origin,
    which accumulates forever on never-archiving boards. ``blocked`` is
    abandoned (unlike ``backlog``/``ready``) so it reaps on the same clock. Age
    = latest event, else ``completed_at``, else ``created_at`` — any activity,
    including a reopen, exempts the sub.

    The notifier keeps subscriptions alive through ``done`` because a completed task can be reopened (review
    corrections, continuation) and the reopened cycle must still notify its origin session. On boards that
    never archive, that retention would otherwise accumulate subscription rows forever — each one scanned
    every notifier tick. This GC bounds that: a task that has been ``done`` with no new events for the
    retention window is treated as settled and its subscriptions are purged. ``blocked`` tasks
    (circuit-breaker trips, dead workers) are reaped on the same clock — they are abandoned, not idle,
    unlike a ``backlog``/``ready`` card that is merely waiting for pickup (#100955).
    """
    try:
        days = int(max_age_days)
    except (TypeError, ValueError):
        days = 30
    if days <= 0:
        return 0
    cutoff = int(time.time()) - days * 86400
    stale_rows = conn.execute(
        "SELECT t.id FROM tasks t WHERE t.status IN ('done', 'blocked') "
        "AND COALESCE((SELECT MAX(e.created_at) FROM task_events e "
        "WHERE e.task_id = t.id), t.completed_at, t.created_at, 0) < ?",
        (cutoff,),
    ).fetchall()
    task_ids = [str(row["id"]) for row in stale_rows]
    if not task_ids:
        return 0
    marks = ",".join("?" for _ in task_ids)
    with _kb.write_txn(conn):
        projection_rows = conn.execute(
            "SELECT legacy_subscription_id FROM kanban_notify_authorities "
            f"WHERE task_id IN ({marks}) AND legacy_subscription_id IS NOT NULL",
            task_ids,
        ).fetchall()
        projection_ids = [str(row["legacy_subscription_id"]) for row in projection_rows]
        if projection_ids:
            projection_marks = ",".join("?" for _ in projection_ids)
            conn.execute(
                f"DELETE FROM kanban_notify_subs WHERE subscription_id IN ({projection_marks})",
                projection_ids,
            )
        legacy_cur = conn.execute(
            f"DELETE FROM kanban_notify_subs WHERE task_id IN ({marks})",
            task_ids,
        )
        authority_cur = conn.execute(
            f"DELETE FROM kanban_notify_authorities WHERE task_id IN ({marks})",
            task_ids,
        )
        conn.execute(
            "DELETE FROM kanban_notify_routes WHERE NOT EXISTS ("
            " SELECT 1 FROM kanban_notify_authorities a"
            " WHERE a.task_id = kanban_notify_routes.task_id"
            " AND a.platform = kanban_notify_routes.platform"
            " AND a.chat_id = kanban_notify_routes.chat_id"
            " AND a.thread_id = kanban_notify_routes.thread_id)"
        )
    return int(legacy_cur.rowcount or 0) + int(authority_cur.rowcount or 0)


def _notify_cursor(
    conn: sqlite3.Connection, task_id: str, platform: str, chat_id: str,
    thread_id: Optional[str], subscription_id: Optional[str] = None,
) -> Optional[int]:
    """``last_event_id`` of one current subscription lease, or ``None``."""
    guard, guard_params = _lease_guard(subscription_id)
    row = conn.execute(
        "SELECT last_event_id FROM kanban_notify_subs " + _SUB_KEY_WHERE + guard,
        (*_sub_key(task_id, platform, chat_id, thread_id), *guard_params),
    ).fetchone()
    return None if row is None else int(row["last_event_id"])


def unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
    subscription_id: Optional[str] = None,
) -> tuple[int, list[Event]]:
    """Return ``(new_cursor, events)`` with ``id > last_event_id``. The cursor
    is NOT advanced here; call :func:`advance_notify_cursor` after delivery.
    """
    cursor = _notify_cursor(
        conn, task_id, platform, chat_id, thread_id, subscription_id,
    )
    if cursor is None:
        return 0, []
    kind_list = list(kinds) if kinds else None
    q = (
        "SELECT * FROM task_events WHERE task_id = ? AND id > ? "
        + ("AND kind IN (" + ",".join("?" * len(kind_list)) + ") " if kind_list else "")
        + "ORDER BY id ASC"
    )
    params: list[Any] = [task_id, cursor]
    if kind_list:
        params.extend(kind_list)
    rows = conn.execute(q, params).fetchall()
    out = [_kb.Event.from_row(r) for r in rows]
    max_id = max([cursor, *(int(r["id"]) for r in rows)])
    return max_id, out


def claim_unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
    subscription_id: Optional[str] = None,
) -> tuple[int, int, list[Event]]:
    """Atomically claim unseen events for one subscription.

    Returns ``(old_cursor, new_cursor, events)``; when events are returned the
    row's ``last_event_id`` has already been advanced inside ``BEGIN IMMEDIATE``,
    so concurrent gateway watchers on the same board DB serialize on SQLite's
    writer lock and only the first claims a given event range. Callers send the
    events, then leave the cursor or call :func:`rewind_notify_cursor` on
    delivery failure.
    """
    with _kb.write_txn(conn):
        old_cursor = _notify_cursor(
            conn, task_id, platform, chat_id, thread_id, subscription_id,
        )
        if old_cursor is None:
            return 0, 0, []
        new_cursor, events = unseen_events_for_sub(
            conn, task_id=task_id, platform=platform, chat_id=chat_id,
            thread_id=thread_id, kinds=kinds, subscription_id=subscription_id,
        )
        if not events:
            return old_cursor, old_cursor, []
        _cas_cursor(
            conn, _sub_key(task_id, platform, chat_id, thread_id),
            new_cursor, old_cursor, subscription_id,
        )
        return old_cursor, new_cursor, events


def _cas_cursor(
    conn: sqlite3.Connection, key: tuple, new_cursor: int, expected: int,
    subscription_id: Optional[str] = None,
) -> sqlite3.Cursor:
    """Move ``last_event_id`` only for the same cursor and durable row lease."""
    guard, guard_params = _lease_guard(subscription_id)
    return conn.execute(
        "UPDATE kanban_notify_subs SET last_event_id = ? "
        + _SUB_KEY_WHERE + " AND last_event_id = ?" + guard,
        (int(new_cursor), *key, int(expected), *guard_params),
    )


def advance_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    new_cursor: int,
    subscription_id: Optional[str] = None,
) -> None:
    guard, guard_params = _lease_guard(subscription_id)
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = MAX(last_event_id, ?) "
            + _SUB_KEY_WHERE + guard,
            (
                int(new_cursor), *_sub_key(task_id, platform, chat_id, thread_id),
                *guard_params,
            ),
        )


def record_notify_ping(
    conn: sqlite3.Connection, *, task_id: str, platform: str, chat_id: str,
    thread_id: Optional[str] = None, event_id: int,
    subscription_id: Optional[str] = None,
) -> None:
    """Checkpoint a sent ping independently of the retryable wake cursor."""
    guard, guard_params = _lease_guard(subscription_id)
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_ping_event_id = MAX(last_ping_event_id, ?) "
            + _SUB_KEY_WHERE + guard,
            (
                int(event_id), *_sub_key(task_id, platform, chat_id, thread_id),
                *guard_params,
            ),
        )


def rewind_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    claimed_cursor: int,
    old_cursor: int,
    subscription_id: Optional[str] = None,
) -> bool:
    """Undo a claim when delivery fails. The CAS guard only rewinds if no later
    notifier advanced the row, so retries never clobber newer progress.
    """
    with _kb.write_txn(conn):
        cur = _cas_cursor(
            conn, _sub_key(task_id, platform, chat_id, thread_id),
            old_cursor, claimed_cursor, subscription_id,
        )
    return cur.rowcount > 0


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb  # noqa: E402
