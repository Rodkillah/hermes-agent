"""Kanban notifier: claim terminal task events per subscription and deliver them.

``GatewayKanbanWatchersMixin._kanban_notifier_watcher`` owns the loop and
the GC cadence; the per-tick claim (``_notifier_collect``) and the
per-subscription delivery (``_KanbanNotification``) live here.
"""

from __future__ import annotations

import contextlib
import re
from functools import partial
from pathlib import Path
import weakref
from typing import Any, Callable, Optional

from agent.i18n import t

from gateway.kanban_watchers_common import _list_boards, _to_thread_process_service, logger


def _kbc():
    from hermes_cli import kanban_db_connect
    return kanban_db_connect


def _kbn():
    from hermes_cli import kanban_db_notify
    return kanban_db_notify

# "status" covers dashboard drag-drop and `_set_status_direct()`.
# ``review_requested`` wakes the origin like a block but is not one;
# the task is not archived so later review cycles keep notifying.
TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out", "status", "archived", "unblocked", "block_loop_detected", "block_loop_resolved", "review_requested", "changes_requested", "production_promoted")
# Kinds that hand a decision back to the origin, which must take a turn.
# status/archived/unblocked are bookkeeping.
_WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked", "review_requested", "changes_requested", "block_loop_detected")


def diagnostic_event(ev) -> bool:
    """Infrastructure attention is distinct from an explicit owner decision."""
    if ev.kind in {"crashed", "timed_out", "gave_up"}:
        return True
    if ev.kind in {"blocked", "block_loop_detected"}:
        return (ev.payload or {}).get("kind") != "needs_input"
    return ev.kind == "status" and (ev.payload or {}).get("status") in {"blocked", "triage"}
# Consecutive send failures (adapter raised OR reported SendResult(success=False))
# before a sub is dropped as a dead chat. 12 ≈ 60s at the 5s cadence: a transient
# API outage must not permanently unsubscribe a live review-gate channel.
# Subscriptions are removed only when the task reaches the irreversible archived status. ``done`` is
# reversible in review/controller flows, so removing its subscription would silence a later reopen. We used
# to also unsub on any terminal event kind (gave_up / crashed / timed_out / blocked), but that silently
# dropped the user out of the loop whenever the dispatcher respawned the task: a worker that crashes, gets
# reclaimed, runs again, and crashes a second time would only notify on the first crash because the
# subscription was deleted after the first event. Same shape as the reblock-after-unblock cycle that PR
# #22941 fixed for `blocked`. Keeping the subscription alive until the task is archived lets the cursor
# (advanced atomically by claim_unseen_events_for_sub) handle dedup, and any retry-loop event reaches the
# user. Per-subscription send-failure counter. Adapter.send raising means the chat is dead (deleted, bot
# kicked, etc.) — after N consecutive send failures the sub is dropped so we don't spin against a dead chat
# every 5 seconds forever. A genuinely dead chat still drops, just ~60s later — a fine trade for an
# unattended gate where a false drop means silent work pileup.
MAX_SEND_FAILURES = 12

_LOCAL_PATH_RE = re.compile(r"(?<![\w:/])(?:/(?:Users|home|private|tmp|var|etc|workspace)/[^\s,;]+|" r"[A-Za-z]:\\[^\s,;]+)")


def _safe_review_reason(value: Any, limit: int = 160) -> str:
    """Return a mobile-friendly review reason safe for external delivery."""
    from agent.redact import redact_sensitive_text

    reason = redact_sensitive_text("" if value is None else str(value), force=True, redact_url_credentials=True)
    reason = " ".join(_LOCAL_PATH_RE.sub("[local path]", reason).split())
    if len(reason) > limit:
        reason = reason[: limit - 1].rstrip() + "…"
    return reason


def _wake_scope_id(adapter: Any, sub: dict) -> Optional[str]:
    """Return the tenant scope (Slack workspace) a subscription's wake keys to.

    ``build_session_key()`` includes ``scope_id`` on multi-tenant platforms,
    so the wake must carry the same scope as inbound messages. Persisted
    ``delivery_metadata`` wins (it records the creating scope); the adapter's
    live chat → scope map only covers rows without metadata. ``None`` means
    unscoped, matching an unscoped platform's key.
    """
    delivery_meta = sub.get("delivery_metadata")
    if isinstance(delivery_meta, dict):
        for key in ("scope_id", "guild_id", "slack_team_id", "team_id"):
            value = delivery_meta.get(key)
            if value:
                return str(value)
    resolver = getattr(adapter, "scope_id_for_chat", None)
    if not callable(resolver):
        return None
    try:
        resolved = resolver(str(sub.get("chat_id") or ""))
    except Exception as exc:
        # An adapter-side lookup failure yields no scope, never an error.
        logger.debug("kanban notifier: scope lookup failed (%s)", type(exc).__name__)
        return None
    return str(resolved) if resolved else None


_ANCHORLESS_WARNED: set[tuple] = set()


def _warn_anchorless_thread_sub_once(sub: dict, platform: str) -> None:
    """A thread-shaped subscription without ``parent_chat_id`` cannot match a channel-level
    ``profile_routes`` entry, so the fail-closed route gate skips it on every tick. Say so ONCE per
    row at WARNING — a subscription that can never deliver was invisible below DEBUG (#110919)."""
    metadata = sub.get("delivery_metadata") or {}
    thread_like = bool(sub.get("thread_id")) or (sub.get("chat_type") or metadata.get("chat_type")) in {
        "thread", "forum", "forum_post", "forum-post", "topic"}
    if not thread_like or metadata.get("parent_chat_id"):
        return
    key = (sub.get("task_id"), platform, sub.get("chat_id"), sub.get("thread_id") or "")
    if key in _ANCHORLESS_WARNED:
        return
    _ANCHORLESS_WARNED.add(key)
    logger.warning(
        "kanban notifier: subscription for %s on %s thread %s has no parent_chat_id anchor and matched no "
        "profile route; it will not be delivered. Re-subscribe with `hermes kanban notify-subscribe ... "
        "--parent-chat-id <channel id> [--guild-id <guild id>]`.",
        sub.get("task_id"), platform, sub.get("chat_id"),
    )


