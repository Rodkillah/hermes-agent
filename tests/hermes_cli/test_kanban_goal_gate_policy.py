from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_goal_gate import (
    ATTEMPT_JUDGE_TIMEOUT_SECONDS,
    GoalGateAuditUnavailable,
    POLICY_VERSION,
    completion_decision,
    new_attempt_id,
    record_unavailable_audit,
    review_readiness_decision,
    run_completion_gate,
)


_GOAL_EVENT_KINDS = {
    "goal_gate_attempt_reserved",
    "goal_gate_attempt_result",
    "goal_gate_unavailable",
}


def _goal_events(conn, task_id):
    return [event for event in kb.list_events(conn, task_id) if event.kind in _GOAL_EVENT_KINDS]


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
    assert payload is not None
    assert set(payload) == {"attempt_id", "classification", "policy_version"}
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

    with kbc.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET payload=json_set(payload, '$.lease_expires_at', ?) "
                "WHERE task_id=? AND kind='goal_gate_attempt_reserved'",
                (time.time() - 1, tid),
            )
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


def test_attempt_identity_is_fully_hashed_and_changes_with_proof():
    first = new_attempt_id("t_secret_task", 7, "proof-one")
    replay = new_attempt_id("t_secret_task", 7, "proof-one")
    changed = new_attempt_id("t_secret_task", 7, "proof-two")
    assert first == replay
    assert first != changed
    assert "t_secret_task" not in first
    assert len(first) <= 80


def test_judge_timeout_has_margin_before_reservation_lease():
    from hermes_cli.kanban_goal_gate import ATTEMPT_RESERVATION_TTL_SECONDS

    assert ATTEMPT_JUDGE_TIMEOUT_SECONDS > 0
    assert ATTEMPT_JUDGE_TIMEOUT_SECONDS <= ATTEMPT_RESERVATION_TTL_SECONDS - 15


def test_missing_event_storage_fails_closed_without_judge():
    task = SimpleNamespace(
        id="t1", goal_mode=True, status="running", current_run_id=1,
        title="goal", body="criteria",
    )
    conn = sqlite3.connect(":memory:")
    calls = 0

    def judge(**_kwargs):
        nonlocal calls
        calls += 1

    with pytest.raises(GoalGateAuditUnavailable, match="goal_gate_audit_unavailable"):
        run_completion_gate(conn, task, "proof", run_id=1, judge=judge)
    assert calls == 0


def test_result_persistence_failure_fails_closed_after_one_judge_call(
    audit_board, monkeypatch,
):
    tid, run_id = audit_board
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda _purpose: (SimpleNamespace(_hermes_aux_effective_provider="provider"), "model"),
    )
    real_append = kb.append_idempotent_event

    def fail_result(conn, task_id, kind, payload, **kwargs):
        if kind == "goal_gate_attempt_result":
            raise sqlite3.OperationalError("forced persistence failure with raw secret")
        return real_append(conn, task_id, kind, payload, **kwargs)

    monkeypatch.setattr(kb, "append_idempotent_event", fail_result)
    calls = 0

    def judge(**_kwargs):
        nonlocal calls
        calls += 1
        return "done", "private reason", False, None, False

    with kbc.connect() as conn:
        with pytest.raises(GoalGateAuditUnavailable) as raised:
            run_completion_gate(
                conn, kb.get_task(conn, tid), "proof", run_id=run_id, judge=judge,
            )
    assert calls == 1
    assert "secret" not in str(raised.value).lower()
    assert "goal_gate_audit_unavailable" in str(raised.value)


def test_task_event_payloads_are_bounded_and_expurged(audit_board, monkeypatch):
    tid, run_id = audit_board
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda _purpose: (
            SimpleNamespace(_hermes_aux_effective_provider="https://user:credential@host"),
            "Bearer model-secret",
        ),
    )

    def judge(**kwargs):
        assert kwargs["timeout"] == ATTEMPT_JUDGE_TIMEOUT_SECONDS
        return "blocked", "raw judge reason sk-forbidden", False, None, False

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        decision = run_completion_gate(
            conn, task, "proof https://user:password@host", run_id=run_id,
            judge=judge, attempt_metadata={"handoff": "credential-freeform"},
        )
        events = _goal_events(conn, tid)

    assert decision.code == "goal_gate_blocked"
    allowed = {
        "attempt_id", "policy_version", "reserved_at", "lease_expires_at",
        "outcome", "code", "classification",
    }
    assert events
    assert all(set((event.payload or {})) <= allowed for event in events)
    encoded = json.dumps([event.payload for event in events]).lower()
    for forbidden in (
        "raw judge reason", "sk-forbidden", "password", "credential-freeform",
        "bearer", "user:credential", "proof https", "handoff", "provider", "model",
        "message", "reason", "prompt", "response", "exception", "metadata", "action",
    ):
        assert forbidden not in encoded


def test_goal_attempt_events_are_collected_for_terminal_tasks(audit_board, monkeypatch):
    tid, run_id = audit_board
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda _purpose: (SimpleNamespace(_hermes_aux_effective_provider="provider"), "model"),
    )
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        decision = run_completion_gate(
            conn, task, "proof", run_id=run_id,
            judge=lambda **_: ("done", "", False, None, False),
        )
        assert decision.outcome == "allow"
        assert kb.complete_task(conn, tid, summary="done", expected_run_id=run_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at=0 WHERE task_id=? AND kind LIKE 'goal_gate_%'",
                (tid,),
            )
        kb.gc_events(conn)
        assert _goal_events(conn, tid) == []


