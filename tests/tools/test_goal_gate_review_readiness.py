from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools as kt


def _metadata():
    return {"review_readiness": {
        "schema_version": 1,
        "candidate_kind": "git",
        "candidate_sha": "a" * 40,
        "base_sha": "b" * 40,
        "remote": "rod",
        "remote_ref": "ironrod/t_c50ee5d3",
        "changed_files": ["hermes_cli/kanban.py"],
        "tests_run": [{"command": "pytest -q", "result": "passed"}],
        "rollback": "git reset --hard " + "b" * 40,
        "limits": [],
    }}


@pytest.fixture
def goal_worker(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "builder")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="t_c50ee5d3 circular review regression", body="ship it",
            assignee="builder", goal_mode=True,
        )
        claimed = kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    return tid


@pytest.mark.parametrize("provider_case", ["401", "403", "timeout"])
def test_tool_completion_transport_failures_fail_open_once_with_audit(
    goal_worker, monkeypatch, provider_case,
):
    calls = []
    client = type("Client", (), {"_hermes_aux_effective_provider": "test-provider"})()
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client", lambda _: (client, "test-model"),
    )

    def judge(**kwargs):
        calls.append(kwargs)
        return "continue", f"raw {provider_case} secret", False, None, True

    monkeypatch.setattr(kt, "judge_goal", judge)
    result = json.loads(kt._handle_complete({"summary": "verified"}))
    assert result["ok"] is True
    assert len(calls) == 1
    with kbc.connect() as conn:
        assert kb.get_task(conn, goal_worker).status == "done"
        events = [e for e in kb.list_events(conn, goal_worker) if e.kind == "goal_gate_unavailable"]
    assert len(events) == 1
    assert events[0].payload["classification"] == "transport"
    assert "raw" not in json.dumps(events[0].payload)


def test_tool_completion_parse_failure_fails_open_and_unresolved_is_audited(goal_worker, monkeypatch):
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client", lambda _: (object(), "model"),
    )
    monkeypatch.setattr(kt, "judge_goal", lambda **_: ("continue", "", True, None, False))
    assert json.loads(kt._handle_complete({"summary": "verified"}))["ok"] is True
    with kbc.connect() as conn:
        events = [e for e in kb.list_events(conn, goal_worker) if e.kind == "goal_gate_unavailable"]
    assert events[-1].payload["classification"] == "parse"


def test_tool_unresolved_judge_fails_open_without_calling_judge(goal_worker, monkeypatch):
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client", lambda _: (None, None),
    )
    monkeypatch.setattr(kt, "judge_goal", lambda **_: pytest.fail("unresolved judge must not be called"))
    assert json.loads(kt._handle_complete({"summary": "verified"}))["ok"] is True
    with kbc.connect() as conn:
        events = [e for e in kb.list_events(conn, goal_worker) if e.kind == "goal_gate_unavailable"]
    assert len(events) == 1
    payload = events[0].payload
    assert payload is not None
    assert payload["classification"] == "unavailable"
    assert set(payload) == {"attempt_id", "classification", "policy_version"}


def test_audit_survives_a_later_completion_guard_failure(goal_worker, monkeypatch):
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client", lambda _: (object(), "model"),
    )
    monkeypatch.setattr(kt, "judge_goal", lambda **_: ("continue", "", False, None, True))
    result = json.loads(kt._handle_complete({
        "summary": "verified", "created_cards": ["t_deadbeef"],
    }))
    assert "error" in result
    with kbc.connect() as conn:
        assert kb.get_task(conn, goal_worker).status == "running"
        events = [e for e in kb.list_events(conn, goal_worker) if e.kind == "goal_gate_unavailable"]
    assert len(events) == 1 and events[0].payload["classification"] == "transport"


def test_tool_completion_audit_failure_is_distinct_and_does_not_rejudge(goal_worker, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client", lambda _: (object(), "model"),
    )
    monkeypatch.setattr(
        kt, "judge_goal",
        lambda **_: calls.append(1) or ("continue", "403", False, None, True),
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_goal_gate.record_unavailable_audit",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db secret")),
    )
    result = json.loads(kt._handle_complete({"summary": "verified"}))
    assert "goal_gate_audit_unavailable" in result["error"]
    assert "evidence" not in result["error"].lower()
    assert calls == [1]
    with kbc.connect() as conn:
        assert kb.get_task(conn, goal_worker).status == "running"


def test_t_c50ee5d3_goal_review_uses_readiness_without_llm(goal_worker, monkeypatch):
    monkeypatch.setattr(
        kt, "judge_goal", lambda **_: pytest.fail("request_review must not call the goal judge"),
    )
    result = json.loads(kt._handle_request_review({
        "summary": "candidate ready; Architect has not reviewed it yet",
        "metadata": _metadata(),
    }))
    assert result["ok"] is True
    with kbc.connect() as conn:
        assert kb.get_task(conn, goal_worker).status == "review"


def test_goal_review_missing_readiness_fails_actionably_without_llm(goal_worker, monkeypatch):
    monkeypatch.setattr(
        kt, "judge_goal", lambda **_: pytest.fail("request_review must not call the goal judge"),
    )
    result = json.loads(kt._handle_request_review({"summary": "candidate ready"}))
    assert "review_readiness_invalid" in result["error"]
    assert "metadata.review_readiness" in result["error"]
    with kbc.connect() as conn:
        assert kb.get_task(conn, goal_worker).status == "running"
