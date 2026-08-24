"""Native review -> production -> live verification lifecycle regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

TEST_SHA = "a" * 40


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    verifier = home / "trusted-production-verifier"
    verifier.write_text(
        """#!/usr/bin/env python3
import datetime
import json
import sys

payload = json.load(sys.stdin)
phase = payload["phase"]
title = (payload["task"].get("title") or "").lower()
deny = ("credential", "token", "destructive", "drop table", "purchase",
        "budget", "legal", "contract", "architecture", "topology",
        "publish", "customer notification")
facts = {}
if phase == "deployment":
    facts = {"git_remote_matches": True, "deployed_revision_matches": True,
             "backup_exists": True, "canary_passed": True,
             "rollback_tested": True}
elif phase == "live":
    facts = {"live_probes_passed": True,
             "deployed_revision_matches": True,
             "rollback_available": True}
receipt = {"ok": True, "phase": phase, "task_id": payload["task"]["id"],
           "receipt_id": "trusted-test-receipt-" + phase,
           "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "facts": facts}
if phase == "risk":
    receipt["decision"] = "human_gate" if any(term in title for term in deny) else "safe"
print(json.dumps(receipt))
""",
        encoding="utf-8",
    )
    verifier.chmod(0o700)
    metadata_path = kb.board_metadata_path()
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps({
            "automation": {
                "auto_production": {
                    "enabled": True,
                    "verifier_command": str(verifier),
                    "verifier_timeout_seconds": 5,
                }
            }
        }),
        encoding="utf-8",
    )
    with kb.connect() as conn:
        yield conn


def _enter_review(conn, title: str = "Deploy safe service change"):
    task_id = kb.create_task(conn, title=title, assignee="ironrod-ops")
    implementation = kb.claim_task(conn, task_id, claimer="ops:source")
    assert implementation is not None
    assert kb.request_review(
        conn, task_id, summary="candidate tested", reviewer="architect",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id, claimer="architect:source")
    assert review is not None
    return task_id, review


def _safe_risk():
    return {
        "risk_classification": {
            "category": "standard_code",
            "high_risk": False,
            "human_gate_required": False,
        }
    }


def _deploy_proof(version: str = TEST_SHA):
    return {
        "production_version": version,
        "deployed_at": "2026-08-24T12:00:00+02:00",
        "canary": {"production_version": version, "result": "passed"},
        "main_promotion": {"mode": "fast-forward", "sha": version, "remote": "origin/main"},
        "backup": {"created": True, "id": "backup-20260824"},
        "rollback_prepared": {"target": "previous_sha", "tested": True},
    }


def _claim_production_phase(conn, phase: str):
    task_id, review = _enter_review(conn)
    assert kb.approve_production(
        conn, task_id, metadata=_safe_risk(), actor="architect",
        expected_run_id=review.current_run_id,
    )[0]
    production = kb.claim_task(conn, task_id)
    assert production is not None
    if phase == "production_ready":
        return task_id, production
    assert kb.mark_prod_implemented(
        conn, task_id, metadata=_deploy_proof(), actor="ironrod-ops",
        expected_run_id=production.current_run_id,
    )[0]
    live = kb.claim_review_task(conn, task_id)
    assert live is not None
    return task_id, live


def test_architect_go_routes_same_card_through_both_production_columns(board):
    task_id, review = _enter_review(board)
    ok, owner = kb.approve_production(
        board, task_id, summary="Architect GO", metadata=_safe_risk(),
        expected_run_id=review.current_run_id,
        actor="architect",
    )
    assert (ok, owner) == (True, "ironrod-ops")
    task = kb.get_task(board, task_id)
    assert task is not None
    assert (task.status, task.assignee) == ("production_ready", "ironrod-ops")

    production = kb.claim_task(board, task_id, claimer="ops:production")
    assert production is not None
    with pytest.raises(kb.ProductionLifecycleError, match="mark_prod_implemented"):
        kb.complete_task(board, task_id, expected_run_id=production.current_run_id)

    ok, reviewer = kb.mark_prod_implemented(
        board, task_id, summary="canary and deploy succeeded",
        metadata=_deploy_proof("815d13d38083a41d3978c33fe40f64413b370697"),
        expected_run_id=production.current_run_id,
        actor="ironrod-ops",
    )
    assert (ok, reviewer) == (True, "architect")
    task = kb.get_task(board, task_id)
    assert task is not None
    assert (task.status, task.assignee) == ("prod_implemented", "architect")

    live_review = kb.claim_review_task(board, task_id, claimer="architect:live")
    assert live_review is not None
    with pytest.raises(kb.ProductionLifecycleError, match="post_deploy_checks"):
        kb.complete_task(
            board, task_id, metadata={
                "delivery_level": "verified_production",
                "production_version": "815d13d38083a41d3978c33fe40f64413b370697",
                "deployed_at": "2026-08-24T12:00:00+02:00",
            }, expected_run_id=live_review.current_run_id, actor="architect",
        )
    assert kb.complete_task(
        board, task_id, result="live GO",
        metadata={
            "delivery_level": "verified_production",
            "production_version": "815d13d38083a41d3978c33fe40f64413b370697",
            "deployed_at": "2026-08-24T12:00:00+02:00",
            "post_deploy_checks": {"result": "passed", "production_version": "815d13d38083a41d3978c33fe40f64413b370697"},
            "rollback": {"verified": True, "available": True, "target": "previous_sha"},
        },
        expected_run_id=live_review.current_run_id,
        actor="architect",
    )
    assert kb.get_task(board, task_id).status == "done"


def test_live_no_go_routes_back_to_production_ready(board):
    task_id, review = _enter_review(board)
    assert kb.approve_production(
        board, task_id, metadata=_safe_risk(),
        expected_run_id=review.current_run_id,
        actor="architect",
    )[0]
    production = kb.claim_task(board, task_id)
    assert production is not None
    assert kb.mark_prod_implemented(
        board, task_id,
        metadata=_deploy_proof(),
        expected_run_id=production.current_run_id,
        actor="ironrod-ops",
    )[0]
    live_review = kb.claim_review_task(board, task_id)
    assert live_review is not None
    ok, owner = kb.request_changes(
        board, task_id, reason="live probe failed; roll back and redeploy",
        expected_run_id=live_review.current_run_id,
    )
    assert (ok, owner) == (True, "ironrod-ops")
    task = kb.get_task(board, task_id)
    assert task is not None
    assert (task.status, task.assignee) == ("production_ready", "ironrod-ops")


@pytest.mark.parametrize("title", [
    "Rotate production credentials and API token",
    "Run destructive migration and drop table",
    "Approve purchase budget",
    "Sign legal contract",
    "Make architecture decision for service topology",
    "Publish release and send customer notification",
])
def test_sensitive_work_is_refused_and_materializes_human_gate(board, title):
    task_id, review = _enter_review(board, title)
    ok, reason = kb.approve_production(
        board, task_id, metadata=_safe_risk(),
        expected_run_id=review.current_run_id,
        actor="architect",
    )
    assert ok is False
    assert "blocked human gate" in (reason or "")
    task = kb.get_task(board, task_id)
    assert task is not None
    assert (task.status, task.assignee) == ("todo", "architect")
    parents = kb.parent_ids(board, task_id)
    assert len(parents) == 1
    gate = kb.get_task(board, parents[0])
    assert gate is not None
    assert (gate.status, gate.assignee) == ("blocked", None)


def test_production_transition_tools_are_exposed_to_kanban_workers(board, monkeypatch):
    import tools.kanban_tools  # noqa: F401 - registers tools
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker_context")
    invalidate_check_fn_cache()
    definitions = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {
        item["function"]["name"] for item in definitions if "function" in item
    }
    assert "kanban_approve_production" in names
    assert "kanban_mark_prod_implemented" in names
    assert "kanban_approve_production" in resolve_toolset("kanban")
    assert "kanban_mark_prod_implemented" in resolve_toolset("kanban")


def test_dispatcher_automatically_runs_both_production_columns(board, monkeypatch):
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda _name: True)
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )
    monkeypatch.setattr(kb, "check_respawn_guard", lambda *a, **k: None)
    task_id, review = _enter_review(board)
    assert kb.approve_production(
        board, task_id, metadata=_safe_risk(),
        expected_run_id=review.current_run_id,
        actor="architect",
    )[0]

    spawned = []

    def spawn(task, workspace):
        spawned.append((task.id, task.assignee, list(task.skills or [])))
        return None

    first = kb.dispatch_once(board, spawn_fn=spawn)
    assert task_id in [item[0] for item in first.spawned]
    production = kb.get_task(board, task_id)
    assert production is not None and production.status == "running"
    assert kb.mark_prod_implemented(
        board, task_id,
        metadata=_deploy_proof(),
        expected_run_id=production.current_run_id,
        actor="ironrod-ops",
    )[0]

    second = kb.dispatch_once(board, spawn_fn=spawn)
    assert task_id in [item[0] for item in second.spawned]
    assert spawned[0][1] == "ironrod-ops"
    assert spawned[1][1] == "architect"
    assert "sdlc-review" in spawned[1][2]


def test_failed_or_inconsistent_production_evidence_is_rejected(board):
    task_id, review = _enter_review(board)
    assert kb.approve_production(
        board, task_id, metadata=_safe_risk(),
        expected_run_id=review.current_run_id, actor="architect",
    )[0]
    production = kb.claim_task(board, task_id)
    assert production is not None
    failed = _deploy_proof()
    failed["canary"] = {"result": "failed", "production_version": TEST_SHA}
    failed["backup"] = {"created": False, "id": "backup-1"}
    assert not kb.mark_prod_implemented(
        board, task_id, metadata=failed,
        expected_run_id=production.current_run_id, actor="ironrod-ops",
    )[0]
    assert kb.get_task(board, task_id).status == "running"
    assert kb.mark_prod_implemented(
        board, task_id, metadata=_deploy_proof(),
        expected_run_id=production.current_run_id, actor="ironrod-ops",
    )[0]
    live = kb.claim_review_task(board, task_id)
    assert live is not None
    with pytest.raises(kb.ProductionLifecycleError, match="production_version_matches"):
        kb.complete_task(
            board, task_id, actor="architect",
            expected_run_id=live.current_run_id,
            metadata={
                "delivery_level": "verified_production",
                "production_version": "b" * 40,
                "deployed_at": "2026-08-24T12:00:00+02:00",
                "post_deploy_checks": {"result": "passed", "production_version": "b" * 40},
                "rollback": {"verified": True, "available": True},
            },
        )


def test_production_roles_are_domain_enforced(board):
    task_id, review = _enter_review(board)
    assert not kb.approve_production(
        board, task_id, metadata=_safe_risk(),
        expected_run_id=review.current_run_id, actor="ironrod-ops",
    )[0]
    assert kb.get_task(board, task_id).status == "running"


def test_invalid_or_stale_review_never_materializes_a_human_gate(board):
    before = len(kb.list_tasks(board, include_archived=True))
    ok, reason = kb.approve_production(
        board, "t_deadbeefdead", metadata=_safe_risk(),
        expected_run_id=123, actor="architect",
    )
    assert (ok, reason) == (False, "task not found")
    assert len(kb.list_tasks(board, include_archived=True)) == before

    task_id, review = _enter_review(board, "Rotate production credentials")
    ok, reason = kb.approve_production(
        board, task_id, metadata=_safe_risk(),
        expected_run_id=review.current_run_id + 1, actor="architect",
    )
    assert (ok, reason) == (False, "run_id mismatch")
    assert kb.parent_ids(board, task_id) == []


def test_external_verifier_is_mandatory_and_fail_closed(board, monkeypatch):
    task_id, review = _enter_review(board)
    monkeypatch.setattr(kb, "read_board_metadata", lambda *_: {"automation": {}})
    ok, reason = kb.approve_production(
        board, task_id, metadata=_safe_risk(),
        expected_run_id=review.current_run_id, actor="architect",
    )
    assert ok is False
    assert "trusted production verifier" in (reason or "")
    task = kb.get_task(board, task_id)
    assert task is not None and task.status == "todo"
    parents = kb.parent_ids(board, task_id)
    assert len(parents) == 1
    gate = kb.get_task(board, parents[0])
    assert gate is not None and (gate.status, gate.assignee) == ("blocked", None)


def test_stale_claims_resume_both_production_phases(board):
    task_id, review = _enter_review(board)
    assert kb.approve_production(
        board, task_id, metadata=_safe_risk(),
        expected_run_id=review.current_run_id, actor="architect",
    )[0]
    production = kb.claim_task(board, task_id, ttl_seconds=1)
    assert production is not None
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET claim_expires = 0 WHERE id = ?", (task_id,))
    assert kb.release_stale_claims(board, signal_fn=lambda *_: None) == 1
    assert kb.get_task(board, task_id).status == "production_ready"
    production = kb.claim_task(board, task_id)
    assert kb.mark_prod_implemented(
        board, task_id, metadata=_deploy_proof(),
        expected_run_id=production.current_run_id, actor="ironrod-ops",
    )[0]
    live = kb.claim_review_task(board, task_id, ttl_seconds=1)
    assert live is not None
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET claim_expires = 0 WHERE id = ?", (task_id,))
    assert kb.release_stale_claims(board, signal_fn=lambda *_: None) == 1
    assert kb.get_task(board, task_id).status == "prod_implemented"


@pytest.mark.parametrize("phase", ["production_ready", "prod_implemented"])
def test_worker_crash_resumes_exact_production_phase(board, monkeypatch, phase):
    task_id, active = _claim_production_phase(board, phase)
    kb._set_worker_pid(board, task_id, 987654)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET started_at = 0 WHERE id = ?", (task_id,))
        board.execute(
            "UPDATE task_runs SET started_at = 0 WHERE id = ?",
            (active.current_run_id,),
        )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("nonzero_exit", 9))
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    assert kb.detect_crashed_workers(board) == [task_id]
    assert kb.get_task(board, task_id).status == phase
    ended = board.execute(
        "SELECT outcome FROM task_runs WHERE id = ?", (active.current_run_id,)
    ).fetchone()
    assert ended["outcome"] == "crashed"


@pytest.mark.parametrize("phase", ["production_ready", "prod_implemented"])
def test_worker_timeout_resumes_exact_production_phase(board, monkeypatch, phase):
    task_id, active = _claim_production_phase(board, phase)
    kb._set_worker_pid(board, task_id, 987655)
    with kb.write_txn(board):
        board.execute(
            "UPDATE tasks SET started_at = 0, max_runtime_seconds = 1 WHERE id = ?",
            (task_id,),
        )
        board.execute(
            "UPDATE task_runs SET started_at = 0 WHERE id = ?",
            (active.current_run_id,),
        )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    assert kb.enforce_max_runtime(board, signal_fn=lambda *_: None) == [task_id]
    assert kb.get_task(board, task_id).status == phase
    ended = board.execute(
        "SELECT outcome FROM task_runs WHERE id = ?", (active.current_run_id,)
    ).fetchone()
    assert ended["outcome"] == "timed_out"


@pytest.mark.parametrize("phase", ["production_ready", "prod_implemented"])
def test_parent_reopen_preserves_production_resume_phase(board, phase):
    parent = kb.create_task(board, title="stable prerequisite", assignee="planner")
    assert kb.complete_task(board, parent)
    task_id = kb.create_task(
        board, title="Deploy safe service change", assignee="ironrod-ops",
        parents=[parent],
    )
    implementation = kb.claim_task(board, task_id)
    assert kb.request_review(
        board, task_id, reviewer="architect", summary="ready",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(board, task_id)
    assert kb.approve_production(
        board, task_id, metadata=_safe_risk(), actor="architect",
        expected_run_id=review.current_run_id,
    )[0]
    if phase == "prod_implemented":
        production = kb.claim_task(board, task_id)
        assert kb.mark_prod_implemented(
            board, task_id, metadata=_deploy_proof(), actor="ironrod-ops",
            expected_run_id=production.current_run_id,
        )[0]
    assert kb.get_task(board, task_id).status == phase
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
    kb.invalidate_descendants_for_parent_reopen(board, parent, author="operator")
    assert kb.get_task(board, task_id).status == "todo"
    event = [
        e for e in kb.list_events(board, task_id)
        if e.kind == "descendant_invalidated"
    ][-1]
    assert event.payload["resume_status"] == phase
    assert kb.complete_task(board, parent)
    kb.recompute_ready(board)
    assert kb.get_task(board, task_id).status == phase


@pytest.mark.parametrize("phase", ["production_ready", "prod_implemented"])
def test_parent_reopen_reclaims_active_production_run_and_resumes_phase(board, phase):
    parent = kb.create_task(board, title="stable prerequisite", assignee="planner")
    assert kb.complete_task(board, parent)
    task_id = kb.create_task(
        board, title="Deploy safe service change", assignee="ironrod-ops",
        parents=[parent],
    )
    implementation = kb.claim_task(board, task_id)
    assert kb.request_review(
        board, task_id, reviewer="architect", summary="ready",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(board, task_id)
    assert kb.approve_production(
        board, task_id, metadata=_safe_risk(), actor="architect",
        expected_run_id=review.current_run_id,
    )[0]
    production = kb.claim_task(board, task_id)
    assert production is not None
    active = production
    if phase == "prod_implemented":
        assert kb.mark_prod_implemented(
            board, task_id, metadata=_deploy_proof(), actor="ironrod-ops",
            expected_run_id=production.current_run_id,
        )[0]
        active = kb.claim_review_task(board, task_id)
        assert active is not None
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
    kb.invalidate_descendants_for_parent_reopen(board, parent, author="operator")
    assert kb.get_task(board, task_id).status == "todo"
    assert kb.latest_run(board, task_id).outcome == "reclaimed"
    assert kb.complete_task(board, parent)
    kb.recompute_ready(board)
    assert kb.get_task(board, task_id).status == phase
