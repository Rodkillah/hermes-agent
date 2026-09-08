#!/usr/bin/env python3
"""Exercise the bounded Amber subscription rollback on disposable DBs only."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py"
os.environ["HERMES_AGENT_RUNTIME"] = str(ROOT)
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("amber_subscription", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
kb = module.kb


def seed(conn, owner=None, mode=None, chat_id="chat"):
    task_id = kb.create_task(conn, title="rollback fixture", assignee="worker")
    if owner is not None:
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id=chat_id,
            thread_id="thread",
            user_id="user",
            user_id_alt="alt",
            chat_type="dm",
            notifier_profile=owner,
            delivery_mode=mode or "notify+wake",
            delivery_metadata={"fixture": "rollback", "thread": 7},
        )
    return task_id


def unread(conn, task_id):
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'changes_requested', '{}', 123)",
            (task_id,),
        )
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = 0 WHERE task_id = ?",
            (task_id,),
        )


def run_nominal(db: Path):
    kb.init_db(db)
    conn = kb.connect(db)
    try:
        anchor = seed(conn, owner="amber")
        forge = seed(conn, owner="forge")
        notify_only = seed(conn, owner="amber", mode="notify")
        created = seed(conn)
        human = seed(conn, owner="human", chat_id="human-chat")
        for task_id in (forge, notify_only, created):
            unread(conn, task_id)
        before = {
            task_id: dict(kb.list_notify_subs(conn, task_id)[0])
            for task_id in (anchor, forge, notify_only, human)
        }
        journal = []
        migrated = module.reconcile(conn, journal=journal)
        assert migrated["changed"] == 4, migrated
        assert len(journal) == 4, journal
        # A notification claim may advance a cursor after apply and is allowed
        # to survive rollback.
        key = dict(task_id=forge, platform="telegram", chat_id="chat", thread_id="thread")
        _, advanced_cursor, claimed = kb.claim_unseen_events_for_sub(conn, **key)
        assert claimed and advanced_cursor > before[forge]["last_event_id"]
        independent_before = dict(kb.list_notify_subs(conn, human)[0])

        restored = module.rollback_journal(conn, journal)
        assert restored == 4
        assert kb.list_notify_subs(conn, created) == []
        forge_after = kb.list_notify_subs(conn, forge)[0]
        notify_after = kb.list_notify_subs(conn, notify_only)[0]
        assert forge_after["notifier_profile"] == before[forge]["notifier_profile"]
        assert forge_after["delivery_mode"] == before[forge]["delivery_mode"]
        assert forge_after["last_event_id"] == advanced_cursor
        assert notify_after["notifier_profile"] == before[notify_only]["notifier_profile"]
        assert notify_after["delivery_mode"] == before[notify_only]["delivery_mode"]
        assert dict(kb.list_notify_subs(conn, human)[0]) == independent_before
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"

        # Re-apply and reverse twice: the second reverse is an idempotent no-op.
        second_journal = []
        reapplied = module.reconcile(conn, journal=second_journal)
        assert reapplied["changed"] == 4
        assert module.rollback_journal(conn, second_journal) == 4
        assert module.rollback_journal(conn, second_journal) == 4
        assert kb.list_notify_subs(conn, created) == []
        assert dict(kb.list_notify_subs(conn, human)[0]) == independent_before
        return migrated, advanced_cursor
    finally:
        conn.close()


def run_conflict(db: Path):
    kb.init_db(db)
    conn = kb.connect(db)
    try:
        seed(conn, owner="amber")
        forge = seed(conn, owner="forge")
        journal = []
        module.reconcile(conn, journal=journal)
        assert journal
        assert kb.transfer_notify_sub_owner(
            conn,
            task_id=forge,
            platform="telegram",
            chat_id="chat",
            thread_id="thread",
            expected_owner="amber",
            new_owner="human",
        )
        before = dict(kb.list_notify_subs(conn, forge)[0])
        try:
            module.rollback_journal(conn, journal)
        except RuntimeError as exc:
            assert "rollback conflict" in str(exc)
        else:
            raise AssertionError("concurrent owner takeover was not rejected")
        assert dict(kb.list_notify_subs(conn, forge)[0]) == before
    finally:
        conn.close()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def run_file_job_restore(root: Path):
    """Apply and reverse only the targeted overlay/job on disposable copies."""
    live_script = root / "live/profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py"
    live_job = root / "live/jobs/85fcd56ee535.json"
    unrelated = root / "live/runtime/tools-send-message-existing.py"
    candidate_job = root / "candidate-job.json"
    for path in (live_script, live_job, unrelated, candidate_job):
        path.parent.mkdir(parents=True, exist_ok=True)
    live_script.write_bytes(b"preactivation-script\n")
    live_script.chmod(0o640)
    live_job.write_text(json.dumps({"id": "85fcd56ee535", "enabled": False, "state": "paused"}) + "\n")
    live_job.chmod(0o600)
    unrelated.write_bytes(b"preexisting-dirty-runtime\n")
    unrelated.chmod(0o644)
    candidate_job.write_text(json.dumps({"id": "85fcd56ee535", "enabled": True, "state": "running"}) + "\n")

    pre = {
        "script_hash": file_hash(live_script),
        "script_mode": file_mode(live_script),
        "job_hash": file_hash(live_job),
        "job_mode": file_mode(live_job),
        "unrelated_hash": file_hash(unrelated),
        "unrelated_mode": file_mode(unrelated),
    }
    script_backup = root / "backup-script"
    job_backup = root / "backup-job"
    shutil.copy2(live_script, script_backup)
    shutil.copy2(live_job, job_backup)

    # Candidate installation and existing-job activation are simulated only on
    # copies; the precondition is the exact observed disabled job identity.
    assert json.loads(live_job.read_text())["id"] == "85fcd56ee535"
    assert json.loads(live_job.read_text())["enabled"] is False
    shutil.copy2(SCRIPT, live_script)
    shutil.copy2(candidate_job, live_job)
    assert json.loads(live_job.read_text())["enabled"] is True

    # Targeted inverse restores bytes and modes, leaving unrelated runtime data.
    shutil.copy2(script_backup, live_script)
    shutil.copy2(job_backup, live_job)
    assert file_hash(live_script) == pre["script_hash"]
    assert file_mode(live_script) == pre["script_mode"]
    assert file_hash(live_job) == pre["job_hash"]
    assert file_mode(live_job) == pre["job_mode"]
    assert file_hash(unrelated) == pre["unrelated_hash"]
    assert file_mode(unrelated) == pre["unrelated_mode"]
    assert json.loads(live_job.read_text())["state"] == "paused"


with tempfile.TemporaryDirectory(prefix="amber-subscription-rollback-") as temp:
    root = Path(temp)
    migrated, advanced_cursor = run_nominal(root / "nominal.db")
    run_conflict(root / "conflict.db")
    run_file_job_restore(root / "files")

print(json.dumps({
    "migration": migrated,
    "advanced_cursor_preserved": advanced_cursor,
    "rollback": "native guarded pre/post-image inverse",
    "concurrent_conflict": "rejected without overwrite",
    "replay": "idempotent",
    "quick_check": "ok",
}, sort_keys=True))
print("targeted Amber rollback verifier: PASS")
