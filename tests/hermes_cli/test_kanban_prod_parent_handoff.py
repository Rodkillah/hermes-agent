"""Iron Rod regression: a 'prod' parent must hand its result down to its child.

Contract: ``WORK_COMPLETED_STATUSES`` (kanban_db.py) is the single definition of
"this parent delivered". Dependency gating already honours it, so a child whose
parent was promoted to 'prod' is released and starts working. The worker context
must honour the same predicate, otherwise that child starts BLIND: no error, no
blocked card, no log line, just a missing handoff.

The predicate lives in ``_ctx_parent_results``, reached only through the public
``build_worker_context``. It was lost on 2026-09-11 when the upstream
decomposition rewrote the function, and the Iron Rod patch landed instead in the
``parent_results`` copy inside the PLUGIN-COMPAT block, which has no callers and
is removed on 2026-09-14. This test is what survives the next upstream merge.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


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
    with kbc.connect() as c:
        yield c


# A value no other fixture or fallback string can produce, so that finding it in
# the rendered context proves it came from the parent's own result.
HANDOFF = "SENTINELLE-HANDOFF-PROD-7f3a"


def _handoff_section(conn, child):
    """The '## Parent task results' section alone. Asserting on the whole worker
    context would let any other section (role history, comments, prior attempts)
    satisfy the assertion and turn this file into a probe that cannot fail."""
    ctx = kb.build_worker_context(conn, child)
    if "## Parent task results" not in ctx:
        return ""
    tail = ctx.split("## Parent task results", 1)[1]
    return tail.split("\n## ", 1)[0]


def _parent_child(conn, parent_status):
    """Build a real parent->child link, complete the parent, then force its
    status. Completion goes through the public API so ``result`` and
    ``completed_at`` are set the way production sets them; only the final status
    is forced, because reaching 'prod' for real needs a deployment receipt and
    an external verification command that have nothing to do with this contract.
    """
    # Distinct assignees on purpose: with a shared one, the parent's result also
    # reaches the child through _ctx_role_history ("the assignee's recent work"),
    # and a handoff assertion would pass without the handoff ever being rendered.
    parent = kb.create_task(conn, title="parent", assignee="upstream-worker")
    child = kb.create_task(conn, title="child", parents=[parent], assignee="downstream-worker")
    kb.claim_task(conn, parent, claimer="upstream-worker")
    kb.complete_task(conn, parent, result=HANDOFF)
    assert kb.get_task(conn, parent).status == "done"
    if parent_status != "done":
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (parent_status, parent))
    assert kb.get_task(conn, parent).status == parent_status
    return parent, child


def test_prod_parent_hands_off_to_child(conn):
    """The defect itself: a promoted parent must still be heard."""
    parent, child = _parent_child(conn, "prod")
    section = _handoff_section(conn, child)
    assert section, "no '## Parent task results' section was rendered at all"
    assert parent in section
    assert HANDOFF in section


def test_done_parent_still_hands_off(conn):
    """Conservation: widening the predicate must not cost us the 'done' case,
    which is the one that already worked."""
    parent, child = _parent_child(conn, "done")
    section = _handoff_section(conn, child)
    assert section, "the 'done' case regressed: no handoff section rendered"
    assert HANDOFF in section


def test_archived_parent_hands_off_nothing(conn):
    """The counterpart that stops the fix from over-widening. 'archived' is a
    terminal status but NOT a completion: it is in EXECUTION_TERMINAL_STATUSES
    and deliberately absent from WORK_COMPLETED_STATUSES. Using the wrong
    constant would make this test red, which is the whole point of having it.
    """
    _parent, child = _parent_child(conn, "archived")
    assert _handoff_section(conn, child) == ""


def test_predicate_is_the_shared_constant_not_a_local_copy(conn):
    """The drift guard. kanban_db.py:93-95 asks for these predicates to stay in
    one place; the defect this file covers was born of a local copy. Adding a
    status to WORK_COMPLETED_STATUSES must change the handoff too, with no edit
    to _ctx_parent_results. If someone re-hardcodes a tuple, this goes red.
    """
    parent, child = _parent_child(conn, "done")
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (parent,))
    assert _handoff_section(conn, child) == ""

    original = kb.WORK_COMPLETED_STATUSES
    try:
        kb.WORK_COMPLETED_STATUSES = frozenset(original | {"review"})
        section = _handoff_section(conn, child)
    finally:
        kb.WORK_COMPLETED_STATUSES = original
    assert HANDOFF in section, (
        "the handoff predicate does not read WORK_COMPLETED_STATUSES; it is a "
        "local copy, and it will drift again at the next upstream merge"
    )
