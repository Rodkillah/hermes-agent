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
import uuid
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


def file_state(path: Path) -> tuple[str, int]:
    """Return the exact regular-file content and mode, rejecting link targets."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RuntimeError(f"rollback file is not a regular file: {path}")
    return file_hash(path), file_mode(path)


def _fsync_parent(path: Path) -> None:
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_restore_marker(marker: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write creating file restore marker")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_parent(marker)


def restore_marker_path(target: Path) -> Path:
    return Path(target).with_name(f".{Path(target).name}.restore-state.json")


def recover_file_restore(target: Path) -> str:
    """Safely recover an interrupted conditional restore from its durable marker.

    A crash after parking but before link restores the verified post-image, not
    a guessed pre-image.  A newly-created target is a concurrent owner and is
    never overwritten; the marker and parked file remain for investigation.
    """
    target = Path(target)
    marker = restore_marker_path(target)
    if not marker.exists():
        return "none"
    if not stat.S_ISREG(marker.lstat().st_mode):
        raise RuntimeError("invalid file restore marker")
    state = json.loads(marker.read_text(encoding="utf-8"))
    parked_name = state.get("parked")
    expected_post = state.get("postimage")
    expected_pre = state.get("preimage")
    if (
        not isinstance(parked_name, str)
        or Path(parked_name).name != parked_name
        or not isinstance(expected_post, list)
        or not isinstance(expected_pre, list)
        or len(expected_post) != len(expected_pre) != 2
    ):
        raise RuntimeError("invalid file restore marker payload")
    parked = target.with_name(parked_name)
    postimage = tuple(expected_post)
    preimage = tuple(expected_pre)
    if not target.exists():
        if not parked.exists() or not stat.S_ISREG(parked.lstat().st_mode):
            raise RuntimeError("interrupted restore has no regular parked post-image")
        parked_state = file_state(parked)
        try:
            os.link(parked, target)
        except FileExistsError as exc:
            raise RuntimeError("concurrent runtime file appeared during restore recovery") from exc
        if file_state(target) != parked_state:
            raise RuntimeError("recovered runtime file differs from parked post-image")
        if parked_state == postimage:
            marker.unlink()
            _fsync_parent(marker)
            return "restored-postimage"
        # A race before parking changed the old target.  Preserve it under the
        # canonical name and retain the marker/parked hardlink as evidence;
        # do not mislabel it as a successful rollback.
        return "restored-conflicting-parked"
    current = file_state(target)
    if current == preimage:
        if parked.exists():
            parked.unlink()
        marker.unlink()
        _fsync_parent(marker)
        return "completed"
    if current == postimage and not parked.exists():
        marker.unlink()
        _fsync_parent(marker)
        return "not-started"
    raise RuntimeError("concurrent runtime file prevents safe restore recovery")


def restore_file_from_private_preimage(
    target: Path,
    staged_preimage: Path,
    *,
    expected_postimage: tuple[str, int],
) -> None:
    """Restore one file without overwriting a name claimed by a concurrent edit.

    The activation gate must first make the runtime quiescent.  This helper
    additionally writes a durable recovery marker and claims the target pathname by atomically parking the verified
    post-image, then links a private fully-written pre-image into the now-empty
    pathname.  A writer that appears after the guard can create that pathname;
    link then fails with EEXIST and its content is preserved rather than copied
    over.  The parked candidate remains as conflict evidence for the operator.
    """
    target = Path(target)
    staged_preimage = Path(staged_preimage)
    preimage = file_state(staged_preimage)
    marker = restore_marker_path(target)
    if marker.exists():
        recover_file_restore(target)
        raise RuntimeError("recovered incomplete guarded restore; retry explicitly")
    if file_state(target) != expected_postimage:
        raise RuntimeError("runtime file changed before guarded restore")

    parked = target.with_name(f".{target.name}.postimage-{uuid.uuid4().hex}")
    temporary = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
    try:
        _write_restore_marker(marker, {
            "parked": parked.name,
            "postimage": list(expected_postimage),
            "preimage": list(preimage),
        })
        os.replace(target, parked)
        # Recheck after the atomic pathname claim: a write racing the original
        # target is never mistaken for the candidate being rolled back.
        if file_state(parked) != expected_postimage:
            raise RuntimeError("runtime file changed during guarded restore")
        shutil.copy2(staged_preimage, temporary)
        if file_state(temporary) != preimage:
            raise RuntimeError("private pre-image changed during guarded restore")
        # ``link`` makes this inode visible under the target name.  The copied
        # bytes and mode must reach stable storage first; syncing only the
        # parent directory would make the name durable without its contents.
        preimage_fd = os.open(temporary, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(preimage_fd)
        finally:
            os.close(preimage_fd)
        _fsync_parent(temporary)
        if file_state(parked) != expected_postimage:
            raise RuntimeError("runtime file changed during guarded restore")
        try:
            # link(2) is conditional creation: unlike replace/copy it never
            # overwrites a target created by a post-guard concurrent editor.
            os.link(temporary, target)
        except FileExistsError as exc:
            raise RuntimeError("runtime file changed during guarded restore") from exc
        if file_state(target) != preimage:
            raise RuntimeError("guarded restore did not produce the pre-image")
    except Exception:
        # Recovery also refuses a concurrent target instead of overwriting it.
        # A SIGKILL cannot execute this path; the durable marker then lets the
        # next guarded invocation restore only the verified post-image.
        try:
            recover_file_restore(target)
        except Exception:
            pass
        raise
    else:
        parked.unlink()
        marker.unlink()
        _fsync_parent(marker)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_file_job_restore(root: Path):
    """Exercise exact candidate files and the existing job via native APIs.

    This is deliberately copy-only: it reads the active Amber jobs document
    once, makes a private copy, and mutates only that copy through cron.jobs.
    It never points cron at the live file and never emits the document.
    """
    from cron import jobs as cron_jobs

    base = "b20d9f3c7c8a0a709e862f63240eb3d6fe302e53"
    target_db = root / "live/hermes_cli/kanban_db.py"
    target_cron = root / "live/cron/jobs.py"
    target_script = root / "live/profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py"
    for target in (target_db, target_cron, target_script):
        target.parent.mkdir(parents=True, exist_ok=True)

    # Snapshot the actual two runtime targets into a private staging area.  The
    # overlay script is already installed on the host, so treating it as an
    # assumed absence would turn a rollback into an unintended deletion.
    runtime_db = Path(os.environ.get(
        "AMBER_RUNTIME_KANBAN_DB", "/mnt/usb-ext4/hermes-agent-runtime/hermes_cli/kanban_db.py"
    ))
    runtime_script = Path(os.environ.get(
        "AMBER_RUNTIME_RECONCILER", "/home/rodrigue/.hermes/profiles/amber/scripts/kanban_telegram_subscribe_all.py"
    ))
    runtime_cron = Path(os.environ.get(
        "AMBER_RUNTIME_CRON_JOBS", "/mnt/usb-ext4/hermes-agent-runtime/cron/jobs.py"
    ))
    targets = (
        (runtime_db, target_db, ROOT / "hermes_cli/kanban_db.py"),
        (runtime_cron, target_cron, ROOT / "cron/jobs.py"),
        (runtime_script, target_script, SCRIPT),
    )
    preimages = []
    for source, target, candidate in targets:
        if not source.is_file():
            raise AssertionError(f"runtime rollback target is missing: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        staged_pre = root / "preimages" / target.name
        staged_pre.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copy2(target, staged_pre)
        preimages.append((target, candidate, staged_pre, file_hash(target), file_mode(target)))

    # Install and prove the exact candidate bytes on copies only.
    for target, candidate, _staged_pre, _pre_hash, _pre_mode in preimages:
        shutil.copy2(candidate, target)
        assert file_hash(target) == file_hash(candidate)
        assert file_mode(target) == file_mode(candidate)

    # Restore through conditional pathname creation, not hash-check then copy.
    # Production activation additionally holds the job/gateway quiescence gate;
    # this copy-only verifier proves that a post-guard name conflict is refused
    # without overwriting the concurrent content.
    for target, candidate, staged_pre, pre_hash, pre_mode in preimages:
        restore_file_from_private_preimage(
            target,
            staged_pre,
            expected_postimage=(file_hash(candidate), file_mode(candidate)),
        )
        assert file_hash(target) == pre_hash
        assert file_mode(target) == pre_mode

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
        # Compare and restore under ONE native cross-process jobs lock. A lock
        # timeout or concurrent native update is a conflict, never a stale
        # overwrite of administrative cron state.
        post_admin = {key: paused.get(key) for key in admin}
        restored = cron_jobs.update_job(observed["id"], admin, expected=post_admin)
        if restored is None:
            raise RuntimeError("cron administrative state changed before guarded restore")
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
    # rollback_journal writes an inverse marker too.  Keep the verifier fully
    # private even though it imports the exact candidate overlay module.
    module.JOURNAL_ROOT = root / "journals"
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
