"""Native multi-row recovery contention and snapshot fencing contracts."""
import json
import sqlite3
import subprocess
import sys
import time

import pytest

from cron import executions as e


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(e, "EXECUTIONS_FILE", tmp_path / "executions.db")
    monkeypatch.setattr(e, "_emit_execution_state", lambda *a, **k: None)
    seed = e.create_execution("seed", source="fixture")
    e.finish_execution(seed["id"], success=True)
    return tmp_path


def _wait(path, children, seconds=10):
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert all(p.poll() is None for p in children)
        assert time.monotonic() < deadline
        time.sleep(.01)


RECOVERY = r'''
import json, sys, time
from contextlib import contextmanager
from pathlib import Path
from cron import executions as e
e.EXECUTIONS_FILE = Path(sys.argv[1])
root = Path(sys.argv[2])
e._emit_execution_state = lambda *a, **k: None
import sqlite3
stats = {"opens": 0, "schema_calls": 0, "ddl": 0, "writer_seconds": 0, "probes": 0}
real_connect = sqlite3.connect
class MeasuredConnection(sqlite3.Connection):
    write_started = None
    def execute(self, sql, *args):
        verb = sql.split()[0].upper()
        if verb in ("CREATE", "ALTER"):
            stats["ddl"] += 1
        result = super().execute(sql, *args)
        if verb in ("UPDATE", "DELETE", "INSERT") and self.write_started is None:
            self.write_started = time.monotonic()
        return result
    def __exit__(self, *args):
        result = super().__exit__(*args)
        if self.write_started is not None:
            stats["writer_seconds"] += time.monotonic() - self.write_started
            self.write_started = None
        return result
def measured_connect(*args, **kwargs):
    stats["opens"] += 1
    return real_connect(*args, **kwargs, factory=MeasuredConnection)
sqlite3.connect = measured_connect
initialize = e._initialize_schema
def measured_schema(conn):
    stats["schema_calls"] += 1
    initialize(conn)
e._initialize_schema = measured_schema
native_probe = e._owner_is_live
def probe(pid, started):
    stats["probes"] += 1
    if pid == int(sys.argv[3]):
        (root / "probe.ready").touch()
        deadline = time.monotonic() + 15
        while not (root / "probe.release").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("fixture probe release missing")
            time.sleep(.01)
    return native_probe(pid, started)
e._owner_is_live = probe
started = time.monotonic()
stats["changed"] = e.recover_interrupted_executions()
stats["seconds"] = time.monotonic() - started
(root / "recovery.json").write_text(json.dumps(stats))
'''

ADOPTER = r'''
import json, sys, time
from pathlib import Path
from cron import executions as e
e.EXECUTIONS_FILE = Path(sys.argv[1])
e._emit_execution_state = lambda *a, **k: None
root, ident = Path(sys.argv[2]), sys.argv[3]
# Open natively before the contender's write, so baseline fails at the adoption UPDATE.
preopened = e._connect()
connect = e._connect
first = True
def prepared_connect(**kwargs):
    global first
    if first:
        first = False
        return preopened
    return connect(**kwargs)
e._connect = prepared_connect
(root / (ident + ".ready")).touch()
deadline = time.monotonic() + 20
while not (root / "adopt.go").exists():
    assert time.monotonic() < deadline
    time.sleep(.01)
started = time.monotonic()
try:
    row = e.adopt_claimed_execution(ident)
    assert row and row["status"] == "running"
    with (root / (ident + ".effect")).open("x") as f:
        f.write(e._PROCESS_ID)
    assert e.touch_execution_progress(ident)
    for _ in range(3):
        assert e.get_execution(ident)["status"] == "running"
    assert e.finish_execution(ident, success=True)["status"] == "completed"
    assert e.adopt_claimed_execution(ident) is None
    result = {"status": "completed", "seconds": time.monotonic() - started, "reads": 3}
except Exception as exc:
    result = {"status": "error", "error": str(exc), "code": getattr(exc, "sqlite_errorcode", None),
              "seconds": time.monotonic() - started}
(root / (ident + ".json")).write_text(json.dumps(result))
'''


# Use the same SQL instrumentation for the worker and finish/prune contender.
MEASUREMENT = RECOVERY[RECOVERY.index("import sqlite3"):RECOVERY.index("native_probe =")]
ADOPTER = ADOPTER.replace("root, ident =", MEASUREMENT + "\nroot, ident =")
ADOPTER = ADOPTER.replace('(root / (ident + ".json")).write_text',
                          'result.update(stats)\n(root / (ident + ".json")).write_text')
