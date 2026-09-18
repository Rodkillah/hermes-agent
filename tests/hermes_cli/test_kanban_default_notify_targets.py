"""Tests for ``kanban.default_notify_targets`` — configured per-board default
notification targets applied to every new task (tool + CLI + dashboard) in the
common ``kanban_db.create_task`` path, in addition to the creator's
auto-subscription.

Covers the acceptance criteria:
  - red: creation without ``HERMES_SESSION_*`` + a configured target produces an
    exact Forge/Telegram/DM/``notify+wake`` subscription;
  - nominal: a persistent creator session keeps its own subscription, and a
    duplicate ``(task, platform, chat, thread)`` is not double-inserted;
  - safety: no configured target preserves upstream behaviour (no implicit
    destination, no invented chat);
  - fail-closed: a non-empty invalid config aborts creation with no orphan task
    and no private routing data in the error.
"""

from __future__ import annotations

import json
import threading

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import projects_db as pdb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(__import__("pathlib").Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    kb.init_db()
    return home


def _write_config(home, targets):
    """Write a config.yaml carrying ``kanban.default_notify_targets`` (and the
    default ``auto_subscribe_on_create: true``) into the isolated home."""
    (home / "config.yaml").write_text(
        "kanban:\n"
        "  auto_subscribe_on_create: true\n"
        "  default_notify_targets: " + json.dumps(targets) + "\n"
    )


def _forge_target(board="iron-rod", **overrides):
    target = {
        "board": board,
        "platform": "telegram",
        "chat_id": "forge-chat",
        "thread_id": "forge-thread",
        "chat_type": "dm",
        "notifier_profile": "forge",
        "delivery_mode": "notify+wake",
    }
    target.update(overrides)
    return target


def _subs(conn, task_id):
    return kbn.list_notify_subs(conn, task_id)


# ---------------------------------------------------------------------------
# normalize_default_notify_targets — pure validation/normalization
# ---------------------------------------------------------------------------

def test_normalize_empty_and_none():
    assert kbn.normalize_default_notify_targets(None) == []
    assert kbn.normalize_default_notify_targets([]) == []


def test_normalize_valid_target():
    out = kbn.normalize_default_notify_targets([_forge_target()])
    assert len(out) == 1
    t = out[0]
    assert t["board"] == "iron-rod"
    assert t["platform"] == "telegram"
    assert t["chat_id"] == "forge-chat"
    assert t["thread_id"] == "forge-thread"
    assert t["chat_type"] == "dm"
    assert t["notifier_profile"] == "forge"
    assert t["delivery_mode"] == "notify+wake"


def test_normalize_uses_canonical_board_slug_and_platform():
    out = kbn.normalize_default_notify_targets([
        _forge_target(board=" Iron-Rod ", platform=" TELEGRAM ")
    ])
    assert out[0]["board"] == "iron-rod"
    assert out[0]["platform"] == "telegram"


def test_normalize_rejects_unknown_platform_without_echoing_route():
    secret_platform = "SECRET-UNRESOLVABLE-PLATFORM"
    with pytest.raises(ValueError, match="unsupported platform") as exc:
        kbn.normalize_default_notify_targets([
            _forge_target(platform=secret_platform)
        ])
    assert secret_platform not in str(exc.value)


def test_normalize_optional_fields_default_to_none():
    out = kbn.normalize_default_notify_targets([{
        "board": "iron-rod", "platform": "telegram", "chat_id": "c",
        "delivery_mode": "notify",
    }])
    t = out[0]
    assert t["thread_id"] is None
    assert t["chat_type"] is None
    assert t["user_id"] is None
    assert t["user_id_alt"] is None
    assert t["notifier_profile"] is None
    assert t["delivery_metadata"] is None


def test_normalize_rejects_non_list():
    with pytest.raises(ValueError, match="must be a list"):
        kbn.normalize_default_notify_targets({"board": "iron-rod"})


def test_normalize_rejects_non_mapping_entry():
    with pytest.raises(ValueError, match=r"\[0\] must be a mapping"):
        kbn.normalize_default_notify_targets(["not-a-mapping"])


def test_normalize_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown key"):
        kbn.normalize_default_notify_targets([{**_forge_target(), "secret": "x"}])


def test_normalize_rejects_missing_required_key():
    with pytest.raises(ValueError, match="missing required key"):
        kbn.normalize_default_notify_targets([{
            "board": "iron-rod", "platform": "telegram", "chat_id": "c",
        }])


def test_normalize_rejects_bad_delivery_mode():
    with pytest.raises(ValueError, match="delivery_mode"):
        kbn.normalize_default_notify_targets([{
            "board": "iron-rod", "platform": "telegram", "chat_id": "c",
            "delivery_mode": "bogus",
        }])


def test_normalize_requires_notifier_profile_for_wake():
    with pytest.raises(ValueError, match="notifier_profile"):
        kbn.normalize_default_notify_targets([{
            "board": "iron-rod", "platform": "telegram", "chat_id": "c",
            "delivery_mode": "notify+wake",
        }])


def test_normalize_rejects_non_mapping_delivery_metadata():
    with pytest.raises(ValueError, match="delivery_metadata"):
        kbn.normalize_default_notify_targets([{
            **_forge_target(), "delivery_metadata": "not-a-mapping",
        }])


def test_normalize_error_never_leaks_chat_id():
    """A validation error must not echo private routing data (chat_id/thread_id)."""
    with pytest.raises(ValueError) as exc:
        kbn.normalize_default_notify_targets([{
            "board": "iron-rod", "platform": "telegram", "chat_id": "SECRET-CHAT",
            "delivery_mode": "bogus",
        }])
    assert "SECRET-CHAT" not in str(exc.value)


# ---------------------------------------------------------------------------
# apply_default_notify_targets — idempotent insertion
# ---------------------------------------------------------------------------

def test_apply_inserts_matching_board_only(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="w")
        targets = kbn.normalize_default_notify_targets([
            _forge_target(board="iron-rod"),
            _forge_target(board="other-board", chat_id="other-chat"),
        ])
        inserted = kbn.apply_default_notify_targets(
            conn, task_id=tid, board="iron-rod", targets=targets)
        assert inserted == 1
        subs = _subs(conn, tid)
        assert len(subs) == 1
        assert subs[0]["chat_id"] == "forge-chat"
        assert subs[0]["notifier_profile"] == "forge"
        assert subs[0]["delivery_mode"] == "notify+wake"
    finally:
        conn.close()


def test_apply_is_idempotent_on_same_identity(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="w")
        targets = kbn.normalize_default_notify_targets([_forge_target()])
        assert kbn.apply_default_notify_targets(
            conn, task_id=tid, board="iron-rod", targets=targets) == 1
        # Second application on the same (task, platform, chat, thread) inserts nothing.
        assert kbn.apply_default_notify_targets(
            conn, task_id=tid, board="iron-rod", targets=targets) == 0
        assert len(_subs(conn, tid)) == 1
    finally:
        conn.close()


def test_apply_cursor_caught_up_no_replay(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="w")
        targets = kbn.normalize_default_notify_targets([_forge_target()])
        kbn.apply_default_notify_targets(
            conn, task_id=tid, board="iron-rod", targets=targets)
        sub = _subs(conn, tid)[0]
        # last_event_id must equal the task's current max event id (the ``created``
        # event), so the notifier never replays history as an alert.
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS c FROM task_events WHERE task_id = ?",
            (tid,),
        ).fetchone()
        assert sub["last_event_id"] == int(row["c"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# create_task integration — the common path (tool + CLI + dashboard)
# ---------------------------------------------------------------------------

def test_create_without_config_adds_no_extra_sub(kanban_home):
    """Safety: no configured target -> upstream behaviour, zero extra rows."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="no config", assignee="w")
        assert _subs(conn, tid) == []
    finally:
        conn.close()


def test_create_with_target_on_matching_board(kanban_home):
    """Red: creation without any session channel + a configured iron-rod target
    produces the exact Forge/Telegram/DM/notify+wake subscription."""
    _write_config(kanban_home, [_forge_target()])
    kb.create_board("iron-rod")
    conn = kbc.connect(board="iron-rod")
    try:
        tid = kb.create_task(conn, title="forge sub", assignee="w", board="iron-rod")
        subs = _subs(conn, tid)
        assert len(subs) == 1
        s = subs[0]
        assert s["platform"] == "telegram"
        assert s["chat_id"] == "forge-chat"
        assert s["thread_id"] == "forge-thread"
        assert s["chat_type"] == "dm"
        assert s["notifier_profile"] == "forge"
        assert s["delivery_mode"] == "notify+wake"
    finally:
        conn.close()


def test_create_with_target_on_other_board_adds_nothing(kanban_home):
    """A target scoped to iron-rod must not leak onto another board."""
    _write_config(kanban_home, [_forge_target(board="iron-rod")])
    kb.create_board("other-board")
    conn = kbc.connect(board="other-board")
    try:
        tid = kb.create_task(conn, title="other board", assignee="w", board="other-board")
        assert _subs(conn, tid) == []
    finally:
        conn.close()


def test_create_keeps_creator_sub_and_adds_default(kanban_home):
    """Nominal: a distinct creator route is preserved alongside the default target."""
    _write_config(kanban_home, [_forge_target()])
    kb.create_board("iron-rod")
    conn = kbc.connect(board="iron-rod")
    try:
        tid = kb.create_task(conn, title="both", assignee="w", board="iron-rod")
        # Simulate the creator's own auto-subscription on a distinct route.
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="creator-chat",
            notifier_profile="creator", delivery_mode="notify+wake",
        )
        subs = _subs(conn, tid)
        assert len(subs) == 2
        chats = {s["chat_id"] for s in subs}
        assert chats == {"forge-chat", "creator-chat"}
    finally:
        conn.close()


def test_create_identical_route_not_double_inserted(kanban_home):
    """Nominal dedup: a default target identical to an already-present route
    (creator/parent) is treated as satisfied and not double-inserted."""
    _write_config(kanban_home, [_forge_target()])
    kb.create_board("iron-rod")
    conn = kbc.connect(board="iron-rod")
    try:
        tid = kb.create_task(conn, title="dedup", assignee="w", board="iron-rod")
        # Pre-seed the exact same route as the default target (as a creator would).
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="forge-chat",
            thread_id="forge-thread", notifier_profile="forge",
            delivery_mode="notify+wake",
        )
        # Re-apply the default target: INSERT OR IGNORE must not add a second row.
        targets = kbn.normalize_default_notify_targets([_forge_target()])
        assert kbn.apply_default_notify_targets(
            conn, task_id=tid, board="iron-rod", targets=targets) == 0
        assert len(_subs(conn, tid)) == 1
    finally:
        conn.close()


def test_create_invalid_config_fails_closed_no_orphan(kanban_home):
    """A non-empty invalid config aborts creation before commit: no orphan task,
    no partial subscription, and the error carries no private routing data."""
    _write_config(kanban_home, [{
        "board": "iron-rod", "platform": "telegram", "chat_id": "SECRET-CHAT",
        "delivery_mode": "bogus",
    }])
    kb.create_board("iron-rod")
    conn = kbc.connect(board="iron-rod")
    try:
        before = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        with pytest.raises(ValueError) as exc:
            kb.create_task(conn, title="invalid", assignee="w", board="iron-rod")
        assert "SECRET-CHAT" not in str(exc.value)
        after = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        assert after == before
    finally:
        conn.close()


def test_create_respects_auto_subscribe_gate(kanban_home):
    """When auto_subscribe_on_create=false, default targets are not applied."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n"
        "  auto_subscribe_on_create: false\n"
        "  default_notify_targets: " + json.dumps([_forge_target()]) + "\n"
    )
    kb.create_board("iron-rod")
    conn = kbc.connect(board="iron-rod")
    try:
        tid = kb.create_task(conn, title="gated", assignee="w", board="iron-rod")
        assert _subs(conn, tid) == []
    finally:
        conn.close()


def test_create_uses_opened_db_board_when_slug_omitted(kanban_home, monkeypatch):
    """HERMES_KANBAN_DB wins over an unrelated ambient board: matching and
    insertion are both scoped to the DB that was actually opened."""
    kb.create_board("iron-rod")
    kb.create_board("other-board")
    _write_config(kanban_home, [
        _forge_target(board=" Iron-Rod ", chat_id="iron-chat"),
        _forge_target(board="other-board", chat_id="other-chat"),
    ])
    iron_db = kb.board_dir("iron-rod") / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(iron_db))

    with kb.scoped_current_board("other-board"), kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="db-pinned", assignee="w")
        subs = _subs(conn, tid)
        assert [(s["platform"], s["chat_id"], s["thread_id"])
                for s in subs] == [("telegram", "iron-chat", "forge-thread")]

    with kbc.connect_closing(db_path=kb.board_dir("other-board") / "kanban.db") as conn:
        assert kb.get_task(conn, tid) is None
        assert kbn.list_notify_subs(conn) == []


