"""Real bash regressions: a shared terminal snapshot is not execution identity."""
import os
import json
import shlex
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.delegation_context import delegated_child_context, is_delegated_child_context
from tools.environments.local import LocalEnvironment


@pytest.mark.parametrize("child_first", [False, True])
def test_parent_identity_survives_child_snapshot(monkeypatch, tmp_path, child_first):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fixture_parent")
    env = LocalEnvironment(cwd=str(tmp_path), timeout=5)
    try:
        if not child_first:
            env.init_session()
        with delegated_child_context():
            if child_first:
                env.init_session()
            child = env.execute('printf "CHILD=%s\\n" "$HERMES_DELEGATED_CHILD_CONTEXT"; export FIXTURE_SHARED=kept', timeout=5)
            assert "CHILD=1" in child["output"]
        assert not is_delegated_child_context()
        assert os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") is None
        parent = env.execute('printf "PARENT=%s SHARED=%s\\n" "${HERMES_DELEGATED_CHILD_CONTEXT:-absent}" "$FIXTURE_SHARED"', timeout=5)
        assert "PARENT=absent SHARED=kept" in parent["output"]
    finally:
        env.cleanup()


@pytest.mark.parametrize("inherited_child", [False, True])
def test_stale_snapshot_cannot_override_spawn_identity(monkeypatch, tmp_path, inherited_child):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fixture_parent")
    env = LocalEnvironment(cwd=str(tmp_path), timeout=5)
    try:
        env.init_session()
        # Old snapshots exist across code updates. Their identity is not authority.
        with Path(env._snapshot_path).open("a") as snapshot:
            snapshot.write('declare -x HERMES_DELEGATED_CHILD_CONTEXT="stale"\n')
            snapshot.write('declare -x HERMES_KANBAN_TASK="t_stale"\n')
        if inherited_child:
            monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
        result = env.execute('printf "IDENTITY=%s TASK=%s\\n" "${HERMES_DELEGATED_CHILD_CONTEXT:-absent}" "${HERMES_KANBAN_TASK:-absent}"', timeout=5)
        expected = "IDENTITY=1 TASK=absent" if inherited_child else "IDENTITY=absent TASK=t_fixture_parent"
        assert expected in result["output"]
    finally:
        env.cleanup()


def test_native_delegation_timeout_parent_cli_and_child_guard(monkeypatch, tmp_path):
    """Real delegate timeout + bash + CLI/DB, with no model or live board."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import delegate_tool
    from tests.tools.test_delegate_timeout_cleanup import _SlowUnwindingChild

    repo = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = kb.init_db()
    assert db.resolve().is_relative_to(tmp_path.resolve())
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fixture", assignee="fixture")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    env = LocalEnvironment(cwd=str(repo), timeout=10)

    def cli(action):
        # -c keeps cwd as the import root, avoiding the editable live install.
        code = (
            "import argparse; from pathlib import Path; "
            "from hermes_cli import kanban; "
            f"assert Path(kanban.__file__).resolve().is_relative_to(Path({str(repo)!r}).resolve()); "
            "p=argparse.ArgumentParser(); s=p.add_subparsers(dest='cmd'); "
            "kanban.build_parser(s); "
            f"a=p.parse_args(['kanban', {action!r}, '--json']); "
            "raise SystemExit(kanban.kanban_command(a))"
        )
        return env.execute(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}", timeout=10)

    child = _SlowUnwindingChild()
    probes = []
    parent = SimpleNamespace(session_id="fixture-parent", _current_task_id=None,
                             _active_children=[child], _active_children_lock=threading.Lock())
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 5)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    # Hold the fake LLM on an event, but use native timeout/ContextVar machinery.
    def child_turn(**kwargs):
        probes.append(cli("stats"))
        child.started.set()
        try:
            assert child.allow_finish.wait(30)
            return {"final_response": "", "completed": False, "api_calls": 1, "messages": []}
        finally:
            child.finished.set()

    child.run_conversation = child_turn
    try:
        env.init_session()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(delegate_tool._run_single_child, 0, "fixture timeout", child, parent)
            assert child.started.wait(15)
            result = future.result(timeout=15)
        assert result["status"] == "timeout"
        assert probes[0]["returncode"] == 1
        assert "could not initialize database" in probes[0]["output"]
        assert "delegate_task child contexts cannot mutate" in probes[0]["output"]
        assert not is_delegated_child_context()
        assert os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") is None
        # Still concurrent with the timed-out child, not only after its cleanup.
        for action in ("stats", "diagnostics"):
            result = cli(action)
            assert result["returncode"] == 0, result
            json.loads(result["output"])
        with kbc.connect() as conn:
            with delegated_child_context():
                with pytest.raises(PermissionError, match="delegate_task child"):
                    kb.create_task(conn, title="forbidden", assignee="fixture")
            task = kb.get_task(conn, tid)
            assert task is not None and task.title == "fixture"
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    finally:
        child.allow_finish.set()
        child.finished.wait(5)
        child.closed.wait(5)
        env.cleanup()
