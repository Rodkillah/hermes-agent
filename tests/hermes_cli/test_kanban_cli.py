"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------



def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert f"Task {child_id}: child task" in output
    assert f"parents:   {parent_id}" in output
    assert "Cannot operate on a closed database" not in output


def test_cli_show_and_diagnostics_read_normalized_production_probes(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="failed production probe", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'prod' WHERE id = ?", (task_id,))
            conn.execute(
                """INSERT INTO production_receipts
                (id, task_id, schema_version, environment, target, deployed_at_utc,
                 candidate_sha, deployed_identity_kind, deployed_identity_value,
                 derivation_ref, backup_ref, rollback_ref, verification_mode, actor,
                 idempotency_key, evidence_sha256, created_at)
                VALUES (?, ?, 1, 'test', 'local', '2026-08-30T10:00:00+00:00',
                        ?, 'git_sha', ?, NULL, 'backup-ref', 'rollback-ref',
                        'system_verified', 'worker', ?, 'evidence', 100)""",
                ("receipt-1", task_id, "a" * 40, "a" * 40, "idem-1"),
            )
            conn.execute(
                """INSERT INTO production_probes
                (receipt_id, ordinal, name, scope, required, result, evidence_ref)
                VALUES (?, 0, 'health', 'live_production', 1, 'failed', 'probe-ref')""",
                ("receipt-1",),
            )

    diagnostics = json.loads(kc.run_slash(f"diagnostics --task {task_id} --json"))
    kinds = {item["kind"] for item in diagnostics[0]["diagnostics"]}
    assert "prod_failed_required_probe" in kinds
    assert "prod_without_receipt" not in kinds

    fleet = json.loads(kc.run_slash("diagnostics --json"))
    fleet_item = next(item for item in fleet if item["task_id"] == task_id)
    fleet_kinds = {item["kind"] for item in fleet_item["diagnostics"]}
    assert "prod_failed_required_probe" in fleet_kinds
    assert "prod_without_receipt" not in fleet_kinds

    show = kc.run_slash(f"show {task_id}")
    assert "Production promotion has a failed required probe" in show


def test_kanban_show_json_event_id_drives_block_loop_resolution(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="loop", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            kb._append_event(
                conn,
                tid,
                "block_loop_detected",
                {"source_status": "ready", "reason": "repeat"},
            )

    detail = json.loads(kc.run_slash(f"show {tid} --json"))
    detection = [e for e in detail["events"] if e["kind"] == "block_loop_detected"][-1]
    assert isinstance(detection["id"], int)

    output = kc.run_slash(
        f"resolve-block-loop {tid} archive --expected-event-id {detection['id']} "
        "--reason superseded"
    )
    assert f"Resolved {tid}: archive → archived" in output


def test_kanban_show_text_includes_event_id(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="loop", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            kb._append_event(
                conn,
                tid,
                "block_loop_detected",
                {"source_status": "ready", "reason": "repeat"},
            )
        event = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"][-1]

    output = kc.run_slash(f"show {tid}")
    assert f"id={event.id}" in output
    assert "block_loop_detected" in output


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kb.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kb.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------


