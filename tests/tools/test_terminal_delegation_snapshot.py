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


_IDENTITE = ('printf "TASK=%s CHILD=%s SHARED=%s\\n" '
             '"${HERMES_KANBAN_TASK:-absent}" '
             '"${HERMES_DELEGATED_CHILD_CONTEXT:-absent}" '
             '"${FIXTURE_SHARED:-absent}"')


@pytest.mark.parametrize("child_first", [False, True])
def test_shell_never_carries_worker_identity(monkeypatch, tmp_path, child_first):
    """Aucun shell ne porte l'identite Kanban, pas meme celui du worker parent.

    Contrat amont depuis b578261808 (mesure le 2026-09-11) : l'identite de tache
    ne franchit pas la frontiere du processus. Un descendant recoit a la place un
    marqueur de refus. L'ancien invariant, ou le shell du parent gardait
    HERMES_KANBAN_TASK, est mort ; celui qui le remplace est teste ici.
    L'etat de shell voulu par l'utilisateur, lui, continue de traverser.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fixture_parent")
    env = LocalEnvironment(cwd=str(tmp_path), timeout=5)
    try:
        if not child_first:
            env.init_session()
        with delegated_child_context():
            if child_first:
                env.init_session()
            child = env.execute("export FIXTURE_SHARED=kept; " + _IDENTITE, timeout=5)
            assert "TASK=absent" in child["output"], child["output"]
            assert "CHILD=1" in child["output"], child["output"]

        # Le contexte enfant ne fuit pas dans le processus agent.
        assert not is_delegated_child_context()
        assert os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") is None
        # ... et l'agent, lui, garde son identite : c'est par ses outils natifs
        # qu'il transitionne sa carte, pas par un shell.
        assert os.environ["HERMES_KANBAN_TASK"] == "t_fixture_parent"

        parent = env.execute(_IDENTITE, timeout=5)
        assert "TASK=absent" in parent["output"], parent["output"]
        assert "SHARED=kept" in parent["output"], parent["output"]
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
        # Invariant Iron Rod, toujours valable : le snapshot n'est pas une autorite.
        # Il ne peut reinjecter ni l'identite de tache ni le marqueur, quelles que
        # soient les valeurs qu'il porte (ici t_stale et "stale").
        assert "TASK=t_stale" not in result["output"], result["output"]
        assert 'IDENTITY=stale' not in result["output"], result["output"]
        # Contrat amont : le shell est scrute, donc la tache est absente des deux cotes.
        assert "TASK=absent" in result["output"], result["output"]
        assert "IDENTITY=1" in result["output"], result["output"]
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

    def cli(action, *extra):
        # -c keeps cwd as the import root, avoiding the editable live install.
        code = (
            "import argparse; from pathlib import Path; "
            "from hermes_cli import kanban; "
            f"assert Path(kanban.__file__).resolve().is_relative_to(Path({str(repo)!r}).resolve()); "
            "p=argparse.ArgumentParser(); s=p.add_subparsers(dest='cmd'); "
            "kanban.build_parser(s); "
            f"a=p.parse_args(['kanban', {action!r}, *{list(extra)!r}] + ([] if {bool(extra)!r} else ['--json'])); "
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
        probes.append(cli("create", "interdit-par-le-contrat", "--assignee", "fixture"))
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
        # L'enfant garde le routage du board : la LECTURE aboutit. C'est
        # l'assouplissement amont, mesure le 2026-09-11 (avant, il ne pouvait meme
        # pas ouvrir la base).
        assert probes[0]["returncode"] == 0, probes[0]
        json.loads(probes[0]["output"])
        # Mais la MUTATION lui est refusee, et c'est la ce qui doit rester prouve.
        assert probes[1]["returncode"] != 0, probes[1]
        assert "cannot mutate" in probes[1]["output"], probes[1]["output"]
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

            # La contrepartie indispensable du bridage : brider l'enfant ne doit
            # pas brider le parent. Apres le retour de l'enfant, le worker doit
            # pouvoir clore SA carte, sinon le durcissement l'aurait enferme.
            assert not is_delegated_child_context()
            assert kb.claim_task(conn, tid, claimer="fixture") is not None
            assert kb.complete_task(conn, tid, result="fait apres le retour de l enfant")
            assert kb.get_task(conn, tid).status == "done"
    finally:
        child.allow_finish.set()
        child.finished.wait(5)
        child.closed.wait(5)
        env.cleanup()
