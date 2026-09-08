from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py"


def load_script(monkeypatch):
    # Make the candidate hermes_cli the explicit runtime for this test; never
    # accidentally import the live runtime while testing the overlay.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME", str(ROOT))
    spec = importlib.util.spec_from_file_location("amber_subscribe", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_task(kb, conn, *, owner: str | None = None, mode: str | None = None, chat_id: str = "chat"):
    task_id = kb.create_task(conn, title=f"task {owner or 'new'}", assignee="worker")
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
            delivery_metadata={"source": "anchor", "thread": 7},
        )
    return task_id


def add_unread_event(kb, conn, task_id: str, reason: str):
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'changes_requested', ?, 123)",
            (task_id, json.dumps({"reason": reason})),
        )
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = 0 WHERE task_id = ?",
            (task_id,),
        )


@pytest.fixture
def db(tmp_path):
    from hermes_cli import kanban_db as kb

    path = tmp_path / "kanban.db"
    kb.init_db(path)
    return path


def test_reconcile_transfers_repairs_creates_and_preserves_human_and_cursor(monkeypatch, db):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        anchor = seed_task(kb, conn, owner="amber")
        forge = seed_task(kb, conn, owner="forge")
        notify_only = seed_task(kb, conn, owner="amber", mode="notify")
        new_task = seed_task(kb, conn)
        human = seed_task(kb, conn, owner="human", chat_id="human-chat")
        for task_id, reason in ((forge, "rejected"), (notify_only, "review"), (new_task, "new")):
            add_unread_event(kb, conn, task_id, reason)
        before_forge = dict(kb.list_notify_subs(conn, forge)[0])
        before_notify = dict(kb.list_notify_subs(conn, notify_only)[0])
        before_human = dict(kb.list_notify_subs(conn, human)[0])

        result = mod.reconcile(conn)
        rows = {task_id: kb.list_notify_subs(conn, task_id) for task_id in (anchor, forge, notify_only, new_task, human)}
    finally:
        conn.close()

    assert result == {"active": 5, "already_subscribed": 1, "missing": 4, "this_run": 4, "changed": 4, "remaining": 0}
    assert rows[forge][0]["notifier_profile"] == "amber"
    assert rows[forge][0]["delivery_mode"] == "notify+wake"
    assert rows[forge][0]["last_event_id"] == before_forge["last_event_id"] == 0
    assert rows[forge][0]["delivery_metadata"] == before_forge["delivery_metadata"]
    assert rows[notify_only][0]["notifier_profile"] == "amber"
    assert rows[notify_only][0]["delivery_mode"] == "notify+wake"
    assert rows[notify_only][0]["last_event_id"] == before_notify["last_event_id"] == 0
    assert rows[human][0] == before_human
    # New subscriptions are intentionally caught up at creation, while the
    # existing unread event remains claimable after transfer/repair.
    assert rows[new_task][0]["notifier_profile"] == "amber"
    assert rows[new_task][0]["last_event_id"] > 0


def test_conflicting_human_or_unknown_destination_is_fail_closed(monkeypatch, db):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        seed_task(kb, conn, owner="amber")
        human = seed_task(kb, conn, owner="human")
        with pytest.raises(RuntimeError, match="Unexpected subscription owner"):
            mod.reconcile(conn)
        assert kb.list_notify_subs(conn, human)[0]["notifier_profile"] == "human"
    finally:
        conn.close()