PRUNER = RECOVERY[:RECOVERY.index("native_probe =")] + r'''
original_execute = MeasuredConnection.execute
blocked = False
def slow_sort(self, sql, *args):
    global blocked
    if "julianday(finished_at)" not in sql or blocked:
        return original_execute(self, sql, *args)
    def pause_vm():
        global blocked
        if not blocked:
            blocked = True
            (root / "sort.ready").touch()
            deadline = time.monotonic() + 15
            while not (root / "sort.release").exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("fixture sort release missing")
                time.sleep(.01)
        return 0
    self.set_progress_handler(pause_vm, 1)
    try:
        return original_execute(self, sql, *args)
    finally:
        self.set_progress_handler(None, 0)
MeasuredConnection.execute = slow_sort
row = e.create_execution("finisher", source="fixture")
started = time.monotonic()
assert e.finish_execution(row["id"], success=True)["status"] == "completed"
stats["seconds"] = time.monotonic() - started
(root / "prune.json").write_text(json.dumps(stats))
'''


def test_native_recovery_allows_distinct_adopters_and_prune(ledger, capsys):
    ids = []
    for n in range(3):
        row = e.create_execution(f"adopter-{n}", source="fixture")
        e.mark_execution_handoff_pending(row["id"])
        ids.append(row["id"])
    dead_pids = []
    for _ in range(2):
        owner = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_pids.append(owner.pid)
        assert owner.wait(timeout=10) == 0
    with e._transaction() as conn:
        # History above retention forces the workers' native finish path to DELETE old rows.
        conn.executemany(
            "INSERT INTO executions (id,job_id,source,process_id,pid,status,claimed_at,finished_at) "
            "VALUES (?,?,'fixture','historic',1,'completed',?,?)",
            [(f"history-{n}", "history", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00")
             for n in range(1005)])
        for n in range(2):
            conn.execute("INSERT INTO executions (id,job_id,source,process_id,pid,status,claimed_at) "
                         "VALUES (?,?,?,'dead-owner',?,'running',?)",
                         (f"orphan-{n}", f"orphan-{n}", "fixture", dead_pids[n], f"{2021-n}-01-01T00:00:00+00:00"))
    workers = [subprocess.Popen([sys.executable, "-c", ADOPTER,
                str(e.EXECUTIONS_FILE), str(ledger), ident]) for ident in ids]
    for ident in ids:
        _wait(ledger / (ident + ".ready"), workers)
    recovery = subprocess.Popen([sys.executable, "-c", RECOVERY, str(e.EXECUTIONS_FILE), str(ledger), str(dead_pids[1])])
    observations = []
    try:
        _wait(ledger / "probe.ready", [recovery])
        (ledger / "adopt.go").touch()
        # Native busy timeout is 5s; hold the owner probe until the adopters report or 6s elapse.
        deadline = time.monotonic() + 6
        while not all((ledger / (ident + ".json")).exists() for ident in ids):
            if time.monotonic() >= deadline:
                break
            time.sleep(.01)
        for ident in ids:
            path = ledger / (ident + ".json")
            observations.append(json.loads(path.read_text()) if path.exists() else {"status": "blocked"})
    finally:
        (ledger / "adopt.go").touch()
        (ledger / "probe.release").touch()
        assert recovery.wait(timeout=10) == 0
        for worker in workers:
            assert worker.wait(timeout=15) == 0
    stats = json.loads((ledger / "recovery.json").read_text())
    with capsys.disabled():
        print("NATIVE_RECOVERY", json.dumps({"recovery": stats, "adopters": observations}), flush=True)
    assert all(row["status"] == "completed" for row in observations)
    assert stats["changed"] == 2
    assert stats["writer_seconds"] < 2
    assert all(e.get_execution(ident)["status"] == "completed" for ident in ids)
    assert len(list(ledger.glob("*.effect"))) == 3
    with e._transaction() as conn:
        assert conn.execute("SELECT count(*) FROM executions WHERE status IN ('completed','failed','unknown')").fetchone()[0] <= e.MAX_TERMINAL_EXECUTIONS
    assert e.recover_interrupted_executions() == 0


@pytest.mark.parametrize("field,value", [
    ("progress_at", "2099-01-01T00:00:00+00:00"),
    ("process_started_at", 12345),
    ("claimed_at", "2099-01-01T00:00:00+00:00"),
    ("handoff_pending", 1),
    ("handoff_started_at", 12345.0),
])
def test_recovery_does_not_terminalize_changed_snapshot(ledger, monkeypatch, field, value):
    row = e.create_execution("progress-owner", source="fixture")
    with e._transaction() as conn:
        conn.execute("UPDATE executions SET process_id='other', status='running' WHERE id=?", (row["id"],))
    def progressing(*args):
        with sqlite3.connect(e.EXECUTIONS_FILE) as writer:
            writer.execute(f"UPDATE executions SET {field}=? WHERE id=?", (value, row["id"]))
        return True
    monkeypatch.setattr(e, "_owner_is_live", progressing)
    monkeypatch.setattr(e, "_live_owner_stale_after_seconds", lambda: 1)
    monkeypatch.setattr(e, "_stale_age_seconds", lambda *a: 100)
    assert e.recover_interrupted_executions() == 0
    current = e.get_execution(row["id"])
    assert current["status"] == "running"
    assert current[field] == value


def test_existing_schema_reads_issue_no_ddl(ledger, monkeypatch, capsys):
    statements = []
    real_connect = e.sqlite3.connect
    attempted_ddl = []
    class CountingConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.split()[0].upper() in ("CREATE", "ALTER"):
                attempted_ddl.append(sql)
            return super().execute(sql, *args)
    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs, factory=CountingConnection)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(e.sqlite3, "connect", traced)
    started = time.monotonic()
    for _ in range(8):
        assert e.get_execution("absent") is None
    ddl = [sql for sql in statements if sql.split()[0].upper() in ("CREATE", "ALTER")]
    with capsys.disabled():
        print("SCHEMA_READS", json.dumps({"opens": 8, "ddl": len(attempted_ddl),
                                         "seconds": time.monotonic()-started}), flush=True)
    assert attempted_ddl == []


