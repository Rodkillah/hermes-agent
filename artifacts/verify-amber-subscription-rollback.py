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
import time
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
        forward_batch_id = "nominal-" + uuid.uuid4().hex
        migrated = module.reconcile(conn, batch_id=forward_batch_id)
        assert migrated["changed"] == 4, migrated
        assert module.kb.get_notify_batch(conn, forward_batch_id)["entry_count"] == 4
        # A notification claim may advance a cursor after apply and is allowed
        # to survive rollback.
        key = dict(task_id=forge, platform="telegram", chat_id="chat", thread_id="thread")
        _, advanced_cursor, claimed = kb.claim_unseen_events_for_sub(conn, **key)
        assert claimed and advanced_cursor > before[forge]["last_event_id"]
        independent_before = dict(kb.list_notify_subs(conn, human)[0])

        restored = module.rollback_batch(conn, forward_batch_id)
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
        second_batch_id = "reapply-" + uuid.uuid4().hex
        reapplied = module.reconcile(conn, batch_id=second_batch_id)
        assert reapplied["changed"] == 4
        assert module.rollback_batch(conn, second_batch_id) == 4
        assert module.rollback_batch(conn, second_batch_id) == 4
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
        forward_batch_id = "conflict-" + uuid.uuid4().hex
        module.reconcile(conn, batch_id=forward_batch_id)
        assert module.kb.get_notify_batch(conn, forward_batch_id)
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
            module.rollback_batch(conn, forward_batch_id)
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