def _platform_names(mapping: Any) -> set[str]:
    """Lower-cased platform names of an adapters mapping (Platform enums or strings)."""
    return {getattr(platform, "value", str(platform)).lower() for platform in mapping}


def _resolve_subscription_route(
    runner: Any, platform: Any, sub: dict, owner_profile: Optional[str]
) -> Optional[tuple[Any, Optional[str], Optional[str]]]:
    """Return ``(adapter, runtime profile, adapter profile)`` for one durable row.

    Ownerless rows carry no transport identity. Under multiplex, derive it from
    the canonical profile-route matcher and require exactly one viable bot; a
    missing anchor or competing bot leaves the claim retryable.
    """
    config = getattr(runner, "config", None)
    adapter = runner._authorization_adapter(platform, owner_profile)
    if not getattr(config, "multiplex_profiles", False):
        return (adapter, owner_profile, None) if adapter is not None else None

    primary = runner.adapters.get(platform)
    if owner_profile and adapter is not None and adapter is not primary:
        registered, adapter_profile = runner._owning_profile(adapter, platform)
        return (adapter, owner_profile, adapter_profile) if registered else None

    metadata = sub.get("delivery_metadata") or {}
    guild = metadata.get("scope_id") or metadata.get("guild_id")
    parent = metadata.get("parent_chat_id")
    chat, thread = sub.get("chat_id"), sub.get("thread_id") or None
    thread_like = bool(thread) or (sub.get("chat_type") or metadata.get("chat_type")) in {
        "thread", "forum", "forum_post", "forum-post", "topic",
    }
    # For each receiving bot, only its highest-priority exact (or potentially
    # exact, when the persisted row lacks an anchor) route matters.
    route_states: dict[Optional[str], tuple[str, Any]] = {}
    for route in getattr(config, "profile_routes", None) or []:
        bot_profile = route.bot_profile or None
        if bot_profile in route_states:
            continue
        args = dict(
            platform=platform.value, guild_id=guild, chat_id=chat,
            thread_id=thread, parent_chat_id=parent, adapter_profile=bot_profile,
        )
        if route.matches(**args):
            route_states[bot_profile] = ("exact", route)
            continue
        args["guild_id"] = guild or route.guild_id
        args["parent_chat_id"] = parent or (route.chat_id if thread_like else None)
        if route.matches(**args):
            route_states[bot_profile] = ("uncertain", route)

    if route_states:
        applicable = [
            (bot_profile, state, route)
            for bot_profile, (state, route) in route_states.items()
            if not owner_profile or route.profile == owner_profile
        ]
        if len(applicable) != 1 or applicable[0][1] != "exact":
            return None
        bot_profile, _state, route = applicable[0]
        try:
            from gateway.run import _multiplex_profile_homes
            served = {name for name, _home in _multiplex_profile_homes(config)}
        except Exception:
            return None
        if route.profile not in served or (bot_profile and bot_profile not in served):
            return None
        # Shared-bot routes resolve through the runtime profile so
        # _is_shared_bot_satellite enforces credential-boundary failures.
        transport_profile = bot_profile or route.profile
        adapter = runner._authorization_adapter(platform, transport_profile)
        if adapter is None:
            return None
        registered, actual_profile = runner._owning_profile(adapter, platform)
        actual_profile = actual_profile if actual_profile not in ("", "default") else None
        if not registered or actual_profile != bot_profile:
            return None
        return adapter, route.profile, bot_profile

    # No profile route covers this destination: preserve the legacy owner
    # subscription, or the primary transport for an ownerless singleton row.
    adapter = runner._authorization_adapter(platform, owner_profile)
    if adapter is None:
        return None
    if owner_profile and adapter is primary:
        primary_profile = (
            getattr(runner, "_primary_profile_name", None)
            or runner._active_profile_name()
        )
        if owner_profile not in ("default", primary_profile):
            return None
    registered, adapter_profile = runner._owning_profile(adapter, platform)
    if not registered:
        return None
    return adapter, owner_profile, adapter_profile


def _resolve_v2_authority(
    runner: Any, platform: Any, sub: dict
) -> Optional[tuple[Any, str, str]]:
    """Resolve only the credential and runtime named by a v2 authority."""
    bot_profile = str(sub.get("bot_profile") or "").strip()
    runtime_profile = str(sub.get("notifier_profile") or "").strip()
    if not bot_profile or not runtime_profile:
        return None
    adapter = runner._authorization_adapter(platform, bot_profile)
    if adapter is None:
        return None
    registered, actual_profile = runner._owning_profile(adapter, platform)
    primary_profile = (
        getattr(runner, "_primary_profile_name", None)
        or runner._active_profile_name()
        or "default"
    )
    normalized_actual = (
        primary_profile if actual_profile in (None, "", "default")
        else str(actual_profile)
    )
    normalized_bot = primary_profile if bot_profile == "default" else bot_profile
    if not registered or normalized_actual != normalized_bot:
        return None
    return adapter, runtime_profile, bot_profile


# --- Collection (runs in a worker thread) ---


