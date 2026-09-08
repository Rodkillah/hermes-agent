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

# The package is deliberately narrow: it is a rollback receipt for exactly the
# four files and five administrative fields that this candidate owns.  Keep
# these contracts explicit so a partial/duplicate manifest cannot be mistaken
# for a complete rollback package.
_ROLLBACK_TARGETS = frozenset({
    "live/hermes_cli/kanban_db.py",
    "live/cron/jobs.py",
    "live/cron/scheduler.py",
    "live/profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py",
})
_ROLLBACK_JOB_ID = "85fcd56ee535"
_JOB_ADMIN_FIELDS = (
    "enabled",
    "state",
    "paused_at",
    "paused_reason",
    "next_run_at",
)
_JOB_ADMIN_FIELD_SET = frozenset(_JOB_ADMIN_FIELDS)
_FILE_IMAGE_FIELDS = frozenset({
    "target",
    "preimage",
    "candidate_image",
    "pre_hash",
    "pre_mode",
    "post_hash",
    "post_mode",
})

os.environ["HERMES_AGENT_RUNTIME"] = str(ROOT)
sys.path.insert(0, str(ROOT))

# Keep the verifier's fixture installation on the native API even when a
# contract probe wraps cron.jobs.update_job to observe the consume order.
from cron import jobs as _cron_jobs
NATIVE_UPDATE_JOB = _cron_jobs.update_job

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


def _fsync_file(path: Path) -> None:
    """Flush one verified regular file before publishing its manifest."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RuntimeError(f"rollback package file is not regular: {path}")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_manifest_atomic(path: Path, encoded: str) -> None:
    """Write and publish a manifest without truncating the live receipt."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        data = encoded.encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write publishing rollback manifest")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_parent(temporary)
    os.replace(temporary, path)
    _fsync_parent(path)


