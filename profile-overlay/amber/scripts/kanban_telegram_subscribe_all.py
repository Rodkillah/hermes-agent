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
# Hermes Amber runs on Linux. Some Python builds omit these GNU/Linux flags
# even though the kernel supports them; use the stable ABI values rather than
# silently dropping the no-follow protection.
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0o200000)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0o400000)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0o4000)
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


def _open_safe_directory(path: Path) -> int:
    """Open an already-created absolute directory one component at a time.

    Operations below use the resulting descriptor instead of resolving the
    journal pathname again.  A later rename can therefore neither redirect a
    write through a replacement symlink nor make us follow a marker symlink.
    """
    absolute = path.absolute()
    fd = os.open(absolute.anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for part in absolute.parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("journal path is not a directory")
        return fd
    except Exception:
        os.close(fd)
        raise


def _same_directory_entry(parent_fd: int, name: str, child_fd: int) -> bool:
    """True only while ``name`` still names the directory opened by ``child_fd``."""
    try:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    child = os.fstat(child_fd)
    return (
        stat.S_ISDIR(entry.st_mode)
        and entry.st_dev == child.st_dev
        and entry.st_ino == child.st_ino
    )


def _read_fd_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 64 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_private_json_at(
    parent_fd: int,
    name: str,
    payload: Mapping[str, Any],
    *,
    exclusive: bool,
    anchor_fd: int | None = None,
    anchor_name: str | None = None,
    domain_parent_fd: int | None = None,
    domain_name: str | None = None,
    domain_fd: int | None = None,
) -> None:
    """Durably publish one JSON record through anchored directory handles."""
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("journal write made no progress")
            offset += written
        os.fsync(fd)
    finally:
        if fd >= 0:
            os.close(fd)
    def attached() -> bool:
        if anchor_fd is not None and anchor_name is not None and not _same_directory_entry(
            anchor_fd, anchor_name, parent_fd
        ):
            return False
        return not (
            domain_parent_fd is not None
            and domain_name is not None
            and domain_fd is not None
            and not _same_directory_entry(domain_parent_fd, domain_name, domain_fd)
        )

    published_identity: tuple[int, int] | None = None
    detached_after_publish = False
    try:
        if not attached():
            raise RuntimeError("journal recovery domain detached before private publication")
        if exclusive:
            os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            os.unlink(temporary, dir_fd=parent_fd)
        else:
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.chmod(name, 0o600, dir_fd=parent_fd, follow_symlinks=False)
        published = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(published.st_mode):
            raise RuntimeError("journal marker publication is unsafe")
        published_identity = (published.st_dev, published.st_ino)
        os.fsync(parent_fd)
        # A held directory descriptor prevents symlink redirection. A
        # same-UID rename can nevertheless detach the recovery root after the
        # final publication syscall; detect it after the durability boundary,
        # remove only our inode through the old handle, and let the SQLite
        # transaction roll back before COMMIT.
        if not attached():
            detached_after_publish = True
            raise RuntimeError("journal recovery domain detached during private publication")
    except Exception:
        if detached_after_publish and published_identity is not None:
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == published_identity:
                    os.unlink(name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except (FileNotFoundError, OSError):
                pass
        try:
            os.unlink(temporary, dir_fd=parent_fd)
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


_IMAGE_FIELDS = (
    "task_id", "platform", "chat_id", "thread_id", "user_id", "user_id_alt",
    "chat_type", "notifier_profile", "delivery_mode", "delivery_metadata",
    "subscription_generation", "created_at", "last_event_id",
)


def _validate_image(image: Any, *, label: str) -> dict[str, Any]:
    """Reject partial or ambiguous recovery images before touching SQLite."""
    if not isinstance(image, dict) or any(field not in image for field in _IMAGE_FIELDS):
        raise RuntimeError(f"{label} is not a complete subscription image")
    generation = image["subscription_generation"]
    if not isinstance(generation, str) or len(generation) != 32 or any(
        char not in "0123456789abcdef" for char in generation
    ):
        raise RuntimeError(f"{label} has invalid subscription generation")
    if not all(isinstance(image[field], str) and image[field] for field in ("task_id", "platform", "chat_id")):
        raise RuntimeError(f"{label} has invalid subscription key")
    if image["thread_id"] is None or not isinstance(image["created_at"], int) or not isinstance(image["last_event_id"], int):
        raise RuntimeError(f"{label} has invalid subscription cursor")
    return image


def _read_regular_json_at(parent_fd: int, name: str, *, label: str) -> tuple[dict[str, Any], bytes]:
    """Read a marker by descriptor, never resolving its parent again."""
    try:
        # A FIFO/device can block at open(2), before a post-open fstat has a
        # chance to reject it.  Open non-blocking, then accept regular files
        # only by the descriptor actually obtained.
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} is missing") from exc
    except OSError as exc:
        # ELOOP, permission failures and special-file open failures are unsafe
        # recovery evidence, never an absent batch.
        raise RuntimeError(f"{label} cannot be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"{label} is unsafe")
        raw = _read_fd_all(fd)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} is malformed")
    return value, raw


def _marker_exists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _load_batch_record_at(batch_fd: int, batch_id: str) -> tuple[dict[str, Any], bool]:
    """Validate a whole durable record before treating it as resolved."""
    try:
        payload, raw = _read_regular_json_at(batch_fd, "prepared.json", label="journal preparation")
    except RuntimeError as exc:
        if str(exc) != "journal preparation is missing":
            raise
        if _marker_exists_at(batch_fd, "committed.json") or _marker_exists_at(batch_fd, "aborted.json"):
            raise RuntimeError("journal marker has no prepared record")
        return {}, False
    if payload.get("schema_version") != 1 or payload.get("batch_id") != batch_id:
        raise RuntimeError("journal preparation identity is malformed")
    if payload.get("phase") not in {"forward", "inverse"}:
        raise RuntimeError("journal preparation phase is malformed")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("journal preparation entries are malformed")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError("journal entry is malformed")
        post = _validate_image(entry.get("post_image"), label=f"journal post-image {index}")
        pre = entry.get("pre_image")
        if pre is not None:
            pre = _validate_image(pre, label=f"journal pre-image {index}")
            for field in _IMAGE_FIELDS:
                if field in {"notifier_profile", "delivery_mode", "last_event_id"}:
                    continue
                if pre[field] != post[field]:
                    raise RuntimeError("journal images identify different subscription incarnations")
    resolved = False
    if _marker_exists_at(batch_fd, "committed.json"):
        marker, _ = _read_regular_json_at(batch_fd, "committed.json", label="journal committed marker")
        if (
            marker.get("schema_version") != 1
            or marker.get("batch_id") != batch_id
            or marker.get("prepared_sha256") != hashlib.sha256(raw).hexdigest()
        ):
            raise RuntimeError("journal committed marker does not match preparation")
        resolved = True
    if _marker_exists_at(batch_fd, "aborted.json"):
        marker, _ = _read_regular_json_at(batch_fd, "aborted.json", label="journal aborted marker")
        if marker.get("batch_id") != batch_id:
            raise RuntimeError("journal aborted marker does not match preparation")
        if resolved:
            raise RuntimeError("journal has conflicting terminal markers")
        resolved = True
    return payload, resolved


class BatchJournal:
    """Crash-recoverable private journal for one mutative reconciliation batch."""

    def __init__(self, root: Path, *, phase: str = "forward"):
        self.root = root.absolute()
        _private_directory(self.root)
        self.root_parent = self.root.parent
        self.root_parent_fd = _open_safe_directory(self.root_parent)
        self.root_name = self.root.name
        try:
            self.root_fd = os.open(
                self.root_name,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                dir_fd=self.root_parent_fd,
            )
        except Exception:
            os.close(self.root_parent_fd)
            raise
        if not _same_directory_entry(self.root_parent_fd, self.root_name, self.root_fd):
            os.close(self.root_fd)
            os.close(self.root_parent_fd)
            raise RuntimeError("journal recovery root detached during open")
        self.batch_id = uuid.uuid4().hex
        self.path = self.root / self.batch_id
        try:
            os.mkdir(self.batch_id, 0o700, dir_fd=self.root_fd)
            self.batch_fd = os.open(
                self.batch_id,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                dir_fd=self.root_fd,
            )
        except Exception:
            os.close(self.root_fd)
            os.close(self.root_parent_fd)
            raise
        os.fsync(self.root_fd)
        self.phase = phase
        self.prepared_path = self.path / "prepared.json"
        self.committed_path = self.path / "committed.json"

    def _assert_attached(self) -> None:
        if not _same_directory_entry(self.root_parent_fd, self.root_name, self.root_fd):
            raise RuntimeError("journal recovery root detached from parent")
        if not _same_directory_entry(self.root_fd, self.batch_id, self.batch_fd):
            raise RuntimeError("journal batch detached from root")

    def _read(self, name: str, *, label: str) -> tuple[dict[str, Any], bytes]:
        self._assert_attached()
        try:
            # See _read_regular_json_at: do not let a hostile FIFO retain the
            # process-wide reconciliation lock before descriptor validation.
            fd = os.open(
                name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=self.batch_fd
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"{label} is missing") from exc
        except OSError as exc:
            raise RuntimeError(f"{label} cannot be opened safely") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError(f"{label} is unsafe")
            raw = _read_fd_all(fd)
        finally:
            os.close(fd)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{label} is malformed") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"{label} is malformed")
        return value, raw

    def prepare(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        _write_private_json_at(
            self.batch_fd,
            "prepared.json",
            {"schema_version": 1, "batch_id": self.batch_id, "phase": self.phase, "entries": entries},
            exclusive=True,
            anchor_fd=self.root_fd,
            anchor_name=self.batch_id,
            domain_parent_fd=self.root_parent_fd,
            domain_name=self.root_name,
            domain_fd=self.root_fd,
        )

    def mark_committed(self) -> None:
        payload, raw = self._read("prepared.json", label="journal preparation")
        if payload.get("batch_id") != self.batch_id:
            raise RuntimeError("journal preparation identity is malformed")
        _write_private_json_at(
            self.batch_fd,
            "committed.json",
            {
                "schema_version": 1,
                "batch_id": self.batch_id,
                "prepared_sha256": hashlib.sha256(raw).hexdigest(),
            },
            exclusive=True,
            anchor_fd=self.root_fd,
            anchor_name=self.batch_id,
            domain_parent_fd=self.root_parent_fd,
            domain_name=self.root_name,
            domain_fd=self.root_fd,
        )

    @staticmethod
    def recover_pending(conn: sqlite3.Connection, root: Path) -> None:
        """Resolve an interrupted marker write conservatively before a new pass."""
        if not root.exists():
            return
        _private_directory(root)
        root_fd = _open_safe_directory(root)
        try:
            with kb.write_txn(conn):
                for name in sorted(os.listdir(root_fd)):
                    try:
                        batch_fd = os.open(
                            name,
                            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                            dir_fd=root_fd,
                        )
                    except OSError as exc:
                        raise RuntimeError("unsafe journal batch entry") from exc
                    try:
                        if not _same_directory_entry(root_fd, name, batch_fd):
                            raise RuntimeError("journal batch detached from root")
                        data, resolved = _load_batch_record_at(batch_fd, name)
                        if not data or resolved:
                            continue
                        entries = data["entries"]
                        post = pre = True
                        for entry in entries:
                            image = entry["post_image"]
                            rows = _fetch_target_raw(conn, image)
                            current = rows[0] if len(rows) == 1 else None
                            post = post and _image_matches(current, image)
                            pre = pre and _image_matches(current, entry.get("pre_image"))
                        phase = data["phase"]
                        applied = post if phase == "forward" else pre
                        unapplied = pre if phase == "forward" else post
                        if applied:
                            _prepared, raw = _read_regular_json_at(
                                batch_fd, "prepared.json", label="journal preparation"
                            )
                            _write_private_json_at(
                                batch_fd,
                                "committed.json",
                                {
                                    "schema_version": 1,
                                    "batch_id": data["batch_id"],
                                    "prepared_sha256": hashlib.sha256(raw).hexdigest(),
                                },
                                exclusive=True,
                                anchor_fd=root_fd,
                                anchor_name=name,
                            )
                        elif unapplied:
                            _write_private_json_at(
                                batch_fd,
                                "aborted.json",
                                {"schema_version": 1, "batch_id": data["batch_id"]},
                                exclusive=True,
                                anchor_fd=root_fd,
                                anchor_name=name,
                            )
                        else:
                            raise RuntimeError("journal recovery is ambiguous; refusing a new batch")
                    finally:
                        os.close(batch_fd)
        finally:
            os.close(root_fd)


def _validate_export_path(path: Path) -> None:
    parent = path.parent
    if parent.exists() and not parent.is_dir():
        raise RuntimeError("journal parent is not a directory")
    parent.mkdir(parents=True, exist_ok=True)
    # The export is replaceable compatibility output, never recovery input.
    # Do not follow or read an existing pathname while the reconciliation lock
    # is held: a FIFO would block the durable SQLite result indefinitely.
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(current.st_mode):
        raise RuntimeError("journal export path is unsafe")


def _append_export(path: Path, entries: list[dict[str, Any]], *, batch_id: str) -> None:
    """Regenerate one non-authoritative compatibility export after COMMIT.

    Replacing the requested export with its uniquely identified batch avoids a
    read of untrusted existing JSON, so a FIFO/symlink race cannot retain the
    reconciliation lock.  The authoritative ledger remains untouched if this
    best-effort publication fails.
    """
    if not entries:
        return
    _validate_export_path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (json.dumps(entries, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _export_batch_entries(conn: sqlite3.Connection, batch_id: str) -> list[dict[str, Any]]:
    """Regenerate a compatibility export from the committed SQLite ledger."""
    exported: list[dict[str, Any]] = []
    for entry in kb.list_notify_batch_entries(conn, batch_id):
        exported.append({
            "schema_version": 1,
            "batch_id": batch_id,
            "action": entry["action"],
            "pre_image": json.loads(entry["pre_image_json"]) if entry["pre_image_json"] else None,
            "post_image": json.loads(entry["post_image_json"]),
        })
    return exported


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


def _forward_request_digest(limit: int) -> str:
    """Stable request identity independent of post-application DB state."""
    request = {"board": BOARD, "operation": "amber-notify-wake-v1", "limit": limit}
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def rollback_batch(conn: sqlite3.Connection, forward_batch_id: str, *, batch_id: str | None = None) -> int:
    """Inverse a persisted forward batch; caller-provided JSON is never authority."""
    inverse_id = batch_id or hashlib.sha256(
        f"amber-notify-wake-inverse-v1:{forward_batch_id}".encode()
    ).hexdigest()
    request = {"board": BOARD, "operation": "amber-notify-wake-inverse-v1", "forward_batch_id": forward_batch_id}
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return kb.inverse_notify_batch(
        conn,
        forward_batch_id=forward_batch_id,
        inverse_batch_id=inverse_id,
        board=BOARD,
        request_digest=digest,
    )


def rollback_journal(conn: sqlite3.Connection, journal: Iterable[Mapping[str, Any]]) -> int:
    """Reject obsolete caller-authoritative JSON rollback input.

    Compatibility exports remain readable outside this primitive, but neither
    they nor an in-memory list can decide an inverse after a crash.
    """
    del conn, journal
    raise RuntimeError("rollback requires a persisted forward batch_id")


def reconcile(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    limit: int = MAX_BATCH_SIZE,
    batch_id: str | None = None,
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

    if prepare_journal is not None:
        raise RuntimeError("external JSON journal preparation is no longer authoritative")
    batch_id = batch_id or uuid.uuid4().hex
    request_digest = _forward_request_digest(limit)
    pending_journal: list[dict[str, Any]] = []
    # One outer transaction is deliberate: if any CAS/read-back fails, every
    # transfer, mode repair, and new row in this batch is rolled back.  The
    # plan and every pre-image are captured only after BEGIN IMMEDIATE, so a
    # native writer cannot make a stale pre-image rollback its own update.
    with kb.write_txn(conn):
        existing = kb.get_notify_batch(conn, batch_id)
        if existing is not None:
            if existing["request_digest"] != request_digest or existing["phase"] != "forward":
                raise RuntimeError("notify batch id was reused with a different request")
            if existing["state"] == "reverted":
                raise RuntimeError("notify batch is reverted; refusing to reapply it")
            stored = json.loads(existing["result_json"])
            if not isinstance(stored, dict):
                raise RuntimeError("notify batch result is malformed")
            return {key: int(value) for key, value in stored.items()}
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
        # The authoritative ledger row and every exact image are inserted in
        # this same BEGIN IMMEDIATE transaction as subscription mutations.
        # There is no prepared filesystem state whose attachment must survive
        # until COMMIT.
        kb.record_notify_batch(
            conn,
            batch_id=batch_id,
            board=BOARD,
            phase="forward",
            request_digest=request_digest,
            result=result,
            entries=pending_journal,
        )
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
    parser.add_argument("--batch-id", help="stable opaque ID for one idempotent forward batch")
    args = parser.parse_args(argv)
    batch_id: str | None = None
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
                # The optional compatibility export is deliberately after the
                # durable SQLite commit. Its failure is surfaced as a warning,
                # never recast as an uncommitted subscription batch.
                batch_id = args.batch_id or uuid.uuid4().hex
                with kb.connect(DB_PATH, board=BOARD) as conn:
                    result = reconcile(
                        conn,
                        dry_run=False,
                        limit=args.limit,
                        batch_id=batch_id,
                    )
                    committed_journal = _export_batch_entries(conn, batch_id)
                if args.journal is not None:
                    try:
                        _append_export(args.journal, committed_journal, batch_id=batch_id)
                    except (OSError, RuntimeError, TypeError, ValueError) as export_error:
                        result["export_failed"] = 1
                        print(f"kanban_subscription_export_warning: {export_error}", file=sys.stderr)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as error:
        # ``write_txn`` can surface an exception after SQLite has committed
        # (for example a post-COMMIT invariant check).  Reopen the configured
        # trusted DB and classify the durable batch before reporting failure;
        # never retry or infer state from the compatibility export.  An unreadable
        # DB remains explicitly unknown and fail-closed.
        if not args.dry_run and batch_id:
            try:
                with kb.connect(DB_PATH, board=BOARD) as probe:
                    durable = kb.get_notify_batch(probe, batch_id)
                if durable is not None:
                    stored = json.loads(durable["result_json"])
                    print(json.dumps({
                        "batch_id": batch_id,
                        "state": durable["state"],
                        "result": stored,
                        "warning": "post_commit_exception_reclassified",
                    }, sort_keys=True), file=sys.stderr)
                    return 1
            except Exception as probe_error:
                print(
                    f"kanban_subscription_unknown: durable batch {batch_id} "
                    f"could not be read after error: {probe_error}",
                    file=sys.stderr,
                )
        print(f"kanban_subscription_error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