def test_adoption_propagates_real_readonly_database_error(ledger, monkeypatch):
    row = e.create_execution("readonly", source="fixture")
    e.mark_execution_handoff_pending(row["id"])
    calls = []
    def readonly(**kwargs):
        calls.append(1)
        conn = sqlite3.connect(f"file:{e.EXECUTIONS_FILE}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(e, "_connect", readonly)
    with pytest.raises(sqlite3.OperationalError) as error:
        e.adopt_claimed_execution(row["id"])
    assert error.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
    assert len(calls) == 1
    with sqlite3.connect(e.EXECUTIONS_FILE) as conn:
        assert conn.execute("SELECT status FROM executions WHERE id=?", (row["id"],)).fetchone()[0] == "claimed"


def test_recovery_fences_adoption_during_owner_probe(ledger, monkeypatch):
    row = e.create_execution("handoff-race", source="fixture")
    e.mark_execution_handoff_pending(row["id"])
    with e._transaction() as conn:
        conn.execute("UPDATE executions SET process_id='old', handoff_started_at=0 WHERE id=?", (row["id"],))
    def adopt(*args):
        assert e.adopt_claimed_execution(row["id"])["status"] == "running"
        return False
    monkeypatch.setattr(e, "_owner_is_live", adopt)
    assert e.recover_interrupted_executions() == 0
    assert e.get_execution(row["id"])["status"] == "running"
    assert e.adopt_claimed_execution(row["id"]) is None


def test_native_finish_sort_does_not_hold_writer_lock(ledger, capsys):
    row = e.create_execution("adopter-during-prune", source="fixture")
    e.mark_execution_handoff_pending(row["id"])
    worker = subprocess.Popen([sys.executable, "-c", ADOPTER,
                str(e.EXECUTIONS_FILE), str(ledger), row["id"]])
    _wait(ledger / (row["id"] + ".ready"), [worker])
    pruner = subprocess.Popen([sys.executable, "-c", PRUNER, str(e.EXECUTIONS_FILE), str(ledger)])
    result = {"status": "blocked"}
    try:
        _wait(ledger / "sort.ready", [pruner])
        (ledger / "adopt.go").touch()
        path = ledger / (row["id"] + ".json")
        deadline = time.monotonic() + 6
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        if path.exists():
            result = json.loads(path.read_text())
    finally:
        (ledger / "adopt.go").touch()
        (ledger / "sort.release").touch()
        assert pruner.wait(timeout=10) == 0
        if worker:
            assert worker.wait(timeout=15) == 0
    stats = json.loads((ledger / "prune.json").read_text())
    with capsys.disabled():
        print("NATIVE_FINISH", json.dumps({"prune": stats, "adopter": result}), flush=True)
    assert result["status"] == "completed"
    assert stats["writer_seconds"] < 2