class _Collector:
    """One tick's claim state: which profiles/platforms this gateway serves and the GC gate."""

    def __init__(self, runner: Any, kb: Any, *, notifier_profile: Optional[str], gc_due: bool, gc_retention_days: int) -> None:
        self.runner = runner
        self.kb = kb
        self.notifier_profile = notifier_profile
        self.gc_due = gc_due
        self.gc_retention_days = gc_retention_days
        self.deliveries: list[dict] = []
        config = getattr(runner, "config", None)
        self.multiplex_profiles = bool(getattr(config, "multiplex_profiles", False))
        # The singleton owner remains the legacy fallback. A multiplex gateway
        # may also inspect ownerless rows because exact persisted route anchors
        # select the authorized profile adapter before the atomic event claim.
        self.include_unowned = runner._owns_kanban_dispatcher_lock() or self.multiplex_profiles
        self.profile_adapters = getattr(runner, "_profile_adapters", {})
        self.notifier_profiles = {
            str(notifier_profile).strip()
        } if notifier_profile and str(notifier_profile).strip() else set()
        self.notifier_profiles.update(str(p).strip() for p in self.profile_adapters if str(p).strip())
        if self.multiplex_profiles:
            self.notifier_profiles.update(
                route.profile for route in config.profile_routes
                if route.enabled and route.platform in _platform_names(runner.adapters)
            )
        # Include every platform any secondary profile has live. This is only a
        # coarse pre-filter; exact destination authorization runs before claim
        # and again at delivery, rewinding if the route or adapter changed.
        self.active_platforms = _platform_names(runner.adapters).union(
            *(_platform_names(m) for m in self.profile_adapters.values()))

    def collect(self) -> list[dict]:
        if not self.active_platforms:
            logger.debug("kanban notifier: no connected adapters; skipping tick")
            return self.deliveries
        # Poll each resolved DB path once: several slugs can map to one DB when
        # HERMES_KANBAN_DB pins the board path.
        kb = self.kb
        seen_db_paths: set[str] = set()
        for board_meta in _list_boards(kb):
            slug = board_meta.get("slug") or kb.DEFAULT_BOARD
            db_path = board_meta.get("db_path")
            try:
                resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(kb.kanban_db_path(slug).resolve())
            except Exception:
                resolved_db_path = f"slug:{slug}"
            if resolved_db_path in seen_db_paths:
                logger.debug("kanban notifier: skipping duplicate board slug %s for DB %s", slug, resolved_db_path)
                continue
            seen_db_paths.add(resolved_db_path)
            self.collect_board(slug)
        return self.deliveries

    def _board_has_subs(self, slug: str) -> bool:
        """Cheap read-only probe before the writable connect() (schema init, WAL
        sidecars, checkpoints); a probe failure falls back to the writable open."""
        try:
            count = _kbn().count_notify_subs(
                board=slug, notifier_profiles=self.notifier_profiles,
                include_unowned=self.include_unowned,
            ) + _kbn().count_notify_authorities(board=slug)
        except Exception as exc:
            logger.debug("kanban notifier: read-only subscription probe failed "
                         "for board %s (%s); falling back to writable open", slug, exc)
            return True
        if count == 0:
            logger.debug("kanban notifier: board %s has no subscriptions owned by %s; skipping open",
                         slug, sorted(self.notifier_profiles))
        return count != 0

    def _gc_stale_subs(self, conn: Any, slug: str) -> None:
        """Best-effort stale-sub sweep: a failed sweep never blocks delivery; the next hourly gate retries."""
        try:
            _purged = _kbn().purge_stale_done_notify_subs(conn, max_age_days=self.gc_retention_days)
            if _purged:
                logger.info("kanban notifier: purged %d stale done/blocked-task subscription(s) on board %s (retention %dd)",
                            _purged, slug, self.gc_retention_days)
        except Exception as _gc_exc:
            logger.debug("kanban notifier: stale-sub GC failed for board %s: %s", slug, _gc_exc)

    def _claim_for_sub(self, conn: Any, slug: str, sub: dict) -> Optional[dict]:
        """Claim one subscription's unseen events; None when skipped or nothing new."""
        # Every row loaded through connect() is migrated to a durable lease.
        # Refuse a hand-written post-migration row that omitted it rather than
        # claiming without rebind protection.
        if not sub.get("subscription_id"):
            return None
        owner_profile = sub.get("notifier_profile") or None
        platform = (sub.get("platform") or "").lower()
        if platform not in self.active_platforms:
            logger.debug("kanban notifier: subscription for %s on %s skipped; adapter not connected",
                         sub.get("task_id"), platform or "<missing>")
            return None
        from gateway.config import Platform
        # The durable row is authoritative: resolve through the canonical route
        # matcher, which fails closed when a thread-shaped row carries no anchor
        # and no profile route covers it.
        resolved = _resolve_subscription_route(
            self.runner, Platform(platform), sub, owner_profile
        )
        if resolved is None:
            _warn_anchorless_thread_sub_once(sub, platform)
            return None
        _adapter, runtime_profile, adapter_profile = resolved
        old_cursor, cursor, events = _kbn().claim_unseen_events_for_sub(
            conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
            thread_id=sub.get("thread_id") or "", kinds=TERMINAL_KINDS,
            subscription_id=sub.get("subscription_id") or None,
        )
        if not events:
            return None
        task = self.kb.get_task(conn, sub["task_id"])
        logger.debug("kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s",
                     len(events), sub["task_id"], slug, old_cursor, cursor)
        return {
            "sub": sub, "old_cursor": old_cursor, "cursor": cursor,
            "events": events, "task": task, "board": slug,
            "resolved_profile": runtime_profile,
            "resolved_adapter_profile": adapter_profile,
        }

    def _claim_v2(self, conn: Any, slug: str, sub: dict) -> Optional[dict]:
        platform_name = str(sub.get("platform") or "").lower()
        flow = sub["flow"]
        if platform_name not in self.active_platforms:
            return None
        # Ping ownership follows the credential-bearing bot. The wake runtime is
        # an independent flow and may be served by another gateway process.
        if flow == "wake" and sub.get("notifier_profile") not in self.notifier_profiles:
            return None
        from gateway.config import Platform
        try:
            platform = Platform(platform_name)
        except ValueError:
            return None
        resolved = _resolve_v2_authority(self.runner, platform, sub)
        if resolved is None:
            return None
        _adapter, runtime_profile, adapter_profile = resolved
        if flow == "ping":
            claim = _kbn().claim_notify_ping(
                conn,
                route_id=sub["route_id"],
                event_kinds=TERMINAL_KINDS,
            )
            old_cursor_key = "last_ping_event_id"
        else:
            claim = _kbn().claim_notify_wake(
                conn,
                subscription_id=sub["subscription_id"],
                event_kinds=TERMINAL_KINDS,
            )
            old_cursor_key = "last_wake_event_id"
        if claim is None:
            return None
        event = self.kb.Event(**claim["event"])
        task = self.kb.get_task(conn, claim["task_id"])
        return {
            "sub": claim,
            "old_cursor": int(claim.get(old_cursor_key) or 0),
            "cursor": event.id,
            "events": [event],
            "task": task,
            "board": slug,
            "resolved_profile": runtime_profile,
            "resolved_adapter_profile": adapter_profile,
        }

    def _collect_v2(self, conn: Any, slug: str) -> None:
        authorities = _kbn().list_notify_authorities(conn)
        by_subscription = {
            authority["subscription_id"]: authority for authority in authorities
        }
        candidates: list[dict] = []
        for route in _kbn().list_notify_routes(conn):
            authority = by_subscription.get(route.get("ping_subscription_id"))
            if authority is not None:
                candidates.append({**authority, **route, "flow": "ping"})
        candidates.extend(
            {**authority, "flow": "wake"}
            for authority in authorities
            if authority["delivery_mode"] in ("wake", "notify+wake")
        )
        candidates.sort(key=lambda item: (
            0 if item["flow"] == "ping" else 1,
            str(item.get("notifier_profile") or ""),
            str(item.get("subscription_id") or ""),
        ))
        for candidate in candidates:
            try:
                claimed = self._claim_v2(conn, slug, candidate)
                if claimed is not None:
                    self.deliveries.append(claimed)
            except Exception as exc:
                logger.warning(
                    "kanban notifier: v2 %s claim for task %s on board %s failed: %s",
                    candidate["flow"], candidate.get("task_id"), slug, exc,
                )

    def collect_board(self, slug: str) -> None:
        """Claim events on one board, appending delivery dicts to ``deliveries``."""
        if not self._board_has_subs(slug):
            return
        kb = self.kb
        try:
            conn = _kbc().connect(board=slug)
        except Exception as exc:
            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
            return
        try:
            if self.gc_due:
                self._gc_stale_subs(conn, slug)
            # No explicit init_db(): connect() already runs the migration once per
            # process, and init_db() would re-run it on a second connection racing
            # the first.
            self._collect_v2(conn, slug)
            subs = _kbn().list_notify_subs(conn, notifier_profiles=self.notifier_profiles, include_unowned=self.include_unowned)
            if not subs:
                logger.debug("kanban notifier: board %s has no subscriptions", slug)
            for sub in subs:
                try:
                    claimed = self._claim_for_sub(conn, slug, sub)
                    if claimed is not None:
                        self.deliveries.append(claimed)
                except Exception as sub_exc:
                    # One bad subscription must not block the rest of the tick.
                    logger.warning("kanban notifier: subscription for %s on board %s failed: %s",
                                   sub.get("task_id"), slug, sub_exc)
        finally:
            conn.close()


