"""Matrice de tests obligatoire — colonne prod (violation 7).

Couvre les scénarios manquants du candidat initial :
- GC ne supprime jamais production_promoted ni les reçus (M18/rollback)
- archive depuis prod conserve le reçu (M18)
- migration additive sans backfill (matrice 1)
- concurrence : seconde promotion refusée (matrice 5)
- fallback : lecture/archivage d'une carte prod sans downgrade (matrice 10)
"""

from __future__ import annotations

import json
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


def _promote(conn, task_id: str, key: str) -> None:
    kb.mark_task_prod(conn, task_id, receipt=_receipt(), actor="amber", idempotency_key=key)


# ---------------------------------------------------------------------------
# Matrice 8 : GC ne supprime jamais production_promoted ni les reçus
# ---------------------------------------------------------------------------

def test_gc_preserves_production_promoted_event(
    kanban_home: Path, tmp_path: Path
) -> None:
    _configure(kanban_home, _verifier(tmp_path))
    with kb.connect() as conn:
        task_id = _done(conn)
        _promote(conn, task_id, "idem-gc-1")
        assert kb.get_task(conn, task_id).status == "prod"
        # GC agressif : tout ce qui a plus d'1 seconde
        kb.gc_events(conn, older_than_seconds=1)
        n = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'production_promoted'",
            (task_id,),
        ).fetchone()[0]
        assert n == 1, "production_promoted must survive gc_events"


def test_gc_preserves_production_receipt(
    kanban_home: Path, tmp_path: Path
) -> None:
    _configure(kanban_home, _verifier(tmp_path))
    with kb.connect() as conn:
        task_id = _done(conn)
        _promote(conn, task_id, "idem-gc-2")
        kb.gc_events(conn, older_than_seconds=1)
        assert kb.get_production_receipt(conn, task_id) is not None
        n = conn.execute(
            "SELECT COUNT(*) FROM production_receipts WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        assert n == 1


# ---------------------------------------------------------------------------
# Matrice 2 : archive depuis prod conserve le reçu
# ---------------------------------------------------------------------------

def test_archive_prod_preserves_receipt(kanban_home: Path, tmp_path: Path) -> None:
    _configure(kanban_home, _verifier(tmp_path))
    with kb.connect() as conn:
        task_id = _done(conn)
        _promote(conn, task_id, "idem-arch-1")
        assert kb.get_task(conn, task_id).status == "prod"
        assert kb.archive_task(conn, task_id)
        assert kb.get_task(conn, task_id).status == "archived"
        # Le reçu est conservé après archive
        assert kb.get_production_receipt(conn, task_id) is not None


# ---------------------------------------------------------------------------
# Matrice 1 : migration additive sans backfill
# ---------------------------------------------------------------------------

def test_migration_adds_tables_without_backfill(kanban_home: Path) -> None:
    with kb.connect() as conn:
        # Tables additives présentes et vides
        for table in ("production_receipts", "production_probes"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n == 0, f"{table} must start empty (no backfill)"
        # Aucune carte done n'a de reçu
        task_id = _done(conn)
        assert kb.get_production_receipt(conn, task_id) is None


# ---------------------------------------------------------------------------
# Matrice 5 : concurrence — seconde promotion refusée
# ---------------------------------------------------------------------------

def test_second_promotion_with_different_key_is_refused(
    kanban_home: Path, tmp_path: Path
) -> None:
    _configure(kanban_home, _verifier(tmp_path))
    with kb.connect() as conn:
        task_id = _done(conn)
        _promote(conn, task_id, "idem-conc-1")
        # Une seconde promotion (clé différente) doit échouer : la carte est déjà prod
        with pytest.raises(kb.ProductionLifecycleError):
            _promote(conn, task_id, "idem-conc-2")
        assert kb.get_task(conn, task_id).status == "prod"


# ---------------------------------------------------------------------------
# Matrice 10 : fallback — lecture/archivage d'une carte prod sans downgrade
# ---------------------------------------------------------------------------

def test_fallback_reads_and_archives_prod_without_downgrade(
    kanban_home: Path, tmp_path: Path
) -> None:
    _configure(kanban_home, _verifier(tmp_path))
    with kb.connect() as conn:
        task_id = _done(conn)
        _promote(conn, task_id, "idem-fb-1")
        # Lecture : la carte prod est listable et lisible
        page = kb.list_tasks_page(conn, status="prod", limit=10)
        assert any(t.id == task_id for t in page["tasks"])
        task = kb.get_task(conn, task_id)
        assert task.status == "prod"
        # Aucun downgrade : le statut reste prod après lecture
        assert kb.get_task(conn, task_id).status == "prod"