def test_explicit_board_mismatch_without_targets_preserves_creation(
    kanban_home, monkeypatch
):
    """An empty default-target config preserves task creation even when an env
    DB override is authoritative over a divergent explicit board."""
    kb.create_board("iron-rod")
    kb.create_board("other-board")
    iron_db = kb.board_dir("iron-rod") / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(iron_db))

    with kbc.connect_closing(board="other-board") as conn:
        task_id = kb.create_task(
            conn, title="preserve upstream creation", assignee="w", board="other-board"
        )
        assert kb.get_task(conn, task_id) is not None
        assert _subs(conn, task_id) == []


def test_create_uses_opened_db_board_when_explicit_differs(
    kanban_home, monkeypatch
):
    """A divergent explicit board cannot select another board's target when an
    env override pins the connection to a known board."""
    kb.create_board("iron-rod")
    kb.create_board("other-board")
    _write_config(kanban_home, [
        _forge_target(board="iron-rod", chat_id="iron-chat"),
        _forge_target(board="other-board", chat_id="other-route"),
    ])
    iron_db = kb.board_dir("iron-rod") / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(iron_db))

    with kbc.connect_closing(board="other-board") as conn:
        task_id = kb.create_task(
            conn, title="must not cross boards", assignee="w", board="other-board"
        )
        assert kb.get_task(conn, task_id) is not None
        assert [(sub["platform"], sub["chat_id"]) for sub in _subs(conn, task_id)] == [
            ("telegram", "iron-chat")
        ]

    with kbc.connect_closing(
        db_path=kb.board_dir("other-board") / "kanban.db"
    ) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs").fetchone()[0] == 0