def test_one_failure_rolls_back_entire_batch_without_rewinding_or_deleting_data(monkeypatch, db):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        seed_task(kb, conn, owner="amber")
        first = seed_task(kb, conn, owner="forge")
        second = seed_task(kb, conn, owner="forge")
        add_unread_event(kb, conn, first, "first")
        add_unread_event(kb, conn, second, "second")
        with kb.write_txn(conn):
            conn.execute(
                f"""
                CREATE TRIGGER abort_second_transfer
                BEFORE UPDATE OF notifier_profile ON kanban_notify_subs
                WHEN OLD.task_id = '{second}'
                BEGIN SELECT RAISE(ABORT, 'second transfer abort'); END
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="second transfer abort"):
            mod.reconcile(conn)
        rows = {task_id: kb.list_notify_subs(conn, task_id)[0] for task_id in (first, second)}
    finally:
        conn.close()

    assert rows[first]["notifier_profile"] == "forge"
    assert rows[second]["notifier_profile"] == "forge"
    assert rows[first]["last_event_id"] == 0
    assert rows[second]["last_event_id"] == 0


def test_explicit_db_scope_ignores_poisoned_kanban_environment(monkeypatch, db, tmp_path, capsys):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        seed_task(kb, conn, owner="amber")
        seed_task(kb, conn, owner="forge")
    finally:
        conn.close()
    monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setattr(mod, "LOCK_PATH", tmp_path / "amber.lock")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "poison.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "wrong-board")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "wrong-task")

    journal = tmp_path / "journal.json"
    assert mod.main(["--journal", str(journal)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["changed"] == 1
    entries = json.loads(journal.read_text())
    assert len(entries) == 1
    assert entries[0]["post_image"]["notifier_profile"] == "amber"
    assert not (tmp_path / "poison.db").exists()


def test_two_real_processes_are_serialized_by_existing_lock(monkeypatch, db, tmp_path):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        seed_task(kb, conn, owner="amber")
        seed_task(kb, conn, owner="forge")
    finally:
        conn.close()

    code = (
        "import importlib.util, os; "
        f"p=importlib.util.spec_from_file_location('s', {str(SCRIPT)!r}); "
        "m=importlib.util.module_from_spec(p); p.loader.exec_module(m); "
        f"m.DB_PATH=__import__('pathlib').Path({str(db)!r}); "
        f"m.LOCK_PATH=__import__('pathlib').Path({str(tmp_path / 'same.lock')!r}); "
        "raise SystemExit(m.main([]))"
    )
    env = os.environ.copy()
    env.update({
        "HERMES_AGENT_RUNTIME": str(ROOT),
        "HERMES_KANBAN_DB": str(tmp_path / "poison.db"),
        "HERMES_KANBAN_BOARD": "wrong-board",
        "HERMES_KANBAN_TASK": "wrong-task",
    })
    first = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first_out, first_err = first.communicate(timeout=30)
    second_out, second_err = second.communicate(timeout=30)
    results = [(first.returncode, first_out, first_err), (second.returncode, second_out, second_err)]

    assert sorted(result[0] for result in results) == [0, 75]
    conn = kb.connect(db)
    try:
        rows = kb.list_notify_subs(conn)
    finally:
        conn.close()
    assert sum(row["notifier_profile"] == "amber" for row in rows) == 2


def test_compatible_missing_origin_ids_use_deterministic_anchor_without_rewriting_existing(monkeypatch, db):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        known = seed_task(kb, conn, owner="amber")
        legacy = seed_task(kb, conn, owner="forge")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE kanban_notify_subs SET user_id = NULL, user_id_alt = NULL "
                "WHERE task_id = ?", (legacy,)
            )
        before = dict(kb.list_notify_subs(conn, legacy)[0])
        result = mod.reconcile(conn)
        rows = {task: kb.list_notify_subs(conn, task)[0] for task in (known, legacy)}
    finally:
        conn.close()
    assert result["changed"] == 1
    assert rows[legacy]["notifier_profile"] == "amber"
    assert rows[legacy]["user_id"] is None
    assert rows[legacy]["user_id_alt"] is None
    assert rows[legacy]["delivery_metadata"] == before["delivery_metadata"]


def test_conflicting_nonempty_origins_fail_closed_without_delta(monkeypatch, db):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        first = seed_task(kb, conn, owner="amber")
        second = seed_task(kb, conn, owner="forge")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE kanban_notify_subs SET user_id = 'different' WHERE task_id = ?",
                (second,),
            )
        before = [dict(row) for row in kb.list_notify_subs(conn)]
        with pytest.raises(RuntimeError, match="Conflicting existing Telegram DM origin"):
            mod.reconcile(conn)
        assert [dict(row) for row in kb.list_notify_subs(conn)] == before
    finally:
        conn.close()
