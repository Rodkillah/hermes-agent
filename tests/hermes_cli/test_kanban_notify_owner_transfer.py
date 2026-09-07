from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _seed(conn):
    task_id = kb.create_task(conn, title="owner handoff", assignee="worker")
    kb.add_notify_sub(
        conn,
        task_id=task_id,
        platform="telegram",
        chat_id="chat-1",
        thread_id="thread-1",
        user_id="user-exact",
        user_id_alt="alt-exact",
        chat_type="group",
        notifier_profile="forge",
        delivery_mode="notify+wake",
        delivery_metadata={"message_thread_id": 42, "source": "exact"},
    )
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at) "
            "VALUES (?, 'changes_requested', ?, 1234567890)",
            (task_id, json.dumps({"reason": "needs source review"})),
        )
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = 1, created_at = 1234560000 "
            "WHERE task_id = ?",
            (task_id,),
        )
    return task_id


def _sub(conn, task_id):
    return kb.list_notify_subs(conn, task_id)[0]


def test_transfer_is_atomic_cas_and_preserves_subscription_exactly(kanban_home):
    conn = kb.connect()
    try:
        task_id = _seed(conn)
        before = dict(_sub(conn, task_id))

        assert kb.transfer_notify_sub_owner(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="thread-1",
            expected_owner="forge",
            new_owner="amber",
        ) is True
        after = _sub(conn, task_id)
    finally:
        conn.close()

    assert after["notifier_profile"] == "amber"
    for field in (
        "task_id", "platform", "chat_id", "thread_id", "user_id", "user_id_alt",
        "chat_type", "delivery_mode", "delivery_metadata", "created_at", "last_event_id",
    ):
        assert after[field] == before[field], field


def test_transfer_rejects_unexpected_owner_without_mutation(kanban_home):
    conn = kb.connect()
    try:
        task_id = _seed(conn)
        before = dict(_sub(conn, task_id))
        assert kb.transfer_notify_sub_owner(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="thread-1",
            expected_owner="other",
            new_owner="amber",
        ) is False
        assert dict(_sub(conn, task_id)) == before
    finally:
        conn.close()


def test_transfer_retry_is_idempotent_and_rollback_does_not_rewind_cursor(kanban_home):
    conn = kb.connect()
    try:
        task_id = _seed(conn)
        key = dict(
            task_id=task_id, platform="telegram", chat_id="chat-1", thread_id="thread-1"
        )
        assert kb.transfer_notify_sub_owner(
            conn, **key, expected_owner="forge", new_owner="amber"
        ) is True
        moved = dict(_sub(conn, task_id))
        assert kb.transfer_notify_sub_owner(
            conn, **key, expected_owner="forge", new_owner="amber"
        ) is True
        assert dict(_sub(conn, task_id)) == moved

        # Inverse CAS is the real rollback path. It changes only the owner.
        assert kb.transfer_notify_sub_owner(
            conn, **key, expected_owner="amber", new_owner="forge"
        ) is True
        rolled_back = _sub(conn, task_id)
    finally:
        conn.close()

    assert rolled_back["notifier_profile"] == "forge"
    assert rolled_back["last_event_id"] == 1
    assert rolled_back["created_at"] == 1234560000
    assert rolled_back["user_id"] == "user-exact"
    assert rolled_back["user_id_alt"] == "alt-exact"


def test_transfer_rolls_back_owner_when_transaction_aborts(kanban_home):
    conn = kb.connect()
    try:
        task_id = _seed(conn)
        conn.execute(
            """
            CREATE TRIGGER abort_owner_transfer
            BEFORE UPDATE OF notifier_profile ON kanban_notify_subs
            BEGIN
                SELECT RAISE(ABORT, 'handoff abort');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="handoff abort"):
            kb.transfer_notify_sub_owner(
                conn,
                task_id=task_id,
                platform="telegram",
                chat_id="chat-1",
                thread_id="thread-1",
                expected_owner="forge",
                new_owner="amber",
            )
        assert _sub(conn, task_id)["notifier_profile"] == "forge"
        assert _sub(conn, task_id)["last_event_id"] == 1
    finally:
        conn.close()


def test_transfer_does_not_lose_new_event_and_cursor_remains_claimable(kanban_home):
    conn = kb.connect()
    try:
        task_id = _seed(conn)
        key = dict(
            task_id=task_id, platform="telegram", chat_id="chat-1", thread_id="thread-1"
        )
        assert kb.transfer_notify_sub_owner(
            conn, **key, expected_owner="forge", new_owner="amber"
        ) is True
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events(task_id, kind, payload, created_at) "
                "VALUES (?, 'changes_requested', ?, 1234567891)",
                (task_id, json.dumps({"reason": "second event"})),
            )
        old_cursor, new_cursor, events = kb.claim_unseen_events_for_sub(conn, **key)
        owner = _sub(conn, task_id)["notifier_profile"]
    finally:
        conn.close()

    assert owner == "amber"
    assert old_cursor == 1
    assert new_cursor > old_cursor
    assert [event.kind for event in events] == ["changes_requested", "changes_requested"]
    assert events[-1].payload["reason"] == "second event"
