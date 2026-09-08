# Candidat bounded Amber subscription reconciliation — rollback manifest

Status: source-only candidate; no live board, gateway, job, subscription, Brain file, or runtime tree was changed. The existing job remains disabled until a fresh Architect GO and Amber activation gate.

## Exact candidate

- Prior reviewed candidate: `b7752bdfe62fd15a5fb9d8b64df0ac923935ce2e`.
- Final candidate: `e247d38c99754c77c6c0f6bca8f3ed6ed18d8ef9`.
- Branch: `ironrod/forge-amber-subscription-transfer-20260907`.
- Existing job: `85fcd56ee535`, `forge-kanban-telegram-subscriptions`, `no_agent=true`, every 1 minute, script `kanban_telegram_subscribe_all.py`, observed `enabled=false`, state `paused`.
- Runtime base remains `b20d9f3c7c8a0a709e862f63240eb3d6fe302e53`; no runtime promotion was performed.

## Candidate changes and bounded effect

- `hermes_cli/kanban_db.py`: native `restore_notify_sub_state` inverse. It restores only owner and delivery mode from exact pre/post images, preserves an advanced cursor, and refuses human takeover, replacement, metadata/identity change, or other concurrent modification without overwriting it. Rows created by the batch are removed only while their post-image is unchanged; a repeated reverse is idempotent.
- `profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py`: distinguishes destination identity from origin completeness. NULL legacy origin IDs are compatible with one known non-empty origin; true destination/origin conflicts fail closed. Existing rows are not enriched. A deterministic known anchor is used only for new rows. `--journal PATH` writes the committed batch's exact pre/post-images after the outer transaction commits.
- `tests/profile_overlay/test_telegram_subscribe.py`: existing 63 checks retained plus compatible-NULL and conflicting-origin fixtures.
- `artifacts/verify-amber-subscription-rollback.py`: disposable DB verifier for transfer/repair/new-card coverage, unread cursor preservation, native guarded inverse, concurrent conflict rejection, idempotent reverse, quick-check, and exact file/job copy restoration with hashes and modes.

## Forward activation preconditions (not performed here)

1. Architect reviews this exact final SHA after the previous NO-GO; a source-only GO is not an activation order.
2. Amber confirms effective WIP values; do not change configured ceilings.
3. Confirm job `85fcd56ee535` still has the same script, `no_agent=true`, interval 1m, and `enabled=false`/`paused` before any activation.
4. Capture the targeted live script/runtime/job pre-images, bytes, SHA-256, modes, ownership, and job JSON. Preserve unrelated pre-existing runtime changes.
5. Create a dated read-only SQLite backup with `VACUUM INTO`; verify `PRAGMA quick_check`, task count, subscription count, and SHA-256. Never use `sqlite3 .backup` on the live board.
6. Activate only the existing job through its native cron tool after both gates. Do not add a cron, watcher, webhook, or dispatcher.
7. Bound the first run to `--limit 1 --journal <durable-gated-path>` and prove event → Amber session → native action, then inspect coverage/read-back and job state.

## Native rollback procedure

1. Stop/disable the same existing job through the native cron tool first. Preserve its exact pre-activation JSON and restore the prior enabled/paused state through the same native tool.
2. Keep the committed journal produced by the actual run. Reverse entries in reverse order with `rollback_journal`, which calls the native `restore_notify_sub_state` API inside one outer transaction.
3. For each journal entry with a pre-image, restore only `notifier_profile` and `delivery_mode` to that pre-image. Preserve `last_event_id`, events, origin IDs, metadata, `created_at`, and all rows outside the journal.
4. For each entry with no pre-image, remove the exact `(task_id, platform, chat_id, thread_id)` row only if its post-image still matches. If a human took it over, changed it, or recreated it, abort the whole inverse with a conflict and make no overwrite.
5. Re-read targeted rows, advanced cursors, independent human rows, subscription counts, and `PRAGMA quick_check`. A replay of the same inverse is a no-op for already restored rows and remains conflict-safe.
6. If source removal is required, revert `e247d38c99754c77c6c0f6bca8f3ed6ed18d8ef9` in a controlled source worktree to `b7752bdfe62fd15a5fb9d8b64df0ac923935ce2e`, rebuild/retest, and separately restore the live targeted files from the captured pre-images. A Git revert alone is not a database/job rollback.

The verifier exercises the DB inverse, backup integrity, cursor preservation, conflict refusal, replay, and copy-only file/job restoration. It is not a live backup and does not authorize activation.
