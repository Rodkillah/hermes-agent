"""Real WAL writer contention at the external-worker adoption gate."""
import sqlite3
import subprocess
import sys
import time

import pytest


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from cron import executions
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    row = executions.create_execution("fixture", source="test")
    executions.mark_execution_handoff_pending(row["id"])
    return executions, row["id"]


def _writer(ledger, tmp_path, seconds):
    executions, execution_id = ledger
    ready = tmp_path / "writer.ready"
    code = """
import sqlite3, sys, time
from pathlib import Path
conn = sqlite3.connect(sys.argv[1])
assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
conn.execute('UPDATE executions SET error=? WHERE id=?', ('fixture writer', sys.argv[2]))
Path(sys.argv[3]).touch()
time.sleep(float(sys.argv[4]))
conn.commit()
conn.close()
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(executions.EXECUTIONS_FILE),
                              execution_id, str(ready), str(seconds)])
    deadline = time.monotonic() + 5
    while not ready.exists():
        assert child.poll() is None
        assert time.monotonic() < deadline
        time.sleep(.01)
    return child


def _start_writer_after_open(ledger, tmp_path, monkeypatch, seconds):
    executions, _ = ledger
    connect = executions._connect
    children = []
    def open_then_contend(**kwargs):
        conn = connect(**kwargs)
        if not children:
            children.append(_writer(ledger, tmp_path, seconds))
        return conn
    monkeypatch.setattr(executions, "_connect", open_then_contend)
    return children


def test_adoption_survives_writer_longer_than_one_busy_wait(ledger, tmp_path, monkeypatch):
    executions, execution_id = ledger
    # Supplemental retry stress test; production keeps the native five-second bound.
    monkeypatch.setattr(executions, "ADOPTION_BUSY_BUDGET_SECONDS", 10)
    children = _start_writer_after_open(ledger, tmp_path, monkeypatch, 6)
    try:
        adopted = executions.adopt_claimed_execution(execution_id)
        assert adopted["status"] == "running"
        assert executions.get_execution(execution_id) == adopted
        assert executions.adopt_claimed_execution(execution_id) is None
        assert executions.finish_execution(execution_id, success=True)["status"] == "completed"
        assert executions.adopt_claimed_execution(execution_id) is None
        assert executions.finish_execution(execution_id, success=False) is None
    finally:
        children[0].wait(timeout=10)
    assert children[0].returncode == 0


def test_adoption_busy_budget_is_bounded_and_recoverable(ledger, tmp_path, monkeypatch):
    executions, execution_id = ledger
    monkeypatch.setattr(executions, "ADOPTION_BUSY_BUDGET_SECONDS", .25)
    children = _start_writer_after_open(ledger, tmp_path, monkeypatch, 3)
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError) as error:
            executions.adopt_claimed_execution(execution_id)
        assert error.value.sqlite_errorcode & 0xff == sqlite3.SQLITE_BUSY
        assert time.monotonic() - started < 2
    finally:
        children[0].wait(timeout=10)
    assert executions.get_execution(execution_id)["status"] == "claimed"
    assert executions.adopt_claimed_execution(execution_id)["status"] == "running"


@pytest.mark.parametrize("code", [None, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_IOERR, sqlite3.SQLITE_READONLY])
def test_adoption_propagates_non_contention_error(ledger, monkeypatch, code):
    executions, execution_id = ledger
    calls = []
    def broken_connect(**kwargs):
        calls.append(kwargs)
        error = sqlite3.OperationalError("fixture disk error")
        if code is not None:
            error.sqlite_errorcode = code
        raise error
    monkeypatch.setattr(executions, "_connect", broken_connect)
    with pytest.raises(sqlite3.OperationalError, match="fixture disk error"):
        executions.adopt_claimed_execution(execution_id)
    assert len(calls) == 1


def test_competing_processes_have_one_durable_winner(ledger, tmp_path):
    executions, execution_id = ledger
    gate = tmp_path / "start"
    code = """
import sys, time
from pathlib import Path
from cron import executions as e
e.EXECUTIONS_FILE = Path(sys.argv[1])
ready, gate, effect = map(Path, sys.argv[3:6])
ready.touch()
while not gate.exists():
    time.sleep(.01)
