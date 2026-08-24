"""Native review -> production -> live verification lifecycle regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
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


def _deploy_proof(version: str = "abc123"):
    return {
        "production_version": version,
        "deployed_at": "2026-08-24T12:00:00+02:00",
        "canary": {"version": version, "result": "passed"},
        "main_promotion": {"mode": "fast-forward", "sha": version},
        "backup": {"created": True, "dated": True},
        "rollback_prepared": {"target": "previous_sha", "tested": True},
    }


def test_architect_go_routes_same_card_through_both_production_columns(board):
    task_id, review = _enter_review(board)
    ok, owner = kb.approve_production(
        board, task_id, summary="Architect GO", metadata=_safe_risk(),
        expected_run_id=review.current_run_id,
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
            }, expected_run_id=live_review.current_run_id,
        )
    assert kb.complete_task(
        board, task_id, result="live GO",
        metadata={
            "delivery_level": "verified_production",
            "production_version": "815d13d38083a41d3978c33fe40f64413b370697",
            "deployed_at": "2026-08-24T12:00:00+02:00",
            "post_deploy_checks": {"health": "ok", "version": "ok"},
            "rollback": {"verified": True, "target": "previous_sha"},
        },
        expected_run_id=live_review.current_run_id,
    )
    assert kb.get_task(board, task_id).status == "done"


def test_live_no_go_routes_back_to_production_ready(board):
    task_id, review = _enter_review(board)
    assert kb.approve_production(
        board, task_id, metadata=_safe_risk(),
        expected_run_id=review.current_run_id,
    )[0]
    production = kb.claim_task(board, task_id)
    assert production is not None
    assert kb.mark_prod_implemented(
        board, task_id,
        metadata=_deploy_proof(),
        expected_run_id=production.current_run_id,
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


def test_sensitive_work_is_refused_and_never_auto_assigned(board):
    task_id, review = _enter_review(board, "Rotate production credentials and API token")
    ok, reason = kb.approve_production(
        board, task_id, metadata=_safe_risk(),
        expected_run_id=review.current_run_id,
    )
    assert ok is False
    assert "blocked human gate" in (reason or "")
    task = kb.get_task(board, task_id)
    assert task is not None
    assert (task.status, task.assignee) == ("running", "architect")


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
    )[0]

    second = kb.dispatch_once(board, spawn_fn=spawn)
    assert task_id in [item[0] for item in second.spawned]
    assert spawned[0][1] == "ironrod-ops"
    assert spawned[1][1] == "architect"
    assert "sdlc-review" in spawned[1][2]
