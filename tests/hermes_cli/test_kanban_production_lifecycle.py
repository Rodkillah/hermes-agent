"""Behavioral coverage for the explicit done -> prod lifecycle."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _verifier(tmp_path: Path) -> Path:
    path = tmp_path / "verify-production.py"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        "print(json.dumps({\"ok\": True, \"task_id\": request[\"task\"][\"id\"],\n"
        "  \"receipt_id\": \"proof-1\", \"checked_at\": \"2026-08-27T10:00:00+00:00\",\n"
        "  \"facts\": {\"candidate_on_main\": True,\n"
        "    \"deployed_revision_matches\": True, \"backup_exists\": True,\n"
        "    \"rollback_available\": True, \"required_probes_passed\": True}}))\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _receipt() -> dict:
    return {
        "schema_version": 1,
        "environment": "production",
        "target": "ironrod",
        "deployed_at_utc": "2026-08-27T10:00:00+00:00",
        "candidate_sha": SHA,
        "deployed_identity_kind": "git_sha",
        "deployed_identity_value": SHA,
        "backup_ref": "backup-2026-08-27",
        "rollback_ref": "rollback-2026-08-27",
        "verification_mode": "system_verified",
        "probes": [
            {
                "name": "health",
                "scope": "live_production",
                "required": True,
                "result": "passed",
                "evidence_ref": "probe-health-1",
            }
        ],
    }


def _configure(home: Path, verifier: Path) -> None:
    meta_path = kb.board_metadata_path(kb.DEFAULT_BOARD)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "automation": {
                    "production": {
                        "enabled": True,
                        "allowed_profiles": ["amber"],
                        "verifier_command": str(verifier),
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _done(conn) -> str:
    task_id = kb.create_task(conn, title="candidate", assignee="worker")
    assert kb.complete_task(conn, task_id, result="implemented")
    assert kb.get_task(conn, task_id).status == "done"
    return task_id


def test_done_to_prod_stores_receipt_without_second_completion(
    kanban_home: Path, tmp_path: Path
) -> None:
    verifier = _verifier(tmp_path)
    _configure(kanban_home, verifier)
    with kb.connect() as conn:
        task_id = _done(conn)
        before = kb.get_task(conn, task_id)
        stored = kb.mark_task_prod(
            conn, task_id, receipt=_receipt(), actor="amber", idempotency_key="idem-1"
        )
        after = kb.get_task(conn, task_id)
        assert after.status == "prod"
        assert stored.task_id == task_id
        assert kb.get_production_receipt(conn, task_id).evidence_sha256 == stored.evidence_sha256
        assert after.completed_at == before.completed_at
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (task_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'production_promoted'",
            (task_id,),
        ).fetchone()[0] == 1


def test_prod_retry_is_idempotent_but_changed_proof_conflicts(
    kanban_home: Path, tmp_path: Path
) -> None:
    _configure(kanban_home, _verifier(tmp_path))
    with kb.connect() as conn:
        task_id = _done(conn)
        first = kb.mark_task_prod(
            conn, task_id, receipt=_receipt(), actor="amber", idempotency_key="idem-2"
        )
        retry = kb.mark_task_prod(
            conn, task_id, receipt=_receipt(), actor="amber", idempotency_key="idem-2"
        )
        assert retry.id == first.id
        changed = _receipt()
        changed["target"] = "different-target"
        with pytest.raises(kb.ProductionLifecycleError, match="idempotency key conflicts"):
            kb.mark_task_prod(
                conn, task_id, receipt=changed, actor="amber", idempotency_key="idem-2"
            )


def test_missing_verifier_leaves_done_card_untouched(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id = _done(conn)
        with pytest.raises(kb.ProductionLifecycleError):
            kb.mark_task_prod(
                conn, task_id, receipt=_receipt(), actor="amber", idempotency_key="idem-3"
            )
        assert kb.get_task(conn, task_id).status == "done"
        assert kb.get_production_receipt(conn, task_id) is None


def test_non_done_and_unauthorized_promotions_are_refused(
    kanban_home: Path, tmp_path: Path
) -> None:
    _configure(kanban_home, _verifier(tmp_path))
    with kb.connect() as conn:
        task_id = _done(conn)
        with pytest.raises(kb.ProductionLifecycleError, match="allowed"):
            kb.mark_task_prod(
                conn, task_id, receipt=_receipt(), actor="ironrod-ops", idempotency_key="idem-4"
            )
        task_id = kb.create_task(conn, title="not done", assignee="worker")
        with pytest.raises(kb.ProductionLifecycleError, match="only done"):
            kb.mark_task_prod(
                conn, task_id, receipt=_receipt(), actor="amber", idempotency_key="idem-5"
            )
        assert kb.get_task(conn, task_id).status == "ready"


def test_done_page_is_keyset_paginated_and_prod_is_a_valid_status(kanban_home: Path) -> None:
    with kb.connect() as conn:
        ids = [_done(conn) for _ in range(3)]
        page = kb.list_tasks_page(conn, status="done", limit=2)
        assert len(page["tasks"]) == 2
        assert page["has_more"] is True
        assert page["total_matching"] == 3
        next_page = kb.list_tasks_page(conn, status="done", limit=2, cursor=page["next_cursor"])
        assert sorted([task.id for task in page["tasks"]] + [task.id for task in next_page["tasks"]]) == sorted(ids)
        assert "prod" in kb.VALID_STATUSES
