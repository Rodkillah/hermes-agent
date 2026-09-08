from __future__ import annotations

import importlib.util
import json
import os
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py"


@pytest.fixture(autouse=True)
def private_journal_root(monkeypatch, tmp_path):
    """Every module import, including rollback tests, must stay off Amber's profile."""
    monkeypatch.setenv(
        "HERMES_KANBAN_JOURNAL_ROOT", str(tmp_path / "private-journals")
    )


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


def test_guarded_file_restore_refuses_post_guard_concurrent_runtime_edit(monkeypatch, tmp_path):
    """The real restore primitive must not reduce to hash-check then copy."""
    namespace = runpy.run_path(str(ROOT / "artifacts/verify-amber-subscription-rollback.py"))
    restore = namespace["restore_file_from_private_preimage"]
    target = tmp_path / "live" / "kanban_db.py"
    target.parent.mkdir()
    target.write_bytes(b"candidate post-image\n")
    target.chmod(0o644)
    staged = tmp_path / "preimages" / "kanban_db.py"
    staged.parent.mkdir()
    staged.write_bytes(b"pre-activation image\n")
    staged.chmod(0o640)
    expected = (namespace["file_hash"](target), namespace["file_mode"](target))
    concurrent = b"synthetic concurrent runtime edit\n"
    original_link = namespace["os"].link
    injected = []

    def link(source, destination, *args, **kwargs):
        if Path(source).name.startswith(".kanban_db.py.restore-") and Path(source).name != ".kanban_db.py.restore-state.json" and Path(destination) == target:
            target.write_bytes(concurrent)
            target.chmod(0o600)
            injected.append(target)
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(namespace["os"], "link", link)
    with pytest.raises(RuntimeError, match="changed during guarded restore"):
        restore(target, staged, expected_postimage=expected)
    assert injected
    assert target.read_bytes() == concurrent
    assert target.stat().st_mode & 0o777 == 0o600


def test_guarded_file_restore_refuses_race_between_last_check_and_parking(monkeypatch, tmp_path):
    namespace = runpy.run_path(str(ROOT / "artifacts/verify-amber-subscription-rollback.py"))
    restore = namespace["restore_file_from_private_preimage"]
    target = tmp_path / "live" / "kanban_db.py"
    target.parent.mkdir()
    target.write_bytes(b"candidate post-image\n")
    target.chmod(0o644)
    staged = tmp_path / "preimages" / "kanban_db.py"
    staged.parent.mkdir()
    staged.write_bytes(b"pre-activation image\n")
    staged.chmod(0o640)
    expected = (namespace["file_hash"](target), namespace["file_mode"](target))
    concurrent = b"synthetic pre-parking concurrent edit\n"
    original_replace = namespace["os"].replace
    injected = []

    def replace(source, destination, *args, **kwargs):
        if Path(source) == target:
            target.write_bytes(concurrent)
            target.chmod(0o600)
            injected.append(target)
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(namespace["os"], "replace", replace)
    with pytest.raises(RuntimeError, match="changed during guarded restore"):
        restore(target, staged, expected_postimage=expected)
    assert injected
    assert target.read_bytes() == concurrent
    assert target.stat().st_mode & 0o777 == 0o600
    assert namespace["restore_marker_path"](target).exists()


def test_guarded_file_restore_recovers_interruption_between_parking_and_link(monkeypatch, tmp_path):
    namespace = runpy.run_path(str(ROOT / "artifacts/verify-amber-subscription-rollback.py"))
    restore = namespace["restore_file_from_private_preimage"]
    recover = namespace["recover_file_restore"]
    marker_path = namespace["restore_marker_path"]
    target = tmp_path / "live" / "kanban_db.py"
    target.parent.mkdir()
    candidate = b"candidate post-image\n"
    target.write_bytes(candidate)
    target.chmod(0o644)
    staged = tmp_path / "preimages" / "kanban_db.py"
    staged.parent.mkdir()
    staged.write_bytes(b"pre-activation image\n")
    staged.chmod(0o640)
    expected = (namespace["file_hash"](target), namespace["file_mode"](target))
    original_replace = namespace["os"].replace

    def interrupt_after_park(source, destination, *args, **kwargs):
        result = original_replace(source, destination, *args, **kwargs)
        if Path(source) == target:
            raise KeyboardInterrupt("synthetic interruption after parking")
        return result

    monkeypatch.setattr(namespace["os"], "replace", interrupt_after_park)
    with pytest.raises(KeyboardInterrupt, match="after parking"):
        restore(target, staged, expected_postimage=expected)
    assert not target.exists()
    assert marker_path(target).exists()
    assert recover(target) == "restored-postimage"
    assert target.read_bytes() == candidate
    assert target.stat().st_mode & 0o777 == 0o644
    assert not marker_path(target).exists()


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
    monkeypatch.setattr(mod, "JOURNAL_ROOT", tmp_path / "private-journals")
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
        f"m.JOURNAL_ROOT=__import__('pathlib').Path({str(tmp_path / 'private-journals')!r}); "
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