def _notifier_collect(runner: Any, kb: Any, *, notifier_profile: Optional[str], gc_due: bool, gc_retention_days: int) -> list[dict]:
    """Claim unseen terminal events for every owned subscription on every board.

    Each gateway polls subscriptions owned by profiles whose adapters it hosts.
    Legacy rows without a profile stamp are visible to the singleton dispatcher
    owner and to multiplex gateways, where exact persisted route anchors must
    authorize the adapter before the atomic claim.
    """
    return _Collector(
        runner, kb, notifier_profile=notifier_profile, gc_due=gc_due, gc_retention_days=gc_retention_days,
    ).collect()


# --- Per-event message formatting: kind -> (msg, wake_handoff, wake_review_detail) ---
# ``None`` for handoff / review_detail leaves the accumulated wake value untouched.


def _payload(ev: Any, key: str) -> Any:
    """Shared "payload present and truthy" read."""
    return ev.payload.get(key) if ev.payload and ev.payload.get(key) else None


def _clip(ev: Any, key: str, fmt: str, limit: int) -> str:
    """``fmt`` applied to the truncated payload value, or ``""`` when absent."""
    value = _payload(ev, key)
    return fmt.format(str(value)[:limit]) if value else ""


_NL = "\n{}"


def _first_line(text: str, limit: int) -> str:
    lines = text.strip().splitlines()
    return lines[0][:limit] if lines else text[:limit]


def _fmt_completed(ev, n) -> tuple:
    # Prefer the run summary from the event payload; fall back to task.result for legacy rows.
    wake_handoff = None
    payload_summary = _payload(ev, "summary")
    if payload_summary:
        wake_handoff = _first_line(str(payload_summary), 200)
    elif n.task and n.task.result:
        wake_handoff = _first_line(n.task.result, 160)
    handoff = f"\n{wake_handoff}" if wake_handoff is not None else ""
    return f"✔ {n.head} done — {n.title}{handoff}", wake_handoff, None


def _fmt_review_requested(ev, n) -> tuple:
    # Implementation done; task moved to the review lane. Carry the handoff
    # into the wake turn like ``completed`` so the reviewer needn't re-read the board.
    handoff = ""
    wake_handoff = None
    summary = _payload(ev, "summary")
    if summary:
        summary = str(summary)
        handoff = f"\n{summary[:200]}"
        wake_handoff = _first_line(summary, 200)
    return f"👀 {n.head} ready for review — {n.title}{handoff}", wake_handoff, None


def _fmt_changes_requested(ev, n) -> tuple:
    payload = ev.payload or {}
    reason = _safe_review_reason(payload.get("reason"))
    reviewer = _safe_review_reason(payload.get("reviewer"), 48)
    implementer = _safe_review_reason(payload.get("implementer"), 48)
    reason_text = reason or "reviewer feedback requires changes"
    provenance = f" — reviewer @{reviewer}" if reviewer else ""
    if implementer:
        provenance += f" → implementer @{implementer}"
    msg = f"🛑 {n.board_tag}Kanban {n.task_id} review requested changes/BLOCK: {reason_text}{provenance}"
    return msg, None, reason_text


def _fmt_block_loop_detected(ev, n) -> tuple:
    """Re-blocked for the same cause past the limit and routed to `triage`.

    It emits no blocked/status event, so ping loudly here. A repeated-block
    circuit breaker establishes that orchestration attention is needed; it
    does NOT establish that a human decision or owner input exists. Use
    neutral orchestration wording unless the block was typed as a genuine
    owner-input request (`needs_input`, the only kind that carries a concrete
    question for the owner).
    """
    kind = _payload(ev, "kind")
    decision = kind == "needs_input"
    msg = (
        f"🛑 {n.head} routed to TRIAGE — "
        f"{'needs a human decision' if decision else 'for orchestration attention'}"
        f"{_clip(ev, 'recurrences', ' (blocked {}x for the same cause)', 200)}{_clip(ev, 'reason', ': {}', 160)}"
    )
    return msg, None, None


