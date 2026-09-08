# Candidat bounded Amber subscription reconciliation — rollback manifest

Status: source-only candidate; the existing job remains disabled until a fresh Architect GO and Amber activation gate.

## Exact candidate

- Base reviewed primitive: `bba2fe5e40ec6037df9f3c06a5a5876aacba6158`.
- Candidate after caller patch and verifier: `d87bf1e8e8c6585a9c07764362f55474d453eee4`.
- Branch: `ironrod/forge-amber-subscription-transfer-20260907`.
- Existing job: `85fcd56ee535`, `forge-kanban-telegram-subscriptions`, `no_agent=true`, every 1 minute, script `kanban_telegram_subscribe_all.py`, observed `enabled=false`, `state=paused`.

## Files and bounded effect

- `profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py`: replaces the old caller's remove/add sequence with the native `transfer_notify_sub_owner` CAS plus an explicit mode repair. Missing rows are created only at the already-established Amber/Forge Telegram DM destination. One batch is inside one outer `write_txn`; the exact row's owner, mode, cursor, origin identifiers, metadata and creation time are read back. Human/unknown/unowned/conflicting destinations fail closed.
- `tests/profile_overlay/test_telegram_subscribe.py`: disposable-DB tests for transfer, notify-only repair, missing/new task coverage, human preservation, unread cursor preservation, poisoned `HERMES_KANBAN_*` scope, atomic rollback and two real processes.
- `artifacts/verify-amber-subscription-rollback.py`: reproducible disposable-DB rollback and re-apply verifier.

No live board, cron, profile, gateway, subscription or Brain file is changed by this candidate.

## Activation preconditions (not performed here)

1. Re-review this exact candidate SHA after the previous NO-GO.
2. Amber confirms the effective existing WIP values; do not change the configured ceilings.
3. Confirm the job still has id `85fcd56ee535`, same script, `no_agent=true`, interval 1m, and `enabled=false` before any activation.
4. Create a dated read-only SQLite backup with `VACUUM INTO` and verify `PRAGMA quick_check`, task count and subscription count from the backup. Record the backup path and SHA-256 on the card; never use `sqlite3 .backup` on the live board.
5. Activate only the existing job through the native cron tool after the gate. Do not add a cron, watcher, webhook or dispatcher.
6. Bound the first run to `--limit 1` and prove event → Amber session → native action, then inspect coverage/read-back and the job state.

## Native rollback procedure

Stop/disable the same existing job through the native cron tool first (no DB write). Preserve the pre-activation job JSON as the job rollback reference.

For each row changed by the run, using the exact recorded key `(task_id, platform, chat_id, thread_id)`:

- owner transfer: `transfer_notify_sub_owner(expected_owner="amber", new_owner="forge")`;
- mode repair: `add_notify_sub(delivery_mode="notify")` without supplying replacement identity/metadata/cursor fields;
- rows created by this run: `remove_notify_sub` for that exact key only;
- rows not listed in the manifest, human destinations, task events and advanced cursors: do not touch.

If the primitive source itself must also be removed, revert `d87bf1e8e8c6585a9c07764362f55474d453eee4` and then the prior `e67bb82f2a959837cc94f51e9aa900adeaa2ee62` in a controlled source worktree; do not revert `bba2fe5e` blindly when retaining the reviewed primitive is intended. Rebuild/retest after any source rollback. Restore the job's exact pre-activation enabled/paused state through the native cron tool.

The rollback harnais exercises the row-level native inverse, preserves an advanced cursor and independent human data, verifies the disposable backup, and re-applies the candidate twice (first pass changes rows, second pass changes zero rows). It is not a production backup and does not authorize activation.
