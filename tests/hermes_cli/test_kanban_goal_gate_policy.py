from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_goal_gate import (
    POLICY_VERSION,
    completion_decision,
    new_attempt_id,
    record_unavailable_audit,
    review_readiness_decision,
    run_completion_gate,
)


@pytest.mark.parametrize("classification", ["transport", "parse"])
def test_completion_failure_flags_fail_open_with_diagnostic(classification):
    decision = completion_decision(
        verdict="continue",
        reason="provider said add evidence sk-secret",
        transport_failed=classification == "transport",
        parse_failed=classification == "parse",
    )
    assert (decision.outcome, decision.code, decision.classification) == (
        "allow_with_diagnostic", "goal_gate_unavailable", classification,
    )
    assert "provider said" not in decision.message


def test_transport_wins_when_both_failure_flags_are_true():
    decision = completion_decision(
        verdict="continue", reason="raw", transport_failed=True, parse_failed=True,
    )
    assert decision.classification == "transport"


@pytest.mark.parametrize("verdict", ["continue", "wait", "blocked"])
def test_valid_negative_completion_verdict_refuses(verdict):
    decision = completion_decision(
        verdict=verdict, reason="acceptance item missing",
        transport_failed=False, parse_failed=False,
    )
    assert decision.outcome == "reject"
    assert decision.code == f"goal_gate_{verdict}"
    assert "acceptance item missing" in decision.message


def test_done_allows_and_unknown_protocol_fails_open_as_parse():
    done = completion_decision("done", "", False, False)
    unknown = completion_decision("surprise", "raw", False, False)
    assert (done.outcome, done.classification) == ("allow", None)
    assert (unknown.outcome, unknown.classification) == ("allow_with_diagnostic", "parse")


def _git_readiness(**overrides):
    data = {
        "schema_version": 1,
        "candidate_kind": "git",
        "candidate_sha": "a" * 40,
        "base_sha": "b" * 40,
        "remote": "rod",
        "remote_ref": "ironrod/candidate",
        "changed_files": ["hermes_cli/kanban.py"],
        "tests_run": [{"command": "pytest -q", "result": "1 passed"}],
        "rollback": "git reset --hard base",
        "limits": [],
    }
    data.update(overrides)
    return {"review_readiness": data}


def test_git_review_readiness_v1_accepts_complete_and_explicit_no_diff():
    assert review_readiness_decision(_git_readiness()).outcome == "allow"
    no_diff = _git_readiness(changed_files=[], no_diff_reason="documentation-only identity")
    assert review_readiness_decision(no_diff).outcome == "allow"


def test_git_review_readiness_errors_are_complete_and_stably_ordered():
    decision = review_readiness_decision({"review_readiness": {
        "schema_version": 1, "candidate_kind": "git", "candidate_sha": "bad",
        "base_sha": "bad", "remote": "https://user:secret@example.test/repo",
        "remote_ref": "", "changed_files": [], "tests_run": [], "rollback": "",
        "limits": "none",
    }})
    assert decision.outcome == "reject"
    assert decision.code == "review_readiness_invalid"
    expected = [
        "candidate_sha", "base_sha", "remote", "remote_ref", "no_diff_reason",
        "tests_run", "rollback", "limits",
    ]
    positions = [decision.message.index(name) for name in expected]
    assert positions == sorted(positions)


@pytest.mark.parametrize("bad_remote", ["https://token@example.test/x", "user:pass@host", "Bearer abc"])
def test_git_remote_rejects_credentials(bad_remote):
    assert review_readiness_decision(_git_readiness(remote=bad_remote)).outcome == "reject"


def test_artifact_review_readiness_accepts_rollback_reason():
    metadata = {"review_readiness": {
        "schema_version": 1,
        "candidate_kind": "artifact",
        "candidate_identity": "sha256:abc",
        "candidate_location": "attachments/report.pdf",
        "verification": [{"check": "open", "result": "rendered"}],
        "rollback_not_applicable_reason": "read-only report",
        "limits": [],
    }}
    assert review_readiness_decision(metadata).outcome == "allow"


@pytest.mark.parametrize("metadata", [
    None,
    {},
    {"review_readiness": {"schema_version": 2, "candidate_kind": "git"}},
    {"review_readiness": {"schema_version": 1, "candidate_kind": "unknown"}},
])
def test_missing_or_unknown_review_readiness_is_rejected_without_fallback(metadata):
    decision = review_readiness_decision(metadata)
    assert (decision.outcome, decision.code) == ("reject", "review_readiness_invalid")


@pytest.fixture
def audit_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="audit", assignee="worker", goal_mode=True)
        claimed = kb.claim_task(conn, tid)
        run_id = claimed.current_run_id
    return tid, run_id


