"""Iron Rod regression: archived parents must BLOCK descendants.

Contract (verdict Architect t_4f42fd4a, PROD_NO_GO 2026-08-28):
a parent in 'archived' status must NOT satisfy dependency gating.
Only 'done' releases descendants. 'archived' keeps them blocked.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def _parent_child(conn, parent_status):
    parent = kb.create_task(conn, title="parent", assignee="setup")
    child = kb.create_task(
        conn, title="child", parents=[parent], assignee="setup"
    )
    conn.execute(
        "UPDATE tasks SET status=? WHERE id=?", (parent_status, parent)
    )
    return parent, child


def test_archived_parent_blocks_recompute_ready(conn):
    parent, child = _parent_child(conn, "archived")
    assert kb.get_task(conn, child).status == "todo"
    promoted = kb.recompute_ready(conn)
    assert promoted == 0
    assert kb.get_task(conn, child).status == "todo"


def test_archived_parent_blocks_promote_task(conn):
    parent, child = _parent_child(conn, "archived")
    ok, err = kb.promote_task(conn, child, actor="tester")
    assert ok is False
    assert "unsatisfied parent" in err
    assert kb.get_task(conn, child).status == "todo"


def test_archived_parent_blocks_claim_task(conn):
    parent, child = _parent_child(conn, "archived")
    # Force child to 'ready' to prove claim_task re-gates on archived parent.
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
    claimed = kb.claim_task(conn, child)
    assert claimed is None
    # claim_task demotes the racy 'ready' back to 'todo'.
    assert kb.get_task(conn, child).status == "todo"


def test_done_parent_still_releases(conn):
    parent, child = _parent_child(conn, "done")
    assert kb.get_task(conn, child).status == "todo"
    promoted = kb.recompute_ready(conn)
    assert promoted == 1
    assert kb.get_task(conn, child).status == "ready"
