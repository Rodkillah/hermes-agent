from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban import _cmd_complete, _cmd_request_review


@pytest.fixture
def cli_goal_task(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="goal", body="acceptance", assignee="builder", goal_mode=True)
        claimed = kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    return tid


def _review_metadata():
    return {"review_readiness": {
        "schema_version": 1, "candidate_kind": "artifact",
        "candidate_identity": "sha256:abc", "candidate_location": "attachments/a.pdf",
        "verification": [{"check": "render", "result": "ok"}],
        "rollback_not_applicable_reason": "immutable report", "limits": [],
    }}


def test_cli_transport_failure_allows_completion_with_same_policy(cli_goal_task, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client", lambda _: (object(), "model"),
    )
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda **_: calls.append(1) or ("continue", "403 raw", False, None, True),
    )
    args = argparse.Namespace(
        task_ids=[cli_goal_task], summary="verified", result=None, metadata=None,
    )
    assert _cmd_complete(args) == 0
    assert calls == [1]
    with kbc.connect() as conn:
        assert kb.get_task(conn, cli_goal_task).status == "done"
        events = [e for e in kb.list_events(conn, cli_goal_task) if e.kind == "goal_gate_unavailable"]
    assert len(events) == 1 and events[0].payload["classification"] == "transport"


def test_cli_goal_review_uses_readiness_and_zero_llm(cli_goal_task, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal", lambda **_: pytest.fail("review must not invoke judge"),
    )
    args = argparse.Namespace(
        task_id=cli_goal_task, summary="ready", metadata=json.dumps(_review_metadata()),
        reviewer=None, force=False,
    )
    assert _cmd_request_review(args) == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, cli_goal_task).status == "review"


def test_cli_goal_review_lists_invalid_fields_without_llm(cli_goal_task, monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal", lambda **_: pytest.fail("review must not invoke judge"),
    )
    args = argparse.Namespace(
        task_id=cli_goal_task, summary="ready", metadata=json.dumps({}),
        reviewer=None, force=False,
    )
    assert _cmd_request_review(args) != 0
    assert "review_readiness_invalid" in capsys.readouterr().err
    with kbc.connect() as conn:
        assert kb.get_task(conn, cli_goal_task).status == "running"