def _fmt_gave_up(ev, n) -> tuple:
    # The dispatcher auto-blocked the task after ``failures`` consecutive non-success attempts
    # (spawn failure, crash, or timeout alike): it is now Blocked and waiting for a human.
    failures = _payload(ev, "failures")
    count = f"it failed {int(failures)} times in a row" if failures else "it kept failing"
    last = _clip(ev, "error", " (last: {})", 160)
    return (
        f"⛔ {n.head} gave up after repeated failures: it is now blocked. {count}{last}. "
        f"Fix the cause, then `hermes kanban unblock "
        f"{n.task_id}` (or `hermes kanban reassign {n.task_id}`). Logs: `hermes kanban log {n.task_id}`.",
        None, None,
    )


def _fmt_timed_out(ev, n) -> tuple:
    limit = int(_payload(ev, "limit_seconds") or 0)
    minutes = max(1, round(limit / 60)) if limit else 0
    span = f"its {minutes}-minute limit" if minutes else "its time limit"
    return (f"⏱ {n.head} timed out — it ran past {span} and was stopped; "
            f"it will be retried automatically.", None, None)


# archived / unblocked are claimed (so the cursor advances past them) but
# intentionally silent (no formatter), and excluded from _WAKE_KINDS so they
# never wake the creator.
_EVENT_FORMATTERS: dict[str, Callable[[Any, "_KanbanNotification"], tuple]] = {
    "completed": _fmt_completed,
    "blocked": lambda ev, n: (f"⏸ {n.head} blocked{_clip(ev, 'reason', ': {}', 160)}", None, None),
    "gave_up": _fmt_gave_up,
    "crashed": lambda ev, n: (
        f"✖ {n.head} worker crashed — its process stopped unexpectedly; "
        f"it will be retried automatically.", None, None,
    ),
    "timed_out": _fmt_timed_out,
    "status": lambda ev, n: (f"🔄 {n.head} → {_payload(ev, 'status') or ''}", None, None),
    "review_requested": _fmt_review_requested,
    "changes_requested": _fmt_changes_requested,
    "block_loop_detected": _fmt_block_loop_detected,
    # The human decision that closed a block loop: say which way it went.
    "block_loop_resolved": lambda ev, n: (
        f"✅ {n.head} block loop resolved — {_payload(ev, 'decision') or 'resolved'}"
        f"{_clip(ev, 'reason', ': {}', 160)}",
        None, None,
    ),
    # Production promotion is distinct from work completion. Keep this
    # post-commit notification limited to the non-sensitive identity fields;
    # receipt, backup, rollback and probe evidence stay in the database.
    "production_promoted": lambda ev, n: (
        f"🚀 {n.head} production verified"
        f"{_clip(ev, 'target', ' — {}', 80)}{_clip(ev, 'deployed_at_utc', ' @ {}', 40)}"
        f"{_clip(ev, 'deployed_identity_value', ' [{}]', 40)}",
        None, None,
    ),
}


# --- Delivery of one claimed batch (one subscription, N events) ---


def _v2_claim_op(
    board_slug: Optional[str], operation: str, sub: dict, event_id: int
) -> bool:
    with _kbc().connect(board=board_slug) as conn:
        flow = sub["flow"]
        token = sub["claim_token"]
        if operation == "current":
            lease_id = sub["route_id"] if flow == "ping" else sub["subscription_id"]
            return _kbn().notify_v2_claim_is_current(
                conn,
                flow=flow,
                lease_id=lease_id,
                claim_token=token,
                event_id=event_id,
            )
        if operation == "settle":
            if flow == "ping":
                return _kbn().settle_notify_ping(
                    conn, route_id=sub["route_id"], claim_token=token,
                    event_id=event_id,
                )
            return _kbn().settle_notify_wake(
                conn, subscription_id=sub["subscription_id"],
                claim_token=token, event_id=event_id,
            )
        if operation == "release":
            if flow == "ping":
                return _kbn().release_notify_ping(
                    conn, route_id=sub["route_id"], claim_token=token,
                    event_id=event_id,
                )
            return _kbn().release_notify_wake(
                conn, subscription_id=sub["subscription_id"],
                claim_token=token, event_id=event_id,
            )
        if operation == "unsubscribe":
            if flow == "ping" and sub.get("delivery_mode") != "notify":
                return True
            return _kbn().remove_notify_authority(
                conn, subscription_id=sub["subscription_id"]
            )
    return False