def test_reconcile_captures_preimage_after_outer_transaction(monkeypatch, db):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        seed_task(kb, conn, owner="amber")
        forge = seed_task(kb, conn, owner="forge", mode="notify")
        original_plan = mod.plan

        def plan_after_begin(*args, **kwargs):
            planned = original_plan(*args, **kwargs)
            kb.add_notify_sub(
                conn, task_id=forge, platform="telegram", chat_id="chat",
                thread_id="thread", delivery_mode="wake",
            )
            return planned

        monkeypatch.setattr(mod, "plan", plan_after_begin)
        journal = []
        mod.reconcile(conn, journal=journal)
        entry = next(item for item in journal if item["task_id"] == forge)
        mod.rollback_journal(conn, journal)
        restored = kb.list_notify_subs(conn, forge)[0]
    finally:
        conn.close()

    assert entry["pre_image"]["delivery_mode"] == "wake"
    assert restored["delivery_mode"] == "wake"


def test_prepare_failure_rolls_back_without_unjournaled_commit(monkeypatch, db):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        seed_task(kb, conn, owner="amber")
        forge = seed_task(kb, conn, owner="forge")
        with pytest.raises(OSError, match="journal disk full"):
            mod.reconcile(
                conn,
                prepare_journal=lambda entries: (_ for _ in ()).throw(OSError("journal disk full")),
            )
        row = kb.list_notify_subs(conn, forge)[0]
    finally:
        conn.close()
    assert row["notifier_profile"] == "forge"


def test_main_private_batches_are_non_overwriting_and_recover_markers(monkeypatch, db, tmp_path, capsys):
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
    monkeypatch.setattr(mod, "JOURNAL_ROOT", tmp_path / "private-journals")
    export = tmp_path / "legacy-export.json"

    assert mod.main(["--journal", str(export)]) == 0
    first = json.loads(export.read_text())
    batches = list((tmp_path / "private-journals").iterdir())
    assert len(first) == len(batches) == 1
    prepared = batches[0] / "prepared.json"
    assert (batches[0] / "committed.json").is_file()
    assert prepared.stat().st_mode & 0o777 == 0o600
    assert batches[0].stat().st_mode & 0o777 == 0o700

    assert mod.main(["--journal", str(export)]) == 0
    assert json.loads(export.read_text()) == first
    assert len(list((tmp_path / "private-journals").iterdir())) == 1

    (batches[0] / "committed.json").unlink()
    conn = kb.connect(db)
    try:
        mod.BatchJournal.recover_pending(conn, tmp_path / "private-journals")
    finally:
        conn.close()
    assert (batches[0] / "committed.json").is_file()
    assert capsys.readouterr().err == ""


def test_same_second_recreate_gets_new_generation_and_refuses_inverse(monkeypatch, db):
    mod = load_script(monkeypatch)
    kb = mod.kb
    conn = kb.connect(db)
    try:
        seed_task(kb, conn, owner="amber")
        created = seed_task(kb, conn)
        journal = []
        mod.reconcile(conn, journal=journal)
        post = kb.list_notify_subs(conn, created)[0]
        kb.remove_notify_sub(conn, task_id=created, platform="telegram", chat_id="chat", thread_id="thread")
        kb.add_notify_sub(
            conn, task_id=created, platform="telegram", chat_id="chat", thread_id="thread",
            user_id=post["user_id"], user_id_alt=post["user_id_alt"], chat_type=post["chat_type"],
            notifier_profile=post["notifier_profile"], delivery_mode=post["delivery_mode"],
            delivery_metadata=post["delivery_metadata"],
        )
        recreated = kb.list_notify_subs(conn, created)[0]
        with pytest.raises(RuntimeError, match="rollback conflict"):
            mod.rollback_journal(conn, journal)
        after = kb.list_notify_subs(conn, created)[0]
    finally:
        conn.close()
    assert recreated["created_at"] == post["created_at"]
    assert recreated["subscription_generation"] != post["subscription_generation"]
    assert after == recreated
