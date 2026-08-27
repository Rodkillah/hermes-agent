"""Tests for the explicit block-loop triage decision transition."""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="task"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _escalated_task(conn, title="task"):
    tid = _running_task(conn, title)
    assert kb.block_task(conn, tid, reason="missing input", kind="capability")
    assert kb.unblock_task(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    assert kb.block_task(conn, tid, reason="missing input", kind="capability")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_generic_triage_cannot_use_block_loop_resolution(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="raw idea", assignee="worker", triage=True)
        assert not kb.resolve_block_loop_task(
            conn, tid, decision="retry", actor="amber", reason="not this path"
        )
        assert kb.get_task(conn, tid).status == "triage"


def test_retry_preserves_loop_memory_and_parent_gates(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        tid = _escalated_task(conn)
        kb.link_tasks(conn, parent, tid)
        event = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"][-1]

        assert kb.resolve_block_loop_task(
            conn, tid, decision="retry", actor="amber", reason="input was corrected",
            expected_event_id=event.id,
        )
        task = kb.get_task(conn, tid)
        assert task.status == "todo"
        assert task.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
        resolved = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_resolved"][-1]
        assert resolved.payload["decision"] == "retry"
        assert resolved.payload["actor"] == "amber"


def test_resolution_refuses_stale_provenance_cas(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _escalated_task(conn)
        event = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"][-1]
        assert not kb.resolve_block_loop_task(
            conn,
            tid,
            decision="archive",
            actor="amber",
            reason="superseded",
            expected_event_id=event.id + 1,
        )
        assert kb.get_task(conn, tid).status == "triage"


def test_complete_resets_memory_and_releases_dependants(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _escalated_task(conn)
        child = kb.create_task(conn, title="dependent", assignee="worker")
        kb.link_tasks(conn, tid, child)

        assert kb.resolve_block_loop_task(
            conn, tid, decision="complete", actor="amber", reason="accepted",
            summary="The existing result satisfies the acceptance criteria.",
        )
        assert kb.get_task(conn, tid).status == "done"
        assert kb.get_task(conn, tid).block_recurrences == 0
        assert kb.get_task(conn, child).status == "ready"
        assert [e for e in kb.list_events(conn, tid) if e.kind == "completed"]
        run = kb.latest_run(conn, tid)
        assert run is not None and run.outcome == "completed"


def test_complete_preserves_parent_gating(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="open parent", assignee="worker")
        tid = _escalated_task(conn)
        kb.link_tasks(conn, parent, tid)

        assert not kb.resolve_block_loop_task(
            conn,
            tid,
            decision="complete",
            actor="amber",
            reason="accepted",
            summary="handoff",
        )
        assert kb.get_task(conn, tid).status == "triage"


def test_archive_does_not_release_dependants(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _escalated_task(conn)
        child = kb.create_task(conn, title="dependent", assignee="worker")
        kb.link_tasks(conn, tid, child)

        assert kb.resolve_block_loop_task(
            conn, tid, decision="archive", actor="amber", reason="superseded"
        )
        assert kb.get_task(conn, tid).status == "archived"
        assert kb.get_task(conn, child).status == "todo"
        resolved = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_resolved"][-1]
        assert resolved.payload["decision"] == "archive"
        assert [e for e in kb.list_events(conn, tid) if e.kind == "archived"]


def test_resolution_requires_handoff_for_complete(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _escalated_task(conn)
        with pytest.raises(ValueError, match="handoff"):
            kb.resolve_block_loop_task(
                conn, tid, decision="complete", actor="amber", reason="accepted"
            )
        assert kb.get_task(conn, tid).status == "triage"


def test_resolution_refuses_an_orphaned_active_run(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _escalated_task(conn)
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', 1)",
                (tid,),
            )
        assert not kb.resolve_block_loop_task(
            conn, tid, decision="archive", actor="amber", reason="superseded"
        )
        assert kb.get_task(conn, tid).status == "triage"


def test_retry_restores_review_phase_when_loop_started_in_review(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="review loop", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        assert kb.claim_review_task(conn, tid, claimer="reviewer") is not None
        assert kb.block_task(conn, tid, reason="review input", kind="capability")
        assert kb.unblock_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        assert kb.claim_review_task(conn, tid, claimer="reviewer") is not None
        assert kb.block_task(conn, tid, reason="review input", kind="capability")
        assert kb.get_task(conn, tid).status == "triage"

        assert kb.resolve_block_loop_task(
            conn, tid, decision="retry", actor="amber", reason="review input fixed"
        )
        assert kb.get_task(conn, tid).status == "review"