class _KanbanNotification:
    """Deliver one subscription's claimed events, then settle the cursor.

    Both legs of notify+wake must succeed before settling. Sent pings have a
    separate durable checkpoint so wake retries do not resend them. Admission
    is at-least-once queueing, not an execution or final-response receipt.
    """

    def __init__(self, runner: Any, d: dict, *, platform_cls: Any, sub_fail_counts: dict) -> None:
        self.runner = runner
        self.d = d
        self.platform_cls = platform_cls
        self.sub_fail_counts = sub_fail_counts
        self.sub = sub = d["sub"]
        self.task = task = d["task"]
        self.board_slug = d.get("board")
        self.platform_str = (sub["platform"] or "").lower()
        self.task_id = sub["task_id"]
        self.persisted_profile = sub.get("notifier_profile") or None
        self.sub_profile = d.get("resolved_profile") or self.persisted_profile or ""
        self.adapter_profile = d.get("resolved_adapter_profile")
        self.title = (task.title if task else sub["task_id"])[:120]
        self.board_tag = f"[{self.board_slug}] " if self.board_slug else ""
        # Attribute the ping to the worker that did the work.
        tag = f"@{task.assignee} " if task and task.assignee else ""
        self.head = f"{self.board_tag}{tag}Kanban {self.task_id}"
        # The wake self-post path needs the key even when every event was skipped.
        self.sub_key = (sub["task_id"], sub["platform"], sub["chat_id"], sub.get("thread_id") or "")
        self.v2_flow = sub.get("flow") if sub.get("flow") in ("ping", "wake") else None
        if self.v2_flow:
            self.sub_key = (
                *self.sub_key,
                self.v2_flow,
                sub["route_id"] if self.v2_flow == "ping" else sub["subscription_id"],
            )
        mode = sub.get("delivery_mode") or "notify"
        self.wake_agent = self.v2_flow == "wake" if self.v2_flow else mode in ("notify+wake", "wake")
        self.send_passive = self.v2_flow == "ping" if self.v2_flow else mode != "wake"
        # Worker handoff carried into the synthetic wake turn so the woken
        # creator doesn't re-decompose work already on the board.
        self.wake_handoff = self.wake_review_detail = self.session_key = self.synth = ""
        self.plat: Any = None
        self.adapter: Any = None
        self.is_push_adapter = True
        self.wake_kinds: set = set()

    # -- cursor / subscription ops (blocking, run in a fresh-context thread) --

    async def rewind(self) -> None:
        if self.v2_flow:
            await _to_thread_process_service(
                _v2_claim_op,
                self.board_slug,
                "release",
                self.sub,
                self.d["cursor"],
            )
            return
        await _to_thread_process_service(
            self.runner._kanban_rewind, self.sub, self.d["cursor"], self.d.get("old_cursor", 0), self.board_slug,
        )

    async def advance(self) -> None:
        if self.v2_flow:
            await _to_thread_process_service(
                _v2_claim_op,
                self.board_slug,
                "settle",
                self.sub,
                self.d["cursor"],
            )
            return
        await _to_thread_process_service(self.runner._kanban_advance, self.sub, self.d["cursor"], self.board_slug)

    async def unsub(self) -> None:
        if self.v2_flow:
            await _to_thread_process_service(
                _v2_claim_op,
                self.board_slug,
                "unsubscribe",
                self.sub,
                self.d["cursor"],
            )
            return
        await _to_thread_process_service(self.runner._kanban_unsub, self.sub, self.board_slug)

    def clear_failures(self) -> None:
        self.sub_fail_counts.pop(self.sub_key, None)

    async def delivery_failed(self, fmt: str, prefix: tuple, drop_fmt: str, exc: Exception, exc_info: bool) -> None:
        """Bump the failure counter; drop the sub past the limit, else rewind the claim so the next tick retries."""
        fails = self.sub_fail_counts.get(self.sub_key, 0) + 1
        self.sub_fail_counts[self.sub_key] = fails
        logger.warning(fmt, *prefix, fails, MAX_SEND_FAILURES, exc, exc_info=exc_info)
        if fails >= MAX_SEND_FAILURES:
            logger.warning(drop_fmt, self.task_id, self.platform_str, fails)
            await self.unsub()
            self.clear_failures()
        else:
            await self.rewind()

    async def _wake_failed(self, fmt: str, exc: Exception) -> None:
        drop_fmt = "kanban notifier: dropping subscription %s on %s after %d consecutive wake failures"
        await self.delivery_failed(fmt, (self.task_id,), drop_fmt, exc, True)

    # -- formatting --

    def format_event(self, ev: Any) -> Optional[str]:
        """Render one event; accumulates wake handoff/review detail. None → silent kind."""
        formatter = _EVENT_FORMATTERS.get(ev.kind)
        if formatter is None:
            return None
        msg, handoff, review_detail = formatter(ev, self)
        if handoff is not None:
            self.wake_handoff = handoff
        if review_detail is not None:
            self.wake_review_detail = review_detail
        return msg

    def _stale_block_loop_detection_ids(self) -> set[int]:
        """Ids of ``block_loop_detected`` events superseded by a later
        ``block_loop_resolved`` in the same claimed batch.

        A triage escalation already resolved before this tick must not ping a
        human with a stale "routed to TRIAGE" alert. The cursor still advances
        past the skipped event (it was claimed); only the alert and its wake are
        suppressed. History is preserved, and a still-open detection (no later
        resolution) is delivered normally.
        """
        events = self.d["events"]
        resolved_ids = {ev.id for ev in events if ev.kind == "block_loop_resolved"}
        if not resolved_ids:
            return set()
        return {
            ev.id for ev in events
            if ev.kind == "block_loop_detected" and any(rid > ev.id for rid in resolved_ids)
        }

    def build_wake_text(self) -> None:
        """Set ``wake_kinds`` / ``session_key`` / ``synth`` for the wake paths."""
        task, sub = self.task, self.sub
        # A block_loop_detected already resolved on the board must not wake the
        # creator for a closed loop.
        stale = self._stale_block_loop_detection_ids()
        self.wake_kinds = {
            ev.kind for ev in self.d["events"]
            if ev.kind in _WAKE_KINDS and ev.id not in stale
        } if self.wake_agent else set()
        self.wake_diagnostic = all(diagnostic_event(ev) for ev in self.d["events"] if ev.kind in self.wake_kinds)
        if not self.wake_kinds:
            return
        if self.is_push_adapter:
            self.session_key = getattr(task, "session_id", None) or ""
        else:
            # Non-push wakes target sub["chat_id"] (the raw session id the
            # subscriber registered). task.session_id may be a WORKER session
            # for child tasks; use it only for legacy rows.
            self.session_key = sub["chat_id"] or getattr(task, "session_id", None) or ""
        # i18n keys: gateway.kanban.wake.<kind> for each _WAKE_KINDS entry.
        _parts = [t(f"gateway.kanban.wake.{k}") for k in _WAKE_KINDS if k in self.wake_kinds]
        _status = t("gateway.kanban.wake.status_joiner").join(_parts) or t("gateway.kanban.wake.status_default")
        synth = t(
            "gateway.kanban.wake.message",
            task_id=sub["task_id"], status=_status, title=self.title,
            assignee=task.assignee if task else "", board=self.board_slug,
        )
        # Label as an automatic notification and carry the handoff so the
        # creator inspects the board instead of re-decomposing.
        if self.wake_handoff:
            synth += "\n" + t("gateway.kanban.wake.handoff", summary=self.wake_handoff)
        if self.wake_review_detail:
            synth += "\n" + t("gateway.kanban.wake.review_detail", reason=self.wake_review_detail)
        self.synth = synth + "\n\n" + t("gateway.kanban.wake.guidance")

    def _log_woke(self) -> None:
        logger.info("kanban notifier: woke agent for %s on %s profile=%s events=%s",
                    self.task_id, self.platform_str, self.sub_profile or "default", self.wake_kinds)

    def _owner_scope(self):
        """Runtime scope required by this flow under a multiplex gateway."""
        runner = self.runner
        scope_profile = (
            self.adapter_profile
            if self.v2_flow == "ping"
            else self.sub_profile
        )
        if not (
            scope_profile
            and getattr(getattr(runner, "config", None), "multiplex_profiles", False)
        ):
            return contextlib.nullcontext()
        from gateway.run import _async_profile_runtime_scope
        from gateway.session import SessionSource
        source = SessionSource(
            platform=self.plat,
            chat_id=self.sub["chat_id"],
            profile=scope_profile,
        )
        return _async_profile_runtime_scope(runner._resolve_profile_home_for_source(source))

    async def wake(self) -> bool:
        """Wake the creator session; False when delivery authority went stale."""
        from gateway.wake import deliver_wake
        sub = self.sub
        if not self.is_push_adapter:
            # Revalidate the durable route after hydration, immediately before
            # the external emission.
            if await self._current_route() is None:
                return False
            await deliver_wake(self.adapter, text=self.synth, session_id=self.session_key,
                               notification_category="diagnostic" if self.wake_diagnostic else "result")
            self._log_woke()
            return True
        from gateway.session import SessionSource
        # Rebuild the creator's real session scope from the persisted chat_type:
        # build_session_key() keys DMs differently from group/thread, so a
        # hardcoded "group" mis-routed DM/thread creators into a fresh session.
        # Legacy rows may carry chat_type in delivery_metadata; last resort is
        # "group". A mismatch only degrades to a fresh session.
        # Legacy rows written before the column existed may still carry chat_type in delivery_metadata
        # (#60600 rows) — fall back to that, then to "group" (the historical default that suits the
        # dashboard/group flows). handle_message() get_or_create_session's the target, so a mismatch only
        # ever degrades to a fresh session, never an exception.
        _delivery_meta = sub.get("delivery_metadata") or {}
        _chat_type = str(sub.get("chat_type") or _delivery_meta.get("chat_type") or "").strip()
        _source = SessionSource(
            platform=self.plat, chat_id=sub["chat_id"], chat_type=_chat_type or "group",
            thread_id=sub.get("thread_id") or None, user_id=sub.get("user_id"), user_id_alt=sub.get("user_id_alt"),
            profile=self.sub_profile or None, scope_id=_wake_scope_id(self.adapter, sub),
            parent_chat_id=_delivery_meta.get("parent_chat_id"),
        )
        _source._transport_adapter_ref = weakref.ref(self.adapter)
        from gateway.run import _async_profile_runtime_scope
        if self.sub_profile and getattr(getattr(self.runner, "config", None), "multiplex_profiles", False):
            from hermes_cli.profiles import profile_exists
            if not profile_exists(self.sub_profile):
                raise RuntimeError(f"Kanban wake profile {self.sub_profile!r} no longer exists")
        async with _async_profile_runtime_scope(self.runner._resolve_profile_home_for_source(_source)):
            if await self._current_route() is None:
                return False
            await deliver_wake(self.adapter, text=self.synth, session_id=self.session_key, source=_source,
                               notification_category="diagnostic" if self.wake_diagnostic else "result")
        self._log_woke()
        return True

    async def _send_event(self, ev: Any, msg: str) -> bool:
        """Send one text ping; raises on adapter exception or SendResult(success=False)."""
        from gateway.warning_notifications import present_notification
        sub, adapter = self.sub, self.adapter
        delivery_metadata = sub.get("delivery_metadata")
        metadata: dict[str, Any] = dict(delivery_metadata) if isinstance(delivery_metadata, dict) else {}
        if sub.get("thread_id") and not metadata.get("thread_id"):
            metadata["thread_id"] = sub["thread_id"]
        _send_res = None
        async def send_ping():
            nonlocal _send_res
            _send_res = await adapter.send(sub["chat_id"], msg, metadata=metadata)
        if not await present_notification(send_ping, platform=self.platform_str, diagnostic=diagnostic_event(ev)):
            return False
        # SendResult(success=False) without an exception is a FAILED delivery
        # (else the event is lost); None / non-SendResult keeps the
        # "no exception == delivered" contract.
        if getattr(_send_res, "success", True) is False:
            raise RuntimeError(f"adapter send() reported failure: {getattr(_send_res, 'error', None) or 'unknown error'}")
        # Route identity stays out of the logs (chat/thread ids are private);
        # task + platform + board + adapter profile is enough to follow a delivery.
        logger.debug("kanban notifier: delivered %s event for %s on %s via %s on board %s",
                     ev.kind, self.task_id, self.platform_str, self.adapter_profile or "", self.board_slug)
        # Upload artifact paths from the handoff payload / legacy result as
        # native files. Both handoff kinds stage files for exactly this: a
        # review-bound card's files exist precisely so the human sees them at
        # handoff time. Retry exposure matches ``completed`` (the sub cursor is
        # rewound only when a send failed).
        if ev.kind in ("completed", "review_requested"):
            try:
                await self.runner._deliver_kanban_artifacts(
                    adapter=adapter, chat_id=sub["chat_id"], metadata=metadata,
                    event_payload=getattr(ev, "payload", None), task=self.task,
                )
            except Exception as art_exc:
                logger.debug("kanban notifier: artifact delivery for %s failed: %s", self.task_id, art_exc)
        return True

    async def _send_pings(self) -> bool:
        """Send every text ping; False when a send failed (claim already rewound/dropped)."""
        stale = self._stale_block_loop_detection_ids()
        for ev in self.d["events"]:
            if ev.id in stale:
                # Superseded by a later block_loop_resolved in this batch: skip the
                # stale "routed to TRIAGE" alert (and its wake). Cursor still advances.
                continue
            msg = self.format_event(ev)
            if msg is None:
                continue
            # Non-push adapters (api_server) always report SendResult(success=False)
            # from send(); treating that as failure would drop the sub forever and
            # make the wake path unreachable. Skip the doomed send; the self-post
            # IS the delivery and resolves the failure counter.
            if not self.is_push_adapter and self.wake_agent:
                logger.debug(
                    "kanban notifier: adapter %s has no push channel; skipping text ping for %s, relying "
                    "on wake self-post instead", self.platform_str, self.task_id,
                )
                continue
            if not self.send_passive:
                # Wake-only: the wake path is the sole delivery and resolves the counter.
                continue
            if ev.id <= self.sub.get("last_ping_event_id", 0):
                continue
            if await self._current_route() is None:
                await self._rewind_stale_authority()
                return False
            try:
                if await self._send_event(ev, msg) is False:
                    continue
                # A v2 flow advances its own durable route/authority cursor;
                # only the legacy row projects progress through the sub op.
                if not self.v2_flow:
                    await _to_thread_process_service(partial(
                        self.runner._kanban_sub_op, self.board_slug, "record_notify_ping", self.sub,
                        event_id=ev.id,
                    ))
                self.clear_failures()
            except Exception as exc:
                await self.delivery_failed(
                    "kanban notifier: send failed for %s on %s (attempt %d/%d): %s", (self.task_id, self.platform_str),
                    "kanban notifier: dropping subscription %s on %s after %d consecutive send failures", exc, False,
                )
                return False
        return True

    async def _current_route(self) -> Optional[tuple[Any, Optional[str], Optional[str]]]:
        """Revalidate both the durable row lease and live route authority."""
        if self.v2_flow:
            current = await _to_thread_process_service(
                _v2_claim_op,
                self.board_slug,
                "current",
                self.sub,
                self.d["cursor"],
            )
            if not current:
                return None
            resolved = _resolve_v2_authority(self.runner, self.plat, self.sub)
            if (
                resolved is None
                or resolved[1] != self.sub_profile
                or resolved[2] != self.adapter_profile
            ):
                return None
            if self.adapter is not None and resolved[0] is not self.adapter:
                return None
            return resolved
        current = await _to_thread_process_service(
            self.runner._kanban_sub_current, self.sub, self.board_slug,
        )
        if not current:
            return None
        resolved = _resolve_subscription_route(
            self.runner, self.plat, self.sub, self.persisted_profile,
        )
        if (
            resolved is None
            or (resolved[1] or "") != self.sub_profile
            or resolved[2] != self.adapter_profile
        ):
            return None
        if self.adapter is not None and resolved[0] is not self.adapter:
            return None
        return resolved

    async def _rewind_stale_authority(self) -> None:
        logger.debug(
            "kanban notifier: delivery authority changed for %s on %s; rewinding claim",
            self.task_id, self.platform_str,
        )
        await self.rewind()

    async def deliver(self) -> None:
        try:
            self.plat = self.platform_cls(self.platform_str)
        except ValueError:
            await self.advance()
            return
        # Recheck both the exact durable row and its route after claiming:
        # subscriptions, config and adapters can all change between ticks.
        resolved = await self._current_route()
        if resolved is None:
            logger.debug("kanban notifier: route revalidation failed for %s", self.task_id)
            await self._rewind_stale_authority()
            return
        adapter = resolved[0]
        self.adapter = adapter
        from gateway.wake import adapter_supports_push
        self.is_push_adapter = adapter_supports_push(adapter)

        # Pings, artifact uploads (media policy) and the wake text (display.language) all read the
        # SUBSCRIBER profile's config; the notifier thread itself runs in the launch profile's scope.
        from gateway.profile_routing import ProfileRouteRejected
        wake_payloads = []
        try:
            async with self._owner_scope():
                # Secret/config hydration yields to the event loop. Revalidate after
                # that boundary and immediately before any external emission.
                if await self._current_route() is None:
                    await self._rewind_stale_authority()
                    return
                if not await self._send_pings():
                    return
                # All text pings delivered (or skipped for non-push / wake-only).
                # Diagnostic and result events wake separately when user-channel
                # warning notifications are off, so a wake never carries a
                # category the operator asked to suppress.
                original_events = self.d["events"]
                from gateway.warning_notifications import warning_notifications_enabled
                split = not warning_notifications_enabled(self.platform_str)
                wake_groups = ([original_events] if not split else [
                    [ev for ev in original_events if diagnostic_event(ev)],
                    [ev for ev in original_events if not diagnostic_event(ev)],
                ])
                for events in wake_groups:
                    if not events:
                        continue
                    self.d = {**self.d, "events": events}
                    self.wake_handoff = self.wake_review_detail = ""
                    for ev in events:
                        self.format_event(ev)
                    self.build_wake_text()
                    if self.wake_kinds:
                        wake_payloads.append((self.synth, self.wake_diagnostic, self.wake_kinds))
                self.d = {**self.d, "events": original_events}
        except ProfileRouteRejected:
            await self._rewind_stale_authority()
            return
        wake_kinds, is_push = self.wake_kinds, self.is_push_adapter
        from gateway.wake import WakeNotAccepted

        # A requested wake is required even when its passive ping already landed.
        if wake_payloads:
            try:
                for self.synth, self.wake_diagnostic, self.wake_kinds in wake_payloads:
                    if not await self.wake():
                        await self._rewind_stale_authority()
                        return
                self.clear_failures()
            except ProfileRouteRejected:
                await self._rewind_stale_authority()
                return
            except WakeNotAccepted:
                # Startup / full queue is not a dead destination. Keep the durable
                # subscription alive regardless of how long admission takes.
                await self.rewind()
                return
            except Exception as _wk_err:
                await self._wake_failed(
                    "kanban notifier: wake-only delivery failed for %s (attempt %d/%d): %s" if is_push
                    else "kanban notifier: wake self-post failed for %s (attempt %d/%d): %s",
                    _wk_err,
                )
                return

        # Delivery complete: advance the cursor (the dedup mechanism).
        await self.advance()
        if not is_push:
            self.clear_failures()
        # Unsubscribe only on archive; ``done`` is reversible.
        if self.task and self.task.status == "archived":
            await self.unsub()