def _publish_manifest(path: Path, payload: dict[str, object]) -> None:
    """Publish a durable receipt while retaining a recovery copy.

    The backup is fsynced before the compatibility write to ``path``.  If a
    process dies after the destination is truncated, the next consumer can
    recover the last complete receipt instead of rebuilding from live state.
    """
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    backup = path.with_name(path.name + ".previous")
    if path.exists() and path.stat().st_size:
        shutil.copy2(path, backup)
        fd = os.open(backup, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_parent(backup)
    # Keep this named-path write so the disposable SIGKILL probe exercises the
    # real publication boundary.  The previous receipt remains recoverable.
    path.write_text(encoded, encoding="utf-8")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_parent(path)
    if backup.exists():
        backup.unlink()
        _fsync_parent(backup)


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        backup = path.with_name(path.name + ".previous")
        if not backup.is_file():
            raise RuntimeError("rollback package manifest is unreadable and has no durable recovery copy") from exc
        payload = json.loads(backup.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("rollback package recovery copy is malformed")
        _publish_manifest(path, payload)
        return payload


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


def _prepare_and_restore_file_job_package(root: Path):
    """Exercise exact candidate files and the existing job via native APIs.

    This is deliberately copy-only: it reads the active Amber jobs document
    once, makes a private copy, and mutates only that copy through cron.jobs.
    It never points cron at the live file and never emits the document.
    """
    from cron import jobs as cron_jobs

    marker_path = _package_marker(root)
    package = _load_manifest(marker_path)
    if package.get("state") != "prepared" or not isinstance(package.get("files"), list):
        raise RuntimeError("rollback package is not a prepared durable package")
    preimages = []
    for item in package["files"]:
        target = root / item["target"]
        staged_pre = root / item["preimage"]
        staged_post = root / item["candidate_image"]
        candidate = Path(item["candidate"])
        if file_state(staged_pre) != (item["pre_hash"], int(item["pre_mode"])):
            raise RuntimeError(f"rollback package pre-image changed: {target.name}")
        if file_state(staged_post) != (item["post_hash"], int(item["post_mode"])):
            raise RuntimeError(f"rollback package post-image changed: {target.name}")
        preimages.append((target, candidate, staged_pre, item["pre_hash"], int(item["pre_mode"])))

    # The package already contains every pre/post image.  No source runtime or
    # live jobs document is read during consumption.
    for target, candidate, _staged_pre, _pre_hash, _pre_mode in preimages:
        shutil.copy2(candidate, target)
        assert file_hash(target) == file_hash(candidate)
        assert file_mode(target) == file_mode(candidate)

    # Keep all candidate copies installed until the guarded native job restore
    # has completed. The production rollback must not depend on an already
    # imported in-memory copy of cron.jobs after its on-disk candidate has been
    # replaced by old code.

    # Consume only the durable private job document from the package.
    original = package["job_preimage"]
    private_cron = root / "private-cron"
    private_jobs = private_cron / "jobs.json"
    if not private_jobs.is_file():
        raise RuntimeError("rollback package job document is missing")
    raw_document = json.loads(private_jobs.read_text(encoding="utf-8"))
    raw_jobs = raw_document.get("jobs", raw_document) if isinstance(raw_document, dict) else raw_document
    original_constants = cron_jobs.CRON_DIR, cron_jobs.JOBS_FILE, cron_jobs.OUTPUT_DIR
    cron_jobs.CRON_DIR = private_cron
    cron_jobs.JOBS_FILE = private_jobs
    cron_jobs.OUTPUT_DIR = private_cron / "output"
    try:
        observed = cron_jobs.get_job("85fcd56ee535")
        assert observed and observed["id"] == "85fcd56ee535"
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
    restored_job = next(job for job in restored_jobs if job.get("id") == "85fcd56ee535")
    assert all(restored_job.get(key) == value for key, value in admin.items())
    assert [job for job in restored_jobs if job.get("id") != "85fcd56ee535"] == [
        job for job in raw_jobs if job.get("id") != "85fcd56ee535"
    ]

    # Only after the guarded job state has returned through the candidate code
    # may the three exact runtime files be conditionally restored to their
    # private pre-images. This is a pathname-CAS, never hash-check then copy.
    for target, candidate, staged_pre, pre_hash, pre_mode in preimages:
        restore_file_from_private_preimage(
            target,
            staged_pre,
            expected_postimage=(file_hash(candidate), file_mode(candidate)),
        )
        assert file_hash(target) == pre_hash
        assert file_mode(target) == pre_mode


def _package_marker(root: Path) -> Path:
    return root / "package-manifest.json"


def _read_job_admin(path: Path, job_id: str) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    jobs = document.get("jobs", document) if isinstance(document, dict) else document
    job = next((item for item in jobs if item.get("id") == job_id), None)
    if not isinstance(job, dict):
        raise RuntimeError(f"rollback package job is missing: {job_id}")
    return {key: job.get(key) for key in ("enabled", "state", "paused_at", "paused_reason", "next_run_at")}


def _resume_existing_file_job_package(root: Path) -> None:
    """Consume a durable package without reading or rebuilding live sources.

    The manifest is a receipt as well as an input.  It is retained after a
    successful consume so an exact replay can prove completion without
    reopening the live jobs document or recopying any runtime file.
    """
    marker_path = _package_marker(root)
    package = _load_manifest(marker_path)
    if package.get("schema_version") != 1 or not package.get("files"):
        raise RuntimeError("rollback package manifest is malformed")
    if package.get("state") == "completed":
        private_jobs = root / "private-cron" / "jobs.json"
        if private_jobs.is_file():
            observed = _read_job_admin(private_jobs, str(package["job_id"]))
            if observed != package["job_preimage"]:
                raise RuntimeError("completed rollback package job was changed")
        for item in package["files"]:
            preimage = root / item["preimage"]
            target = root / item["target"]
            expected = (item.get("pre_hash"), int(item.get("pre_mode")))
            if not preimage.is_file() or file_state(preimage) != expected or not target.is_file() or file_state(target) != expected:
                raise RuntimeError(f"completed rollback package file was changed: {target.name}")
        return
    if package.get("state") != "prepared":
        raise RuntimeError("rollback package manifest is not consumable")
    if not isinstance(package.get("job_preimage"), dict) or not isinstance(package.get("job_postimage"), dict):
        raise RuntimeError("rollback package has no durable job pre/post-images")

    # Validate every durable pre-image before touching the job or any target.
    # Never hash a staged file and write that newly observed hash back into the
    # manifest: that would silently re-baseline an intervention between runs.
    for item in package["files"]:
        preimage = root / item["preimage"]
        if not preimage.is_file():
            raise RuntimeError(f"rollback package pre-image is missing: {preimage.name}")
        expected_pre = (item.get("pre_hash"), item.get("pre_mode"))
        if expected_pre[0] != file_hash(preimage) or int(expected_pre[1]) != file_mode(preimage):
            raise RuntimeError(f"rollback package pre-image changed: {preimage.name}")

    from cron import jobs as cron_jobs
    private_cron = root / "private-cron"
    private_jobs = private_cron / "jobs.json"
    if not private_jobs.is_file():
        raise RuntimeError("rollback package job document is missing")
    original_constants = cron_jobs.CRON_DIR, cron_jobs.JOBS_FILE, cron_jobs.OUTPUT_DIR
    cron_jobs.CRON_DIR = private_cron
    cron_jobs.JOBS_FILE = private_jobs
    cron_jobs.OUTPUT_DIR = private_cron / "output"
    try:
        job_id = str(package["job_id"])
        observed = cron_jobs.get_job(job_id)
        if not observed:
            raise RuntimeError("rollback package job is missing")
        job_preimage = package["job_preimage"]
        job_postimage = package["job_postimage"]
        job_fields = tuple(job_preimage)
        current_admin = {key: observed.get(key) for key in job_fields}
        expected_post = {key: job_postimage.get(key) for key in job_fields}
        expected_pre = {key: job_preimage.get(key) for key in job_fields}
        post_match = current_admin == expected_post
        if not post_match and current_admin.get("enabled") is False and current_admin.get("state") == "paused":
            post_match = all(
                current_admin.get(key) == expected_post.get(key)
                for key in job_fields
                if key != "paused_at"
            )
        if current_admin == expected_pre:
            pass
        elif post_match:
            # paused_at is generated by the native cron helper and is not
            # predictable during preparation; CAS against the observed full
            # post-image, while the package still records the intended shape.
            restored = cron_jobs.update_job(job_id, expected_pre, expected=current_admin)
            if restored is None:
                raise RuntimeError("rollback package job changed concurrently")
        else:
            raise RuntimeError("rollback package job conflict")

        for item in package["files"]:
            target = root / item["target"]
            preimage = root / item["preimage"]
            expected_pre_file = (item["pre_hash"], int(item["pre_mode"]))
            expected_post_file = (item["post_hash"], int(item["post_mode"]))
            # A SIGKILL can leave the target name absent while the helper's
            # durable marker and parked post-image are intact.  Recover that
            # marker before classifying the target; otherwise the recovery
            # evidence is mistaken for a third-party conflict.
            if restore_marker_path(target).exists():
                recover_file_restore(target)
            current = file_state(target) if target.exists() else None
            if current == expected_pre_file:
                continue
            if current != expected_post_file:
                raise RuntimeError(f"rollback package file conflict: {target.name}")
            restore_file_from_private_preimage(target, preimage, expected_postimage=expected_post_file)
            if file_state(target) != expected_pre_file:
                raise RuntimeError(f"rollback package file restore failed: {target.name}")
    finally:
        cron_jobs.CRON_DIR, cron_jobs.JOBS_FILE, cron_jobs.OUTPUT_DIR = original_constants
    package["state"] = "completed"
    package["completed"] = True
    _publish_manifest(marker_path, package)


def _preview_job_postimage(private_jobs: Path, job_id: str) -> dict[str, object]:
    """Derive the intended native pause shape without touching the package job."""
    current = _read_job_admin(private_jobs, job_id)
    current.update({
        "enabled": False,
        "state": "paused",
        "paused_at": int(time.time()),
        "paused_reason": "private rollback verifier",
    })
    return current


def run_file_job_restore(root: Path):
    """Prepare a complete durable package, then consume it."""
    marker = _package_marker(root)
    if marker.is_file():
        _resume_existing_file_job_package(root)
        return
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    live_jobs = Path(os.environ.get("AMBER_PROFILE_JOBS_FILE", "/home/rodrigue/.hermes/profiles/amber/cron/jobs.json"))
    if not live_jobs.is_file():
        raise RuntimeError("Amber jobs document is missing during package preparation")
    job_id = "85fcd56ee535"
    private_cron = root / "private-cron"
    private_cron.mkdir(mode=0o700, exist_ok=True)
    private_jobs = private_cron / "jobs.json"
    shutil.copy2(live_jobs, private_jobs)
    job_admin = _read_job_admin(private_jobs, job_id)
    job_postimage = _preview_job_postimage(private_jobs, job_id)
    candidates = (
        (root / "live/hermes_cli/kanban_db.py", Path(os.environ.get("AMBER_RUNTIME_KANBAN_DB", "/mnt/usb-ext4/hermes-agent-runtime/hermes_cli/kanban_db.py")), ROOT / "hermes_cli/kanban_db.py"),
        (root / "live/cron/jobs.py", Path(os.environ.get("AMBER_RUNTIME_CRON_JOBS", "/mnt/usb-ext4/hermes-agent-runtime/cron/jobs.py")), ROOT / "cron/jobs.py"),
        (root / "live/profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py", Path(os.environ.get("AMBER_RUNTIME_RECONCILER", "/home/rodrigue/.hermes/profiles/amber/scripts/kanban_telegram_subscribe_all.py")), SCRIPT),
    )
    package = {
        "schema_version": 1, "state": "prepared", "job_id": job_id,
        "job_preimage": job_admin, "job_postimage": job_postimage, "files": [],
    }
    for target, runtime_source, candidate in candidates:
        if not runtime_source.is_file() or not candidate.is_file():
            raise RuntimeError(f"rollback package source is missing: {runtime_source}")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(runtime_source, target)
        staged = root / "preimages" / target.name
        staged.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copy2(target, staged)
        staged_post = root / "postimages" / target.name
        staged_post.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copy2(candidate, staged_post)
        package["files"].append({
            "target": str(target.relative_to(root)), "candidate": str(candidate),
            "candidate_image": str(staged_post.relative_to(root)),
            "preimage": str(staged.relative_to(root)), "pre_hash": file_hash(staged),
            "pre_mode": file_mode(staged), "post_hash": file_hash(staged_post),
            "post_mode": file_mode(staged_post),
        })
    _publish_manifest(marker, package)
    _prepare_and_restore_file_job_package(root)


def run_existing_rollback_package(
    *, root: Path, db: Path, forward_batch_ids: list[str], file_restore_root: Path | None = None
):
    """Resume an existing bounded package without init, seed, or JSON authority.

    ``forward_batch_ids`` are explicit durable inputs.  The SQLite ledger
    decides whether each inverse is still needed or has already committed;
    replay therefore cannot manufacture a new task or reapply a transfer.
    """
    if not root.is_dir() or not db.is_file() or not forward_batch_ids:
        raise RuntimeError("existing rollback package inputs are incomplete")
    private_lock = root / "historical-reconciliation.lock"
    with private_lock.open("a+") as lock_file:
        import fcntl
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        conn = kb.connect(db)
        try:
            for forward_batch_id in forward_batch_ids:
                if module.kb.get_notify_batch(conn, forward_batch_id) is None:
                    raise RuntimeError(f"forward batch is missing: {forward_batch_id}")
                module.rollback_batch(conn, forward_batch_id)
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
        if file_restore_root is not None:
            marker = _package_marker(file_restore_root)
            if not marker.is_file():
                # Backward-compatible fixture path for pre-R12 callers.  New
                # operational paths must prepare explicitly and are fail-closed.
                if file_restore_root.name not in {"runtime", "runtime-package"}:
                    raise RuntimeError("existing rollback package is missing; prepare it explicitly first")
                run_file_job_restore(file_restore_root)
                return
            _resume_existing_file_job_package(file_restore_root)


def run_full_rollback_recipe(root: Path):
    """Fixture generator plus a separate real entry over its existing package."""
    root.mkdir(mode=0o700)
    db = root / "kanban.db"
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
        forward_batch_id = "full-" + uuid.uuid4().hex
        applied = module.reconcile(conn, batch_id=forward_batch_id)
        assert applied["changed"] == 4
        key = dict(task_id=forge, platform="telegram", chat_id="chat", thread_id="thread")
        _, advanced_cursor, claimed = kb.claim_unseen_events_for_sub(conn, **key)
        assert claimed and advanced_cursor > before[forge]["last_event_id"]
    finally:
        conn.close()
    run_file_job_restore(root / "runtime")
    # Replay the exact same package: no seed, transfer, or second inverse.
    run_existing_rollback_package(root=root, db=db, forward_batch_ids=[forward_batch_id])
    conn = kb.connect(db)
    try:
        assert kb.list_notify_subs(conn, created) == []
        assert kb.list_notify_subs(conn, forge)[0]["last_event_id"] == advanced_cursor
        assert dict(kb.list_notify_subs(conn, human)[0]) == before[human]
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()


with tempfile.TemporaryDirectory(prefix="amber-subscription-rollback-") as temp:
    root = Path(temp)
    run_full_rollback_recipe(root / "complete")
    # The separate conflict fixture verifies that the ordered happy-path recipe
    # above does not weaken the fail-closed concurrent-owner guard.
    module.JOURNAL_ROOT = root / "conflict-journals"
    run_conflict(root / "conflict.db")

print(json.dumps({
    "rollback_recipe": "locked recovery -> native inverse -> guarded job -> files",
    "concurrent_conflict": "rejected without overwrite",
    "quick_check": "ok",
}, sort_keys=True))
print("targeted Amber rollback verifier: PASS")
