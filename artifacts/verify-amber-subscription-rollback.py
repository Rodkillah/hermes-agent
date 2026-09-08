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
import subprocess
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
        backup = db.with_name("pre-activation-backup.db")
        conn.execute("VACUUM INTO ?", (str(backup),))
        with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as backup_conn:
            assert backup_conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert backup_conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 5
            assert backup_conn.execute("SELECT count(*) FROM kanban_notify_subs").fetchone()[0] == 4
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
    """Exercise exact candidate files and the existing job via native APIs.

    This is deliberately copy-only: it reads the active Amber jobs document
    once, makes a private copy, and mutates only that copy through cron.jobs.
    It never points cron at the live file and never emits the document.
    """
    from cron import jobs as cron_jobs

    base = "b20d9f3c7c8a0a709e862f63240eb3d6fe302e53"
    target_db = root / "live/hermes_cli/kanban_db.py"
    target_script = root / "live/profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py"
    for target in (target_db, target_script):
        target.parent.mkdir(parents=True, exist_ok=True)

    # The current candidate bytes are the real files to install.  The base
    # runtime contains kanban_db.py but did not contain this new overlay
    # script, so restoration must preserve that exact absence.
    base_db = subprocess.check_output(
        ["git", "show", f"{base}:hermes_cli/kanban_db.py"], cwd=ROOT
    )
    target_db.write_bytes(base_db)
    target_db.chmod(0o640)
    pre_db = {"hash": file_hash(target_db), "mode": file_mode(target_db)}
    assert not target_script.exists()

    shutil.copy2(ROOT / "hermes_cli/kanban_db.py", target_db)
    shutil.copy2(SCRIPT, target_script)
    assert file_hash(target_db) == file_hash(ROOT / "hermes_cli/kanban_db.py")
    assert file_hash(target_script) == file_hash(SCRIPT)
    target_db.write_bytes(base_db)
    target_db.chmod(pre_db["mode"])
    target_script.unlink()
    assert file_hash(target_db) == pre_db["hash"]
    assert file_mode(target_db) == pre_db["mode"]
    assert not target_script.exists()

    # Read the real existing job document, then route cron.jobs through an
    # isolated copy.  Only the named job's administrative fields may change;
    # other records, claims and history are byte-for-byte copied and compared
    # structurally after the native pause/restore cycle.
    live_jobs = Path(
        os.environ.get(
            "AMBER_PROFILE_JOBS_FILE",
            "/home/rodrigue/.hermes/profiles/amber/cron/jobs.json",
        )
    )
    raw_document = json.loads(live_jobs.read_text(encoding="utf-8"))
    raw_jobs = raw_document.get("jobs", raw_document) if isinstance(raw_document, dict) else raw_document
    if not isinstance(raw_jobs, list):
        raise AssertionError("Amber jobs document has no job list")
    original = next(job for job in raw_jobs if job.get("id") == "85fcd56ee535")
    private_cron = root / "private-cron"
    private_cron.mkdir(mode=0o700)
    private_jobs = private_cron / "jobs.json"
    shutil.copy2(live_jobs, private_jobs)
    original_constants = cron_jobs.CRON_DIR, cron_jobs.JOBS_FILE, cron_jobs.OUTPUT_DIR
    cron_jobs.CRON_DIR = private_cron
    cron_jobs.JOBS_FILE = private_jobs
    cron_jobs.OUTPUT_DIR = private_cron / "output"
    try:
        observed = cron_jobs.get_job("85fcd56ee535")
        assert observed and observed["id"] == original["id"]
        admin = {key: original.get(key) for key in (
            "enabled", "state", "paused_at", "paused_reason", "next_run_at"
        )}
        activated = cron_jobs.update_job(
            observed["id"],
            {"enabled": True, "state": "scheduled", "paused_at": None, "paused_reason": None},
        )
        assert activated and activated["enabled"] is True
        paused = cron_jobs.pause_job(observed["id"], reason="private rollback verifier")
        assert paused and paused["enabled"] is False and paused["state"] == "paused"
        restored = cron_jobs.update_job(observed["id"], admin)
        assert restored and all(restored.get(key) == value for key, value in admin.items())
    finally:
        cron_jobs.CRON_DIR, cron_jobs.JOBS_FILE, cron_jobs.OUTPUT_DIR = original_constants
    restored_document = json.loads(private_jobs.read_text(encoding="utf-8"))
    restored_jobs = (
        restored_document.get("jobs", restored_document)
        if isinstance(restored_document, dict) else restored_document
    )
    restored_job = next(job for job in restored_jobs if job.get("id") == original["id"])
    assert all(restored_job.get(key) == value for key, value in admin.items())
    assert [job for job in restored_jobs if job.get("id") != original["id"]] == [
        job for job in raw_jobs if job.get("id") != original["id"]
    ]


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