def _publish_manifest(path: Path, payload: dict[str, object]) -> None:
    """Publish a durable receipt, retaining the last valid receipt for recovery.

    The old primary is copied and fsynced before the new payload is atomically
    replaced into place.  A crash before or during replacement therefore leaves
    either the old primary or the durable ``.previous`` copy intact; recovery
    never promotes a partial primary over that last valid receipt.
    """
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    backup = path.with_name(path.name + ".previous")
    previous = None
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            previous = None
    if isinstance(previous, dict):
        backup_tmp = backup.with_name(f".{backup.name}.tmp-{uuid.uuid4().hex}")
        shutil.copyfile(path, backup_tmp)
        _fsync_file(backup_tmp)
        _fsync_parent(backup_tmp)
        os.replace(backup_tmp, backup)
        _fsync_parent(backup)
    _write_manifest_atomic(path, encoded)


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("rollback package manifest is not an object")
        return payload
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        backup = path.with_name(path.name + ".previous")
        if not backup.is_file():
            raise RuntimeError("rollback package manifest is unreadable and has no durable recovery copy") from exc
        try:
            payload = json.loads(backup.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as backup_exc:
            raise RuntimeError("rollback package recovery copy is malformed") from backup_exc
        if not isinstance(payload, dict):
            raise RuntimeError("rollback package recovery copy is malformed")
        # Do not call _publish_manifest here: that would move the damaged
        # primary over the only valid recovery copy.  Atomic replacement keeps
        # the valid backup available if recovery is interrupted.
        _write_manifest_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
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
    if not os.path.lexists(marker):
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
    job_postimage = package.get("job_postimage")
    if not isinstance(job_postimage, dict):
        raise RuntimeError("rollback package has no durable job post-image")
    preimages = []
    for item in package["files"]:
        target = root / item["target"]
        staged_pre = root / item["preimage"]
        staged_post = root / item["candidate_image"]
        if file_state(staged_pre) != (item["pre_hash"], int(item["pre_mode"])):
            raise RuntimeError(f"rollback package pre-image changed: {target.name}")
        if file_state(staged_post) != (item["post_hash"], int(item["post_mode"])):
            raise RuntimeError(f"rollback package post-image changed: {target.name}")
        # The candidate path is provenance only.  Consumption must use the
        # durable, validated post-image copied into this package.
        preimages.append((target, staged_post, staged_pre, item["pre_hash"], int(item["pre_mode"])))

    # The package already contains every pre/post image.  No source runtime or
    # live jobs document is read during consumption.
    for target, staged_post, _staged_pre, _pre_hash, _pre_mode in preimages:
        shutil.copy2(staged_post, target)
        assert file_hash(target) == file_hash(staged_post)
        assert file_mode(target) == file_mode(staged_post)

    # Keep all durable post-images installed until the guarded native job
    # restore has completed. The production rollback must not depend on an
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
        paused_at = job_postimage.get("paused_at")
        if not isinstance(paused_at, str):
            raise RuntimeError("rollback package job post-image has no typed paused_at")
        paused = cron_jobs.pause_job(
            observed["id"],
            reason="private rollback verifier",
            paused_at=paused_at,
        )
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
    # may the four exact runtime files be conditionally restored to their
    # private pre-images. This is a pathname-CAS, never hash-check then copy.
    for target, staged_post, staged_pre, pre_hash, pre_mode in preimages:
        restore_file_from_private_preimage(
            target,
            staged_pre,
            expected_postimage=(file_hash(staged_post), file_mode(staged_post)),
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
    if any(key not in job for key in _JOB_ADMIN_FIELDS):
        raise RuntimeError(f"rollback package job has incomplete admin fields: {job_id}")
    return {key: job[key] for key in _JOB_ADMIN_FIELDS}


def _validate_job_admin_image(image: object, label: str) -> dict[str, object]:
    """Validate the complete, typed administrative image of the native job."""
    if not isinstance(image, dict) or set(image) != _JOB_ADMIN_FIELD_SET:
        raise RuntimeError(f"rollback package {label} has incomplete job fields")
    if type(image["enabled"]) is not bool:
        raise RuntimeError(f"rollback package {label}.enabled has the wrong type")
    if not isinstance(image["state"], str):
        raise RuntimeError(f"rollback package {label}.state has the wrong type")
    for field in ("paused_at", "paused_reason", "next_run_at"):
        value = image[field]
        if value is not None and not isinstance(value, str):
            raise RuntimeError(f"rollback package {label}.{field} has the wrong type")
    return image


def _validate_file_image_entry(item: object) -> tuple[Path, Path, Path, tuple[str, int], tuple[str, int]]:
    """Validate one immutable file image entry and return its typed paths/states."""
    if not isinstance(item, dict) or not _FILE_IMAGE_FIELDS <= set(item):
        raise RuntimeError("rollback package file entry is incomplete")
    try:
        target_rel = Path(item["target"])
        pre_rel = Path(item["preimage"])
        post_rel = Path(item["candidate_image"])
        pre_hash = item["pre_hash"]
        post_hash = item["post_hash"]
        pre_mode = item["pre_mode"]
        post_mode = item["post_mode"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("rollback package file entry is incomplete") from exc
    if any(path.is_absolute() or ".." in path.parts for path in (target_rel, pre_rel, post_rel)):
        raise RuntimeError("rollback package file entry escapes its root")
    if not all(isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
               for value in (pre_hash, post_hash)):
        raise RuntimeError("rollback package file hash is not a typed SHA-256")
    if any(type(value) is not int or not 0 <= value <= 0o777 for value in (pre_mode, post_mode)):
        raise RuntimeError("rollback package file mode is not a typed mode")
    return target_rel, pre_rel, post_rel, (pre_hash, pre_mode), (post_hash, post_mode)


def _marker_state(target: Path, marker: Path, expected_pre: tuple[str, int], expected_post: tuple[str, int]) -> str:
    """Classify a durable file marker without changing anything on disk."""
    try:
        if not os.path.lexists(marker) or not stat.S_ISREG(marker.lstat().st_mode):
            raise ValueError("restore marker is not a regular file")
        state = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("restore marker is not an object")
        parked_name = state["parked"]
        marker_post = tuple(state["postimage"])
        marker_pre = tuple(state["preimage"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("rollback file restore marker is malformed") from exc
    if (
        not isinstance(parked_name, str)
        or Path(parked_name).name != parked_name
        or marker_post != expected_post
        or marker_pre != expected_pre
        or len(marker_post) != 2
        or len(marker_pre) != 2
        or type(marker_post[0]) is not str
        or type(marker_post[1]) is not int
        or type(marker_pre[0]) is not str
        or type(marker_pre[1]) is not int
    ):
        raise RuntimeError(f"rollback file restore marker is invalid: {target.name}")
    parked = target.with_name(parked_name)
    target_present = os.path.lexists(target)
    parked_present = os.path.lexists(parked)
    target_state = file_state(target) if target_present else None
    parked_state = file_state(parked) if parked_present else None
    # These are exactly the four durable boundaries produced by the helper:
    # marker-only, parked, linked, and completed cleanup.  Every other pair is
    # ambiguous and must remain fail-closed.
    if target_state == expected_post and not parked_present:
        return "not-started"
    if target_state is None and parked_state == expected_post:
        return "parked"
    if target_state == expected_pre and parked_state == expected_post:
        return "linked"
    if target_state == expected_pre and not parked_present:
        return "completed-cleanup"
    raise RuntimeError(f"rollback file restore marker has ambiguous state: {target.name}")


def _validate_existing_file_job_package(root: Path) -> dict[str, object]:
    """Validate the complete durable package before any inverse effect.

    Consumption is intentionally distinct from preparation.  Every manifest
    image, the private job document, and every current target is known before
    the SQLite inverse is allowed to run.  Marker states are classified without
    mutation; recovery remains in the ordered inverse -> job -> files phase.
    """
    root = Path(root)
    if not root.is_dir():
        raise RuntimeError("existing rollback package directory is missing")
    package = _load_manifest(_package_marker(root))
    if package.get("schema_version") != 1 or package.get("state") not in {"prepared", "completed"}:
        raise RuntimeError("rollback package manifest is not consumable")
    files = package.get("files")
    if not isinstance(files, list) or len(files) != len(_ROLLBACK_TARGETS):
        raise RuntimeError("rollback package must contain exactly four file images")
    if package.get("job_id") != _ROLLBACK_JOB_ID:
        raise RuntimeError("rollback package job identity is outside the owned scope")
    job_preimage = _validate_job_admin_image(package.get("job_preimage"), "job_preimage")
    job_postimage = _validate_job_admin_image(package.get("job_postimage"), "job_postimage")
    if set(job_preimage) != set(job_postimage):
        raise RuntimeError("rollback package job images have different fields")

    private_jobs = root / "private-cron" / "jobs.json"
    if not private_jobs.is_file():
        raise RuntimeError("rollback package job document is missing")
    try:
        document = json.loads(private_jobs.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("rollback package job document is unreadable") from exc
    raw_jobs = document.get("jobs", document) if isinstance(document, dict) else document
    if not isinstance(raw_jobs, list):
        raise RuntimeError("rollback package job document is malformed")
    matching_jobs = [
        item for item in raw_jobs
        if isinstance(item, dict) and item.get("id") == _ROLLBACK_JOB_ID
    ]
    if len(matching_jobs) != 1:
        raise RuntimeError("rollback package job is missing")
    job = matching_jobs[0]
    if any(key not in job for key in _JOB_ADMIN_FIELDS):
        raise RuntimeError("rollback package job has incomplete observed admin fields")
    current_admin = _validate_job_admin_image(
        {key: job[key] for key in _JOB_ADMIN_FIELDS}, "observed job"
    )
    expected_pre = {key: job_preimage[key] for key in _JOB_ADMIN_FIELDS}
    expected_post = {key: job_postimage[key] for key in _JOB_ADMIN_FIELDS}
    if current_admin not in (expected_pre, expected_post):
        raise RuntimeError("rollback package job conflict")

    seen_targets: set[str] = set()
    seen_preimages: set[str] = set()
    seen_postimages: set[str] = set()
    for item in files:
        target_rel, pre_rel, post_rel, expected_pre_file, expected_post_file = _validate_file_image_entry(item)
        target_key, pre_key, post_key = str(target_rel), str(pre_rel), str(post_rel)
        if target_key not in _ROLLBACK_TARGETS:
            raise RuntimeError(f"rollback package target is not one of the four owned files: {target_key}")
        if target_key in seen_targets or pre_key in seen_preimages or post_key in seen_postimages:
            raise RuntimeError("rollback package contains duplicate file images")
        seen_targets.add(target_key)
        seen_preimages.add(pre_key)
        seen_postimages.add(post_key)
        target = root / target_rel
        preimage = root / pre_rel
        postimage = root / post_rel
        try:
            pre_state = file_state(preimage)
            post_state = file_state(postimage)
        except FileNotFoundError as exc:
            raise RuntimeError(f"rollback package image is missing: {exc.filename}") from exc
        if pre_state != expected_pre_file:
            raise RuntimeError(f"rollback package pre-image changed: {preimage.name}")
        if post_state != expected_post_file:
            raise RuntimeError(f"rollback package post-image changed: {postimage.name}")
        restore_marker = restore_marker_path(target)
        if os.path.lexists(restore_marker):
            _marker_state(target, restore_marker, expected_pre_file, expected_post_file)
        elif os.path.lexists(target):
            current = file_state(target)
            if current not in (expected_pre_file, expected_post_file):
                raise RuntimeError(f"rollback package file conflict: {target.name}")
        else:
            raise RuntimeError(f"rollback package target is missing: {target.name}")
    if seen_targets != _ROLLBACK_TARGETS:
        raise RuntimeError("rollback package does not cover exactly the four owned files")
    return package


def _resume_existing_file_job_package(root: Path) -> None:
    """Consume a durable package without reading or rebuilding live sources.

    The manifest is a receipt as well as an input.  It is retained after a
    successful consume so an exact replay can prove completion without
    reopening the live jobs document or recopying any runtime file.
    """
    marker_path = _package_marker(root)
    package = _validate_existing_file_job_package(root)
    if package.get("state") == "completed":
        private_jobs = root / "private-cron" / "jobs.json"
        observed = _validate_job_admin_image(
            _read_job_admin(private_jobs, str(package["job_id"])),
            "completed observed job",
        )
        expected_pre = _validate_job_admin_image(package["job_preimage"], "job_preimage")
        if observed != expected_pre:
            raise RuntimeError("completed rollback package job was changed")
        for item in package["files"]:
            preimage = root / item["preimage"]
            target = root / item["target"]
            expected = (item["pre_hash"], item["pre_mode"])
            if file_state(preimage) != expected or file_state(target) != expected:
                raise RuntimeError(f"completed rollback package file was changed: {target.name}")
        return
    if package.get("state") != "prepared":
        raise RuntimeError("rollback package manifest is not consumable")
    # The validator above has already required the complete typed job images.
    job_preimage = package["job_preimage"]
    job_postimage = package["job_postimage"]

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
        observed = _read_job_admin(private_jobs, job_id)
        if not observed:
            raise RuntimeError("rollback package job is missing")
        job_preimage = _validate_job_admin_image(package["job_preimage"], "job_preimage")
        job_postimage = _validate_job_admin_image(package["job_postimage"], "job_postimage")
        # Read the private document directly: get_job() normalizes display
        # fields and would turn an invalid stored observation into an approved
        # image before the native CAS gets to inspect it.
        current_admin = _validate_job_admin_image(
            _read_job_admin(private_jobs, job_id),
            "observed job",
        )
        expected_post = dict(job_postimage)
        expected_pre = dict(job_preimage)
        if current_admin == expected_pre:
            pass
        elif current_admin == expected_post:
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
            if os.path.lexists(restore_marker_path(target)):
                recover_file_restore(target)
            current = file_state(target) if os.path.lexists(target) else None
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
    """Build the exact typed native pause image without mutating the package."""
    from cron import jobs as cron_jobs

    current = _read_job_admin(private_jobs, job_id)
    paused_at = cron_jobs._hermes_now().isoformat()
    current.update({
        "enabled": False,
        "state": "paused",
        "paused_at": paused_at,
        "paused_reason": "private rollback verifier",
    })
    return current


def run_file_job_restore(root: Path, *, consume: bool = True):
    """Prepare a complete durable package, optionally consuming it."""
    marker = _package_marker(root)
    if marker.is_file():
        if consume:
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
    _fsync_file(private_jobs)
    _fsync_parent(private_jobs)
    job_admin = _read_job_admin(private_jobs, job_id)
    job_postimage = _preview_job_postimage(private_jobs, job_id)
    candidates = (
        (root / "live/hermes_cli/kanban_db.py", Path(os.environ.get("AMBER_RUNTIME_KANBAN_DB", "/mnt/usb-ext4/hermes-agent-runtime/hermes_cli/kanban_db.py")), ROOT / "hermes_cli/kanban_db.py"),
        (root / "live/cron/jobs.py", Path(os.environ.get("AMBER_RUNTIME_CRON_JOBS", "/mnt/usb-ext4/hermes-agent-runtime/cron/jobs.py")), ROOT / "cron/jobs.py"),
        (root / "live/cron/scheduler.py", Path(os.environ.get("AMBER_RUNTIME_CRON_SCHEDULER", "/mnt/usb-ext4/hermes-agent-runtime/cron/scheduler.py")), ROOT / "cron/scheduler.py"),
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
        _fsync_file(target)
        _fsync_parent(target)
        staged = root / "preimages" / target.name
        staged.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copy2(target, staged)
        _fsync_file(staged)
        _fsync_parent(staged)
        staged_post = root / "postimages" / target.name
        staged_post.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copy2(candidate, staged_post)
        _fsync_file(staged_post)
        _fsync_parent(staged_post)
        package["files"].append({
            "target": str(target.relative_to(root)), "candidate": str(candidate),
            "candidate_image": str(staged_post.relative_to(root)),
            "preimage": str(staged.relative_to(root)), "pre_hash": file_hash(staged),
            "pre_mode": file_mode(staged), "post_hash": file_hash(staged_post),
            "post_mode": file_mode(staged_post),
        })
    _publish_manifest(marker, package)
    if consume:
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
    if file_restore_root is not None:
        # This is a strict consume entry point: a package must already exist
        # and be fully valid before the SQLite inverse is even considered.
        _validate_existing_file_job_package(file_restore_root)
    private_lock = root / "historical-reconciliation.lock"
    with private_lock.open("a+") as lock_file:
        import fcntl
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        if file_restore_root is not None:
            # Revalidate under the package lock to close the preflight-to-effect
            # race without rebuilding or reseeding anything.
            _validate_existing_file_job_package(file_restore_root)
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
    # Prepare and durably publish the complete package before any inverse or
    # file/job effect.  The single existing-package entry then enforces the
    # locked inverse -> job -> files order.
    runtime_package = root / "runtime"
    run_file_job_restore(runtime_package, consume=False)
    package = _load_manifest(_package_marker(runtime_package))
    # The fixture now simulates the already-installed candidate from the
    # durable post-images.  Rollback consumption itself still starts with the
    # ledger inverse and only then restores the guarded job and files.
    files = package.get("files")
    if not isinstance(files, list):
        raise RuntimeError("rollback package files are missing")
    for item in files:
        target = runtime_package / item["target"]
        staged_post = runtime_package / item["candidate_image"]
        shutil.copy2(staged_post, target)
        _fsync_file(target)
        _fsync_parent(target)
    # Install the exact durable job post-image through the native guarded API;
    # the nominal consume path must exercise this CAS, not a preimage no-op.
    from cron import jobs as cron_jobs
    private_cron = runtime_package / "private-cron"
    private_jobs = private_cron / "jobs.json"
    original_constants = cron_jobs.CRON_DIR, cron_jobs.JOBS_FILE, cron_jobs.OUTPUT_DIR
    cron_jobs.CRON_DIR, cron_jobs.JOBS_FILE, cron_jobs.OUTPUT_DIR = (
        private_cron, private_jobs, private_cron / "output"
    )
    try:
        installed = NATIVE_UPDATE_JOB(
            package["job_id"], package["job_postimage"], expected=package["job_preimage"]
        )
        assert installed is not None
    finally:
        cron_jobs.CRON_DIR, cron_jobs.JOBS_FILE, cron_jobs.OUTPUT_DIR = original_constants
    run_existing_rollback_package(
        root=root,
        db=db,
        forward_batch_ids=[forward_batch_id],
        file_restore_root=runtime_package,
    )
    # Replay the exact same package: no seed, transfer, or second inverse.
    run_existing_rollback_package(
        root=root,
        db=db,
        forward_batch_ids=[forward_batch_id],
        file_restore_root=runtime_package,
    )
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