def test_create_unknown_platform_is_atomic(kanban_home):
    secret_platform = "SECRET-UNRESOLVABLE-PLATFORM"
    _write_config(kanban_home, [
        _forge_target(board="default", platform=secret_platform)
    ])
    conn = kbc.connect()
    try:
        with pytest.raises(ValueError, match="unsupported platform") as exc:
            kb.create_task(conn, title="must not exist", assignee="w")
        assert secret_platform not in str(exc.value)
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs").fetchone()[0] == 0
    finally:
        conn.close()


def test_create_uses_opened_board_metadata_and_parent_tenant(
    kanban_home, monkeypatch, tmp_path
):
    target_repo = tmp_path / "target-repo"
    ambient_repo = tmp_path / "ambient-repo"
    target_repo.mkdir()
    ambient_repo.mkdir()
    with pdb.connect_closing() as conn:
        target_project = pdb.create_project(
            conn, name="Target", primary_path=str(target_repo)
        )
        ambient_project = pdb.create_project(
            conn, name="Ambient", primary_path=str(ambient_repo)
        )
    kb.create_board("target", project_id=target_project)
    kb.create_board("ambient", project_id=ambient_project)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.board_dir("target") / "kanban.db"))

    with kb.scoped_current_board("ambient"), kbc.connect_closing() as conn:
        parent = kb.create_task(
            conn, title="target parent", tenant="target-tenant", workspace_kind="scratch"
        )
        child_id = kb.create_task(conn, title="target child", parents=[parent])
        child = kb.get_task(conn, child_id)

    assert child is not None
    assert child.tenant == "target-tenant"
    assert child.project_id == target_project
    assert child.workspace_kind == "worktree"
    assert child.workspace_path is not None
    assert child.workspace_path.startswith(str(target_repo))
    assert ambient_project not in (child.project_id, child.workspace_path)