def test_multiprocess_single_flight_replay_and_changed_proof(audit_board, monkeypatch):
    """Distinct processes and SQLite connections share exactly one judge call."""
    tid, run_id = audit_board
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda _purpose: (SimpleNamespace(_hermes_aux_effective_provider="provider"), "model"),
    )
    ctx = multiprocessing.get_context("fork")
    calls = ctx.Value("i", 0)
    entered = ctx.Event()
    release = ctx.Event()
    results = ctx.Queue()

    def worker(proof, wait):
        def judge(**_kwargs):
            with calls.get_lock():
                calls.value += 1
            entered.set()
            if wait:
                assert release.wait(5)
            return "done", "private reason", False, None, False

        with kbc.connect() as conn:
            decision = run_completion_gate(
                conn, kb.get_task(conn, tid), proof, run_id=run_id, judge=judge,
            )
        results.put((decision.outcome, decision.code))

    first = ctx.Process(target=worker, args=("same proof", True))
    first.start()
    assert entered.wait(5)
    second = ctx.Process(target=worker, args=("same proof", False))
    second.start()
    second.join(5)
    assert second.exitcode == 0
    release.set()
    first.join(5)
    assert first.exitcode == 0
    concurrent = sorted([results.get(timeout=2), results.get(timeout=2)])
    assert concurrent == [
        ("allow", "goal_gate_done"),
        ("reject", "goal_gate_attempt_in_progress"),
    ]
    assert calls.value == 1

    worker("same proof", False)
    assert results.get(timeout=2) == ("allow", "goal_gate_done")
    assert calls.value == 1
    worker("changed proof", False)
    assert results.get(timeout=2) == ("allow", "goal_gate_done")
    assert calls.value == 2
    with kbc.connect() as conn:
        result_events = [
            event for event in _goal_events(conn, tid)
            if event.kind == "goal_gate_attempt_result"
        ]
    assert len(result_events) == 2
    assert len({event.payload["attempt_id"] for event in result_events}) == 2


def test_multiprocess_crash_recovers_after_lease_without_second_judge(
    audit_board, monkeypatch,
):
    tid, run_id = audit_board
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda _purpose: (SimpleNamespace(_hermes_aux_effective_provider="provider"), "model"),
    )
    ctx = multiprocessing.get_context("fork")

    def crash_worker():
        with kbc.connect() as conn:
            run_completion_gate(
                conn, kb.get_task(conn, tid), "crash proof", run_id=run_id,
                judge=lambda **_: os._exit(23),
            )

    crashed = ctx.Process(target=crash_worker)
    crashed.start()
    crashed.join(5)
    assert crashed.exitcode == 23
    recovery_decisions = ctx.Queue()

    def recover_worker():
        with kbc.connect() as conn:
            decision = run_completion_gate(
                conn, kb.get_task(conn, tid), "crash proof", run_id=run_id,
                judge=lambda **_: os._exit(24),
            )
        recovery_decisions.put((decision.outcome, decision.classification))

    with kbc.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET payload=json_set(payload, '$.lease_expires_at', ?) "
                "WHERE task_id=? AND kind='goal_gate_attempt_reserved'",
                (time.time() - 1, tid),
            )
    recoverers = [ctx.Process(target=recover_worker) for _ in range(2)]
    for process in recoverers:
        process.start()
    for process in recoverers:
        process.join(5)
        assert process.exitcode == 0
    assert [recovery_decisions.get(timeout=2) for _ in recoverers] == [
        ("allow_with_diagnostic", "unavailable"),
        ("allow_with_diagnostic", "unavailable"),
    ]
    with kbc.connect() as conn:
        result_events = [
            event for event in _goal_events(conn, tid)
            if event.kind == "goal_gate_attempt_result"
        ]
    assert len(result_events) == 1


def test_late_judge_cannot_overwrite_expiry_recovery(audit_board, monkeypatch):
    tid, run_id = audit_board
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda _purpose: (SimpleNamespace(_hermes_aux_effective_provider="provider"), "model"),
    )
    ctx = multiprocessing.get_context("fork")
    entered = ctx.Event()
    release = ctx.Event()
    decisions = ctx.Queue()

    def late_worker():
        def judge(**_kwargs):
            entered.set()
            assert release.wait(5)
            return "blocked", "must never become canonical", False, None, False

        with kbc.connect() as conn:
            decision = run_completion_gate(
                conn, kb.get_task(conn, tid), "race proof", run_id=run_id, judge=judge,
            )
        decisions.put((decision.outcome, decision.code, decision.classification))

    late = ctx.Process(target=late_worker)
    late.start()
    assert entered.wait(5)
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET payload=json_set(payload, '$.lease_expires_at', ?) "
                "WHERE task_id=? AND kind='goal_gate_attempt_reserved'",
                (time.time() - 1, tid),
            )
        recovered = run_completion_gate(
            conn, kb.get_task(conn, tid), "race proof", run_id=run_id,
            judge=lambda **_: pytest.fail("recovery must not call judge"),
        )
    release.set()
    late.join(5)
    assert late.exitcode == 0
    canonical = ("allow_with_diagnostic", "goal_gate_unavailable", "unavailable")
    assert (recovered.outcome, recovered.code, recovered.classification) == canonical
    assert decisions.get(timeout=2) == canonical
    with kbc.connect() as conn:
        result_events = [
            event for event in _goal_events(conn, tid)
            if event.kind == "goal_gate_attempt_result"
        ]
    assert len(result_events) == 1