if e.adopt_claimed_execution(sys.argv[2]) is not None:
    with effect.open('x') as stream:
        stream.write(e._PROCESS_ID)
    assert e.finish_execution(sys.argv[2], success=True)['status'] == 'completed'
"""
    effect = tmp_path / "effect"
    children = []
    try:
        for number in range(3):
            ready = tmp_path / f"ready-{number}"
            children.append(subprocess.Popen([sys.executable, "-c", code,
                str(executions.EXECUTIONS_FILE), execution_id, str(ready), str(gate), str(effect)]))
        deadline = time.monotonic() + 10
        while len(list(tmp_path.glob("ready-*"))) != 3:
            assert all(child.poll() is None for child in children)
            assert time.monotonic() < deadline
            time.sleep(.01)
        gate.touch()
        for child in children:
            assert child.wait(timeout=15) == 0
    finally:
        gate.touch()
        for child in children:
            child.wait(timeout=15)
    record = executions.get_execution(execution_id)
    assert record["status"] == "completed"
    assert record["process_id"] == effect.read_text()
    assert executions.finish_execution(execution_id, success=False) is None
    assert executions.adopt_claimed_execution(execution_id) is None


@pytest.mark.parametrize("lose_ack", [False, True])
def test_worker_ack_follows_durable_adoption_and_never_replays(
    ledger, tmp_path, monkeypatch, lose_ack
):
    import json
    from cron import scheduler
    executions, execution_id = ledger
    ack = tmp_path / "worker.ready"
    payload = tmp_path / "payload.json"
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr("hermes_cli.env_loader.hydrate_profile_secret_sources", lambda _home: None)
    monkeypatch.setattr("gateway.run_startup.GatewayStartupMixin._register_config_hooks", lambda *a, **k: None)
    effects = []
    def run(*args, **kwargs):
        assert executions.get_execution(execution_id)["status"] == "running"
        assert json.loads(ack.read_text())["execution_id"] == execution_id
        effects.append(execution_id)
        executions.finish_execution(execution_id, success=True)
        return True
    monkeypatch.setattr(scheduler, "run_one_job", run)
    replace = scheduler.os.replace
    def publish(src, dst):
        # Independent reader observes committed ownership before the ACK appears.
        with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
            assert conn.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()[0] == "running"
        assert not ack.exists()
        if lose_ack:
            raise OSError("fixture lost ACK")
        replace(src, dst)
    monkeypatch.setattr(scheduler.os, "replace", publish)
    def invoke():
        payload.write_text(json.dumps({"job": {"id": "fixture", "execution_id": execution_id},
                                       "profile_home": str(home)}))
        return scheduler._run_external_worker_payload(payload, ack)
    assert invoke() is (not lose_ack)
    assert invoke() is False
    assert effects == ([] if lose_ack else [execution_id])
    assert executions.get_execution(execution_id)["status"] == ("running" if lose_ack else "completed")
    if lose_ack:
        monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-scheduler")
        monkeypatch.setattr(executions, "_owner_is_live", lambda *args: False)
        assert executions.recover_interrupted_executions() == 1
        assert executions.get_execution(execution_id)["status"] == "unknown"
        assert executions.adopt_claimed_execution(execution_id) is None


def test_adoption_reopens_after_busy_snapshot(ledger, monkeypatch):
    executions, execution_id = ledger
    connect = executions._connect
    connections = []
    def stale_snapshot_once(**kwargs):
        conn = connect(**kwargs)
        connections.append(conn)
        if len(connections) == 1:
            conn.execute("BEGIN")
            conn.execute("SELECT * FROM executions").fetchall()
            with sqlite3.connect(executions.EXECUTIONS_FILE) as writer:
                writer.execute("UPDATE executions SET error=? WHERE id=?", ("new snapshot", execution_id))
        return conn
    monkeypatch.setattr(executions, "_connect", stale_snapshot_once)
    assert executions.adopt_claimed_execution(execution_id)["status"] == "running"
    assert len(connections) == 2
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")