def test_create_uses_opened_board_default_workdir_when_ambient_differs(
    kanban_home, monkeypatch, tmp_path
):
    target_workdir = tmp_path / "target-workdir"
    ambient_workdir = tmp_path / "ambient-workdir"
    target_workdir.mkdir()
    ambient_workdir.mkdir()
    kb.create_board("target", default_workdir=str(target_workdir))
    kb.create_board("ambient", default_workdir=str(ambient_workdir))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.board_dir("target") / "kanban.db"))

    with kb.scoped_current_board("ambient"), kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="target directory", workspace_kind="dir"
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    workspace_path = task.workspace_path
    assert workspace_path == str(target_workdir)
    assert str(ambient_workdir) not in (workspace_path or "")


def test_create_rejects_api_server_target_atomically(kanban_home):
    private_route = "PRIVATE-API-SESSION"
    _write_config(kanban_home, [
        _forge_target(
            board="default", platform="api_server", chat_id=private_route
        )
    ])
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match="unsupported platform") as exc:
            kb.create_task(conn, title="must not exist", assignee="w")
        assert private_route not in str(exc.value)
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs").fetchone()[0] == 0


def test_concurrent_create_with_duplicate_targets_is_exactly_once(kanban_home):
    duplicate = _forge_target(board="default", chat_id="one-route")
    _write_config(kanban_home, [duplicate, dict(duplicate)])
    worker_count = 2
    barrier = threading.Barrier(worker_count)
    created: list[str] = []
    failures: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            with kbc.connect_closing() as conn:
                barrier.wait(timeout=5)
                created.append(kb.create_task(conn, title=f"concurrent-{index}", assignee="w"))
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    assert len(created) == worker_count
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == worker_count
        rows = conn.execute(
            "SELECT task_id, COUNT(*) AS n FROM kanban_notify_subs GROUP BY task_id"
        ).fetchall()
        assert {row["task_id"]: row["n"] for row in rows} == {
            task_id: 1 for task_id in created
        }


def test_real_cli_create_applies_default_target(kanban_home):
    from hermes_cli import kanban as kc

    _write_config(kanban_home, [_forge_target(board="default")])
    output = kc.run_slash("create 'cli configured target' --assignee worker")
    task_id = output.split()[1]
    with kbc.connect_closing() as conn:
        assert len(_subs(conn, task_id)) == 1