def test_unavailable_audit_is_redacted_and_idempotent(audit_board):
    tid, run_id = audit_board
    attempt_id = new_attempt_id(tid, run_id)
    client = SimpleNamespace(_hermes_aux_effective_provider="https://user:secret@example.test")
    with kbc.connect() as conn:
        assert record_unavailable_audit(
            conn, task_id=tid, run_id=run_id, attempt_id=attempt_id,
            classification="transport", client=client, model="Bearer topsecret",
        ) is True
        assert record_unavailable_audit(
            conn, task_id=tid, run_id=run_id, attempt_id=attempt_id,
            classification="transport", client=client, model="Bearer topsecret",
        ) is False
        events = [e for e in kb.list_events(conn, tid) if e.kind == "goal_gate_unavailable"]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["action"] == "kanban_complete"
    assert payload["policy_version"] == POLICY_VERSION
    assert payload["attempt_id"] == attempt_id
    encoded = json.dumps(payload)
    assert "secret" not in encoded and "topsecret" not in encoded


def test_unavailable_audit_is_atomic_under_concurrency(audit_board):
    tid, run_id = audit_board
    attempt_id = new_attempt_id(tid, run_id)

    def write_once(_):
        with kbc.connect() as conn:
            return record_unavailable_audit(
                conn, task_id=tid, run_id=run_id, attempt_id=attempt_id,
                classification="parse", client=None, model=None,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write_once, range(2)))
    assert sorted(results) == [False, True]
    with kbc.connect() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "goal_gate_unavailable"]
    assert len(events) == 1


def test_completion_gate_replay_uses_stable_attempt_identity(audit_board, monkeypatch):
    tid, run_id = audit_board
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda _purpose: (SimpleNamespace(_hermes_aux_effective_provider="provider"), "model"),
    )

    judge_calls = 0

    def unavailable_judge(**_kwargs):
        nonlocal judge_calls
        judge_calls += 1
        return "continue", "raw provider failure", False, None, True

    def run_once(evidence):
        with kbc.connect() as conn:
            task = kb.get_task(conn, tid)
            return run_completion_gate(
                conn, task, evidence, run_id=run_id, judge=unavailable_judge,
            )

    decisions = [run_once("same proof"), run_once("same proof")]
    assert all(item.outcome == "allow_with_diagnostic" for item in decisions)
    assert judge_calls == 1

    with kbc.connect() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "goal_gate_unavailable"]
    assert len(events) == 1
    assert events[0].payload is not None
    assert events[0].payload["attempt_id"] == new_attempt_id(tid, run_id, "same proof")
    assert "same proof" not in events[0].payload["attempt_id"]

    run_once("corrected proof")
    assert judge_calls == 2
    with kbc.connect() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "goal_gate_unavailable"]
    assert len(events) == 2
    assert len({event.payload["attempt_id"] for event in events}) == 2


def test_completion_gate_concurrency_calls_judge_once(audit_board, monkeypatch):
    tid, run_id = audit_board
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda _purpose: (SimpleNamespace(_hermes_aux_effective_provider="provider"), "model"),
    )
    judge_entered = threading.Event()
    release_judge = threading.Event()
    judge_calls = 0

    def unavailable_judge(**_kwargs):
        nonlocal judge_calls
        judge_calls += 1
        judge_entered.set()
        assert release_judge.wait(timeout=5)
        return "continue", "raw provider failure", False, None, True

    def run_once():
        with kbc.connect() as conn:
            return run_completion_gate(
                conn, kb.get_task(conn, tid), "same proof", run_id=run_id,
                judge=unavailable_judge,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_once)
        assert judge_entered.wait(timeout=5)
        second = pool.submit(run_once)
        second_decision = second.result(timeout=5)
        release_judge.set()
        first_decision = first.result(timeout=5)

    assert judge_calls == 1
    assert first_decision.outcome == "allow_with_diagnostic"
    assert second_decision.code == "goal_gate_attempt_in_progress"
    with kbc.connect() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "goal_gate_unavailable"]
    assert len(events) == 1
    assert run_once().outcome == "allow_with_diagnostic"
    assert judge_calls == 1


def test_completion_gate_recovers_abandoned_reservation_without_rejudging(
    audit_board, monkeypatch,
):
    tid, run_id = audit_board
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda _purpose: (SimpleNamespace(_hermes_aux_effective_provider="provider"), "model"),
    )
    judge_calls = 0

    def interrupted_judge(**_kwargs):
        nonlocal judge_calls
        judge_calls += 1
        raise KeyboardInterrupt("simulated process death")

    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        with kbc.connect() as conn:
            run_completion_gate(
                conn, kb.get_task(conn, tid), "same proof", run_id=run_id,
                judge=interrupted_judge,
            )

    monkeypatch.setattr("hermes_cli.kanban_goal_gate.ATTEMPT_RESERVATION_TTL_SECONDS", 0)
    with kbc.connect() as conn:
        decision = run_completion_gate(
            conn, kb.get_task(conn, tid), "same proof", run_id=run_id,
            judge=interrupted_judge,
        )
        events = [e for e in kb.list_events(conn, tid) if e.kind == "goal_gate_unavailable"]

    assert decision.outcome == "allow_with_diagnostic"
    assert decision.classification == "unavailable"
    assert judge_calls == 1
    assert len(events) == 1


def test_attempt_identity_ignores_surface_injected_session_metadata():
    base = {"tests_run": 3}
    stamped = {**base, "worker_session_id": "session-from-tool-surface"}
    assert new_attempt_id("t_same", 7, "proof", base) == new_attempt_id(
        "t_same", 7, "proof", stamped,
    )
