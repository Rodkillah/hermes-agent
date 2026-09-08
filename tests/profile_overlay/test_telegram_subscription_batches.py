from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py"


def load_script(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME", str(ROOT))
    spec = importlib.util.spec_from_file_location("amber_subscription_batches", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_task(kb, conn, *, owner: str | None = None):
    task_id = kb.create_task(conn, title="subscription batch", assignee="worker")
    if owner is not None:
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat",
            thread_id="thread",
            user_id="user",
            user_id_alt="alt",
            chat_type="dm",
            notifier_profile=owner,
            delivery_mode="notify+wake",
            delivery_metadata={"source": "batch-test"},
        )
    return task_id


@pytest.fixture
def db(tmp_path):
    from hermes_cli import kanban_db as kb

    path = tmp_path / "kanban.db"
    kb.init_db(path)
    return path


def test_forward_batch_is_atomic_replayable_and_keeps_images_immutable(monkeypatch, db):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        seed_task(kb, conn, owner="amber")
        forge = seed_task(kb, conn, owner="forge")
        result = mod.reconcile(conn, batch_id="f" * 32)
        replay = mod.reconcile(conn, batch_id="f" * 32)
        batch = kb.get_notify_batch(conn, "f" * 32)
        entries = kb.list_notify_batch_entries(conn, "f" * 32)
        assert result == replay
        assert batch["state"] == "committed"
        assert batch["phase"] == "forward"
        assert batch["entry_count"] == 1
        assert len(entries) == 1
        assert json.loads(entries[0]["pre_image_json"])["notifier_profile"] == "forge"
        assert json.loads(entries[0]["post_image_json"])["notifier_profile"] == "amber"
        with pytest.raises(Exception):
            conn.execute("UPDATE kanban_notify_batch_entries SET action = 'repair' WHERE batch_id = ?", ("f" * 32,))
        assert kb.list_notify_subs(conn, forge)[0]["notifier_profile"] == "amber"
    finally:
        conn.close()


def test_inverse_uses_persisted_forward_and_replay_never_reapplies(monkeypatch, db):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        seed_task(kb, conn, owner="amber")
        forge = seed_task(kb, conn, owner="forge")
        mod.reconcile(conn, batch_id="a" * 32)
        inverse = mod.rollback_batch(conn, "a" * 32, batch_id="b" * 32)
        replay = mod.rollback_batch(conn, "a" * 32, batch_id="b" * 32)
        forward = kb.get_notify_batch(conn, "a" * 32)
        reverse = kb.get_notify_batch(conn, "b" * 32)
        assert inverse == replay == 1
        assert forward["state"] == "reverted"
        assert reverse["phase"] == "inverse"
        assert reverse["inverse_of"] == "a" * 32
        assert kb.list_notify_subs(conn, forge)[0]["notifier_profile"] == "forge"
        with pytest.raises(RuntimeError, match="reverted"):
            mod.reconcile(conn, batch_id="a" * 32)
    finally:
        conn.close()


def test_batch_migration_rejects_preexisting_incompatible_schema(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb

    path = tmp_path / "incompatible.db"
    raw = __import__("sqlite3").connect(path)
    raw.execute("CREATE TABLE kanban_notify_batches (batch_id TEXT PRIMARY KEY, junk TEXT)")
    raw.commit()
    raw.close()
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    with pytest.raises(RuntimeError, match="notify batch.*schema"):
        kb.init_db(path)


def test_inverse_mixed_creation_absence_matches_each_forward_ordinal(monkeypatch, db):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        seed_task(kb, conn, owner="amber")
        absent = seed_task(kb, conn)
        present = seed_task(kb, conn)
        mod.reconcile(conn, batch_id="mixed-forward")
        kb.remove_notify_sub(conn, task_id=absent, platform="telegram", chat_id="chat", thread_id="thread")
        assert mod.rollback_batch(conn, "mixed-forward") == 2
        assert kb.list_notify_subs(conn, absent) == []
        assert kb.list_notify_subs(conn, present) == []
        assert kb.get_notify_batch(conn, "mixed-forward")["state"] == "reverted"
    finally:
        conn.close()


def test_authoritative_images_reject_non_sqlite_origin_values(monkeypatch, db):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        task = seed_task(kb, conn, owner="forge")
        mod.reconcile(conn, batch_id="origin-forward")
        post = dict(kb.list_notify_subs(conn, task)[0])
        pre = dict(post, notifier_profile="forge", delivery_mode="notify+wake", user_id=["not text"])
        with pytest.raises(ValueError, match="non-SQLite user_id"):
            kb.record_notify_batch(
                conn, batch_id="invalid-origin", board=mod.BOARD, phase="forward",
                request_digest="invalid-origin", result={},
                entries=[{"action": "transfer", "pre_image": pre, "post_image": post}],
            )
    finally:
        conn.close()
