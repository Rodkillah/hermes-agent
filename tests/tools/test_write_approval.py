"""Tests for the memory/skill write-approval gate (tools/write_approval.py)
and the shared slash-command handlers (hermes_cli/write_approval_commands.py).

Covers the boolean write_approval gate (off by default = write freely; on =
require approval) for both subsystems, the foreground-vs-background staging
split, pending store CRUD, and the list/approve/reject/diff/approval
subcommand dispatch.
"""

import json
import os
import tempfile
import shutil
import threading

import pytest

@pytest.fixture
def hermes_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hermes_wa_test_")
    home = os.path.join(d, ".hermes")
    os.makedirs(home)
    monkeypatch.setenv("HERMES_HOME", home)
    yield home
    shutil.rmtree(d, ignore_errors=True)

def _set_approval(subsystem, enabled):
    import hermes_cli.config as cfg
    c = cfg.load_config()
    c.setdefault(subsystem, {})["write_approval"] = enabled
    cfg.save_config(c)

# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def test_list_pending_skips_non_dict_record(hermes_home):
    """A parseable-but-non-object pending file must be skipped, not crash the sort."""
    from tools import write_approval as wa
    wa.stage_write("memory", {"action": "add", "target": "user", "content": "ok"},
                   summary="ok", origin="foreground")
    pending_dir = wa._pending_path("memory", "").parent
    (pending_dir / "bad.json").write_text('"not a record"', encoding="utf-8")
    records = wa.list_pending("memory")
    assert len(records) == 1 and records[0]["payload"]["content"] == "ok"
    assert wa.get_pending("memory", "bad") is None


def test_skill_rework_gets_immutable_id_and_reject_is_archived(hermes_home):
    from tools import write_approval as wa

    first = wa.stage_write("skills", {"action": "edit", "name": "demo", "content": "v1"},
                           summary="first", origin="background_review")
    second = wa.stage_write("skills", {"action": "patch", "name": "demo", "content": "v2"},
                            summary="rework", origin="foreground")

    assert second["id"] != first["id"]
    assert second["revision"] == 2
    assert second["subject_key"] == "skills:demo:SKILL.md"
    assert wa.pending_count("skills") == 1
    assert wa.get_pending("skills", first["id"]) is None
    superseded = wa.get_hermes_home() / "pending" / "superseded" / "skills" / f"{first['id']}.json"
    archived_first = json.loads(superseded.read_text(encoding="utf-8"))
    assert archived_first["superseded_by"] == second["id"]
    current = wa.get_pending("skills", second["id"])
    assert current is not None
    assert current["payload"]["content"] == "v2"

    assert wa.reject_pending("skills", second["id"]) is True
    assert wa.get_pending("skills", second["id"]) is None
    rejected = wa.get_hermes_home() / "pending" / "rejected" / "skills" / f"{second['id']}.json"
    archived = json.loads(rejected.read_text(encoding="utf-8"))
    assert archived["subject_key"] == "skills:demo:SKILL.md"
    assert archived["rejected_at"] >= archived["updated_at"]


def test_preexisting_skill_duplicates_collapse_to_newest_id(hermes_home):
    from tools import write_approval as wa

    pending = wa._pending_path("skills", "").parent
    pending.mkdir(parents=True)
    for pending_id, created, content in (("older", 1, "v1"), ("newer", 2, "v2")):
        record = {
            "id": pending_id, "subsystem": "skills", "action": "edit",
            "summary": content, "origin": "background_review", "created_at": created,
            "payload": {"action": "edit", "name": "demo", "content": content},
        }
        (pending / f"{pending_id}.json").write_text(json.dumps(record), encoding="utf-8")

    staged = wa.stage_write("skills", {"action": "edit", "name": "demo", "content": "v3"},
                            summary="final", origin="foreground")

    assert staged["id"] not in {"older", "newer"}
    assert staged["revision"] == 2
    assert [record["id"] for record in wa.list_pending("skills")] == [staged["id"]]
    superseded = wa.get_hermes_home() / "pending" / "superseded" / "skills" / "older.json"
    archived = json.loads(superseded.read_text(encoding="utf-8"))
    assert archived["superseded_by"] == staged["id"]
    newer_archive = wa.get_hermes_home() / "pending" / "superseded" / "skills" / "newer.json"
    assert json.loads(newer_archive.read_text(encoding="utf-8"))["superseded_by"] == staged["id"]


def test_stale_skill_approval_cannot_apply_new_revision(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    skill_dir = wa.get_hermes_home() / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("original", encoding="utf-8")
    first = wa.stage_write("skills", {"action": "edit", "name": "demo", "content": "reviewed"},
                           summary="reviewed", origin="foreground")
    second = wa.stage_write("skills", {"action": "edit", "name": "demo", "content": "unreviewed"},
                            summary="unreviewed", origin="foreground")

    stale = handle_pending_subcommand(wa.SKILLS, ["approve", first["id"]])
    assert stale is not None
    assert "No pending skills write" in stale
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "original"
    current = wa.get_pending("skills", second["id"])
    assert current is not None
    assert current["payload"]["content"] == "unreviewed"


def test_stage_write_reports_persistence_failure_without_fake_pending_id(hermes_home, monkeypatch):
    from tools import write_approval as wa

    real_write = wa.atomic_json_write
    def fail_active(path, data):
        if path.parent == wa._pending_path("skills", "").parent:
            raise OSError("disk unavailable")
        return real_write(path, data)
    monkeypatch.setattr(wa, "atomic_json_write", fail_active)

    with pytest.raises(RuntimeError, match="Could not persist pending skills write"):
        wa.stage_write("skills", {"action": "edit", "name": "demo", "content": "v1"},
                       summary="must fail", origin="foreground")
    assert wa.pending_count("skills") == 0


def test_supersession_is_recoverable_on_both_sides_of_publication(hermes_home, monkeypatch):
    """A prepared archive is inert until publication; after publication it is a tombstone."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    first = wa.stage_write("skills", {"action": "edit", "name": "demo", "content": "v1"},
                           summary="first", origin="foreground")
    real_write = wa.atomic_json_write

    def fail_successor_publication(path, data):
        if path.parent == wa._pending_path("skills", "").parent and data.get("id") != first["id"]:
            raise OSError("injected publication failure")
        return real_write(path, data)

    monkeypatch.setattr(wa, "atomic_json_write", fail_successor_publication)
    with pytest.raises(RuntimeError, match="Could not persist pending skills write"):
        wa.stage_write("skills", {"action": "edit", "name": "demo", "content": "v2"},
                       summary="publication fails", origin="foreground")
    assert wa.get_pending("skills", first["id"]) is not None
    assert [record["id"] for record in wa.list_pending("skills")] == [first["id"]]

    monkeypatch.setattr(wa, "atomic_json_write", real_write)
    old_path = wa._pending_path("skills", first["id"])
    real_unlink = type(old_path).unlink

    def fail_predecessor_retirement(path, *args, **kwargs):
        if path == old_path:
            raise OSError("injected retirement failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(old_path), "unlink", fail_predecessor_retirement)
    with pytest.raises(RuntimeError, match="Could not persist pending skills write"):
        wa.stage_write("skills", {"action": "edit", "name": "demo", "content": "v3"},
                       summary="retirement fails", origin="foreground")

    archive = wa.get_hermes_home() / "pending" / "superseded" / "skills" / f"{first['id']}.json"
    successor_id = json.loads(archive.read_text(encoding="utf-8"))["superseded_by"]
    assert old_path.exists()
    assert wa.get_pending("skills", first["id"]) is None
    assert [record["id"] for record in wa.list_pending("skills")] == [successor_id]
    refused = handle_pending_subcommand(wa.SKILLS, ["approve", first["id"]])
    assert refused is not None and "No pending skills write" in refused
    assert wa.get_pending("skills", successor_id) is not None

    unresolved = handle_pending_subcommand(wa.SKILLS, ["approve", successor_id])
    assert unresolved is not None and "Approved 0" in unresolved
    assert "supersession cleanup is incomplete" in unresolved
    assert wa.get_pending("skills", successor_id) is not None

    monkeypatch.setattr(type(old_path), "unlink", real_unlink)
    third = wa.stage_write("skills", {"action": "edit", "name": "demo", "content": "v4"},
                           summary="third revision", origin="foreground")
    assert third["revision"] == 3
    assert wa.get_pending("skills", first["id"]) is None
    assert wa.get_pending("skills", successor_id) is None
    assert wa.reject_pending("skills", third["id"]) is True
    assert wa.list_pending("skills") == []
    assert not old_path.exists()
    assert wa.get_pending("skills", first["id"]) is None


def test_interrupted_tombstone_commit_recovers_before_later_stage(hermes_home, monkeypatch):
    """A later revision must not retire a successor before its predecessor is durable."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    skill_dir = wa.get_hermes_home() / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    skill = skill_dir / "SKILL.md"
    content = lambda version: f"---\nname: demo\ndescription: Offline fixture.\n---\n\n{version}\n"
    skill.write_text(content("v0"), encoding="utf-8")
    first = wa.stage_write("skills", {"action": "edit", "name": "demo", "content": content("v1")},
                           summary="v1", origin="foreground")
    real_write = wa.atomic_json_write

    def fail_commit(path, data):
        if data.get("supersession_state") == "committed":
            raise OSError("injected tombstone commit failure")
        return real_write(path, data)

    monkeypatch.setattr(wa, "atomic_json_write", fail_commit)
    with pytest.raises(RuntimeError, match="Could not persist pending skills write"):
        wa.stage_write("skills", {"action": "edit", "name": "demo", "content": content("v2")},
                       summary="v2", origin="foreground")
    archive = wa.get_hermes_home() / "pending" / "superseded" / "skills" / f"{first['id']}.json"
    second_id = json.loads(archive.read_text(encoding="utf-8"))["superseded_by"]
    assert wa.get_pending("skills", first["id"]) is None
    assert wa.get_pending("skills", second_id) is not None

    monkeypatch.setattr(wa, "atomic_json_write", real_write)
    third = wa.stage_write("skills", {"action": "edit", "name": "demo", "content": content("v3")},
                           summary="v3", origin="foreground")
    assert [record["id"] for record in wa.list_pending("skills")] == [third["id"]]
    assert wa.get_pending("skills", first["id"]) is None
    assert wa.get_pending("skills", second_id) is None
    approved = handle_pending_subcommand(wa.SKILLS, ["approve", third["id"]])
    assert approved is not None and "Approved 1" in approved
    assert skill.read_text(encoding="utf-8") == content("v3")


def test_concurrent_approval_and_rework_preserve_new_pending(hermes_home, monkeypatch):
    from hermes_cli import write_approval_commands as commands
    from tools import write_approval as wa

    first = wa.stage_write("skills", {"action": "edit", "name": "demo", "content": "reviewed"},
                           summary="reviewed", origin="foreground")
    applying = threading.Event()
    release = threading.Event()
    results = {}

    def controlled_apply(subsystem, record, memory_store):
        applying.set()
        assert release.wait(timeout=5)
        return True, "", {}

    monkeypatch.setattr(commands, "_apply_one", controlled_apply)
    approve_thread = threading.Thread(target=lambda: results.setdefault(
        "approval", commands.handle_pending_subcommand(wa.SKILLS, ["approve", first["id"]])))
    approve_thread.start()
    assert applying.wait(timeout=5)

    stage_thread = threading.Thread(target=lambda: results.setdefault(
        "staged", wa.stage_write("skills", {"action": "edit", "name": "demo", "content": "rework"},
                                 summary="rework", origin="foreground")))
    stage_thread.start()
    stage_thread.join(timeout=0.1)
    assert stage_thread.is_alive()  # stage waits for the atomic review transition
    release.set()
    approve_thread.join(timeout=5)
    stage_thread.join(timeout=5)

    assert "Approved 1" in results["approval"]
    staged = results["staged"]
    assert staged["id"] != first["id"]
    current = wa.get_pending("skills", staged["id"])
    assert current is not None
    assert current["payload"]["content"] == "rework"
    assert wa.pending_count("skills") == 1


def test_full_rewrite_patch_diff_uses_content(hermes_home, monkeypatch):
    from tools import write_approval as wa

    skill_dir = wa.get_hermes_home() / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(wa, "_find_skill_path", lambda name: skill_dir)
    record = {"id": "p1", "payload": {
        "action": "patch", "name": "demo", "content": "new\n",
        "old_string": "", "new_string": "",
    }}

    diff = wa.skill_pending_diff(record)
    assert "-old" in diff
    assert "+new" in diff
    assert "(no textual change)" not in diff


def test_normalize_enabled_coerces_values():
    from tools import write_approval as wa
    # Real bools pass through.
    assert wa._normalize_enabled(True) is True
    assert wa._normalize_enabled(False) is False
    # Truthy strings → True (incl. legacy 'approve').
    assert wa._normalize_enabled("on") is True
    assert wa._normalize_enabled("approve") is True
    assert wa._normalize_enabled("true") is True
    # Everything else → False (gate off is the safe default).
    assert wa._normalize_enabled("off") is False
    assert wa._normalize_enabled("garbage") is False
    assert wa._normalize_enabled(None) is False

# ---------------------------------------------------------------------------
# Memory gate
# ---------------------------------------------------------------------------

def test_memory_gate_off_allows_write(hermes_home):
    # Default (gate off) → write straight through, no staging.
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "user", "save me", store=store))
    assert r["success"] is True
    assert r["entry_count"] == 1
    assert wa.pending_count("memory") == 0

def test_cli_memory_approve_without_live_agent_uses_fresh_store(hermes_home, capsys):
    """#46783: ``/memory approve`` from a context with no live agent (e.g. the
    Desktop GUI) passed ``memory_store=None`` into the shared handler, which
    returned "memory store unavailable" and applied nothing. The CLI handler must
    fall back to a freshly loaded on-disk store, like the gateway path does."""
    import json
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    from hermes_cli.cli_commands_mixin import CLICommandsMixin

    _set_approval("memory", True)
    staging = MemoryStore(); staging.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "remember the launch date", store=staging))
    assert r.get("pending_id"), r
    assert wa.pending_count("memory") == 1

    # Bare CLI handler with no live agent → store resolves to None pre-fix.
    handler = CLICommandsMixin.__new__(CLICommandsMixin)
    handler.agent = None
    handler._handle_memory_command("/memory approve all")

    out = capsys.readouterr().out
    assert "memory store unavailable" not in out, out
    assert "Approved 1" in out, out
    assert wa.pending_count("memory") == 0
    # The approved write landed in a freshly loaded on-disk store (MEMORY.md).
    reloaded = MemoryStore(); reloaded.load_from_disk()
    assert any("remember the launch date" in e for e in reloaded.memory_entries)

def test_load_on_disk_store_honors_configured_limits_and_permissions(hermes_home, monkeypatch):
    """Fresh approval stores must match the live agent's limits and target gates."""
    from tools.memory_tool import MemoryStore, load_on_disk_store

    # Config override path: helper picks up configured limits and store flags.
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "memory": {
                "memory_char_limit": 999,
                "user_char_limit": 444,
                "memory_enabled": False,
                "user_profile_enabled": True,
            }
        },
    )
    store = load_on_disk_store()
    assert store.memory_char_limit == 999
    assert store.user_char_limit == 444
    assert store.memory_enabled is False
    assert store.user_profile_enabled is True

    # Failure path: config raises → defaults, never blows up.
    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr("hermes_cli.config.load_config", _boom)
    fallback = load_on_disk_store()
    defaults = MemoryStore()
    assert fallback.memory_char_limit == defaults.memory_char_limit
    assert fallback.user_char_limit == defaults.user_char_limit
    assert fallback.memory_enabled is True
    assert fallback.user_profile_enabled is True

# ---------------------------------------------------------------------------
# Shared command handler
# ---------------------------------------------------------------------------

def test_handle_approve_all(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa
    store = MemoryStore(); store.load_from_disk()
    wa.stage_write("memory", {"action": "add", "target": "user", "content": "a"},
                   summary="a", origin="foreground")
    wa.stage_write("memory", {"action": "add", "target": "user", "content": "b"},
                   summary="b", origin="foreground")
    out = handle_pending_subcommand(wa.MEMORY, ["approve", "all"], memory_store=store)
    assert "Approved 2" in out
    assert wa.pending_count("memory") == 0
    assert len(store.user_entries) == 2

def test_handle_approve_surfaces_overwritten_entry(hermes_home):
    """#117952: on the /memory approve surface a partial-entry replace must show the
    approver the FULL entry it overwrote — the store's replaced_entries field used to be
    dropped by _apply_one, so the incident path stayed silent."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa
    store = MemoryStore(); store.load_from_disk()
    entry = "RULE A: gate merges. RULE B: ci per HEAD. RULE C: never squash."
    store.add("memory", entry)
    wa.stage_write("memory", {"action": "batch", "target": "memory", "operations": [
        {"action": "replace", "old_text": "RULE B: ci per HEAD.", "content": "RULE B: CI is per-head.",
         "matched_entry": entry}]},
        summary="batch", origin="background_review")
    out = handle_pending_subcommand(wa.MEMORY, ["approve", "all"], memory_store=store)
    assert "Approved 1" in out and entry in out
    assert store.memory_entries == ["RULE B: CI is per-head."]


def test_handle_approve_lists_a_patch_as_the_entry_before_the_edit(hermes_home):
    """An approved patch keeps the rest of its entry; the approve output called it an
    overwrite and told the user to re-add text they had deliberately changed."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import MemoryStore
    from tools import write_approval as wa
    store = MemoryStore(); store.load_from_disk()
    entry = "RULE A: gate merges. RULE B: ci per HEAD. RULE C: never squash."
    store.add("memory", entry)
    wa.stage_write("memory", {"action": "batch", "target": "memory", "operations": [
        {"action": "patch", "old_text": "ci per HEAD", "content": "CI is per-head", "matched_entry": entry}]},
        summary="batch", origin="background_review")
    out = handle_pending_subcommand(wa.MEMORY, ["approve", "all"], memory_store=store)
    assert "Patched entry (as it was before the edit):" in out and entry in out
    assert "Overwrote" not in out
    assert store.memory_entries == ["RULE A: gate merges. RULE B: CI is per-head. RULE C: never squash."]


_KEPT = "Repo lives in ~/src/app; tests via make test"
_REVIEWED = "Staging DB: pg-staging-2 (old cluster, retiring)"


def _review_stages_remove(shape):
    """Seed memory, then stage a remove the way the unattended background review does."""
    from tools.memory_tool import MemoryStore, memory_tool
    from tools.skill_provenance import (reset_current_write_origin, reset_review_attended,
                                        set_current_write_origin, set_review_attended)
    store = MemoryStore(); store.load_from_disk()
    for entry in (_KEPT, _REVIEWED):
        assert store.add("memory", entry)["success"]
    op = {"action": "remove", "old_text": "Staging DB"}
    kwargs = op if shape == "single" else {"operations": [op, {"action": "add", "content": "Deploys via make ship"}]}
    origin, attended = set_current_write_origin("background_review"), set_review_attended(False)
    try:
        staged = json.loads(memory_tool(target="memory", store=store, **kwargs))
    finally:
        reset_review_attended(attended)
        reset_current_write_origin(origin)
    assert staged["staged"] is True, staged
    return store, staged["pending_id"]


@pytest.mark.parametrize("shape", ["single", "batch"])
def test_approve_refuses_staged_remove_whose_entry_changed(hermes_home, shape):
    """Approval re-ran the staged old_text search against the file as it is THEN, so it
    deleted the newer entry the live agent had written in place, which the approver never saw."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import load_on_disk_store, memory_tool
    from tools import write_approval as wa
    store, pid = _review_stages_remove(shape)
    newer = "Staging DB: pg-staging-3 (migrated 2026-09-20, creds in vault 'stg')"
    assert json.loads(memory_tool(action="patch", old_text=_REVIEWED, content=newer, store=store))["success"]

    out = handle_pending_subcommand(wa.MEMORY, ["approve", pid], memory_store=load_on_disk_store())

    assert load_on_disk_store().memory_entries == [_KEPT, newer], out
    assert "changed since it was staged" in out
    assert wa.get_pending(wa.MEMORY, pid) is not None
    # The pending list shows the whole entry the write targets, not just its search string.
    assert _REVIEWED in handle_pending_subcommand(wa.MEMORY, ["pending"])


@pytest.mark.parametrize("shape", ["single", "batch"])
def test_approve_names_the_entry_a_remove_deleted(hermes_home, shape):
    """Approve listed what a replace overwrote but was silent about what a remove deleted."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import load_on_disk_store
    from tools import write_approval as wa
    _store, pid = _review_stages_remove(shape)
    out = handle_pending_subcommand(wa.MEMORY, ["approve", pid], memory_store=load_on_disk_store())
    assert _REVIEWED not in load_on_disk_store().memory_entries, out
    assert _REVIEWED in out


def test_approve_refuses_unpinned_legacy_remove(hermes_home):
    """A record staged before removes were pinned to their full entry has no verifiable target,
    so approve refuses it (keeping the record) instead of replaying its old_text search."""
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_tool import load_on_disk_store
    from tools import write_approval as wa
    _store, pid = _review_stages_remove("single")
    path = wa._pending_path(wa.MEMORY, pid)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"].pop("matched_entry", None)
    path.write_text(json.dumps(record), encoding="utf-8")
    assert "unpinned legacy target" in handle_pending_subcommand(wa.MEMORY, ["pending"])

    out = handle_pending_subcommand(wa.MEMORY, ["approve", pid], memory_store=load_on_disk_store())

    assert "Approved 0" in out and "predates entry pinning" in out, out
    assert _REVIEWED in load_on_disk_store().memory_entries
    assert wa.get_pending(wa.MEMORY, pid) is not None

def test_handle_approval_on(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    captured = {}
    out = handle_pending_subcommand(
        wa.MEMORY, ["approval", "on"],
        set_mode_fn=lambda enabled: captured.update(enabled=enabled),
    )
    assert captured["enabled"] is True
    assert "on" in out

def test_handle_approval_off(hermes_home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    captured = {}
    out = handle_pending_subcommand(
        wa.SKILLS, ["approval", "off"],
        set_mode_fn=lambda enabled: captured.update(enabled=enabled),
    )
    assert captured["enabled"] is False
    assert "off" in out

# ---------------------------------------------------------------------------
# Inline (interactive CLI) approval path — regression for the bug where the
# per-thread approval callback was never passed to prompt_dangerous_approval,
# so every gated foreground memory write was silently denied.
# ---------------------------------------------------------------------------

@pytest.fixture
def approval_callback_cleanup():
    yield
    from tools.terminal_tool import set_approval_callback
    set_approval_callback(None)

def test_memory_inline_approve_writes(hermes_home, approval_callback_cleanup):
    from tools.memory_tool import memory_tool, MemoryStore
    from tools.terminal_tool import set_approval_callback
    from tools import write_approval as wa
    _set_approval("memory", True)

    calls = []
    def approve_cb(command, description, **kw):
        calls.append((command, description))
        return "once"
    set_approval_callback(approve_cb)

    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "approved fact", store=store))
    assert r["success"] is True
    assert r.get("staged") is None  # real write, not staged
    assert store.memory_entries == ["approved fact"]
    assert wa.pending_count("memory") == 0
    # The registered callback must actually be invoked (not the input() path).
    assert len(calls) == 1
    assert "approved fact" in calls[0][0]

def test_memory_inline_deny_blocks(hermes_home, approval_callback_cleanup):
    from tools.memory_tool import memory_tool, MemoryStore
    from tools.terminal_tool import set_approval_callback
    from tools import write_approval as wa
    _set_approval("memory", True)
    set_approval_callback(lambda command, description, **kw: "deny")

    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "denied fact", store=store))
    assert r["success"] is False
    assert "denied" in r["error"].lower()
    assert store.memory_entries == []
    assert wa.pending_count("memory") == 0  # denied, not staged


@pytest.mark.parametrize("context", ["single_query", "cron", "webhook"])
def test_headless_memory_write_stages_without_inline_prompt(
    hermes_home, approval_callback_cleanup, monkeypatch, context
):
    """`hermes chat -q`, cron and unattended platforms can register the CLI callback but have no human to answer
    it: the write stages at once instead of waiting the approval timeout."""
    from tools.memory_tool import memory_tool, MemoryStore
    from tools.terminal_tool import set_approval_callback
    from tools import write_approval as wa

    _set_approval("memory", True)
    monkeypatch.setenv(*{"single_query": ("HERMES_SINGLE_QUERY_SESSION", "1"), "cron": ("HERMES_CRON_SESSION", "1"),
                         "webhook": ("HERMES_SESSION_PLATFORM", "webhook")}[context])

    calls = []

    def approve_if_prompted(*args, **kwargs):
        calls.append((args, kwargs))
        return "once"

    set_approval_callback(approve_if_prompted)
    store = MemoryStore()
    store.load_from_disk()

    r = json.loads(memory_tool("add", "memory", "headless fact", store=store))

    assert calls == []
    assert r["success"] is True
    assert r["staged"] is True
    assert store.memory_entries == []
    assert wa.pending_count("memory") == 1


def test_memory_invalid_params_rejected_before_staging(hermes_home):
    # Param validation must run BEFORE the gate so a broken write is rejected
    # immediately instead of staged and failing at approve time.
    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa
    _set_approval("memory", True)
    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", None, store=store))
    assert r["success"] is False
    assert wa.pending_count("memory") == 0


# ---------------------------------------------------------------------------
# Staged-write review hint is surface-aware (#98330)
# ---------------------------------------------------------------------------

def _stage_one_memory_write():
    from tools.memory_tool import memory_tool, MemoryStore
    store = MemoryStore(); store.load_from_disk()
    r = json.loads(memory_tool("add", "memory", "surface hint fact", store=store))
    assert r.get("staged") is True, r
    return r


def test_staged_hint_names_the_review_command_on_slash_surfaces(hermes_home):
    from tools import write_approval as wa
    _set_approval("memory", True)
    r = _stage_one_memory_write()
    # No headless markers bound (plain foreground turn) → the slash hint stands.
    assert "/memory pending" in r["message"], r["message"]
    assert wa.pending_count("memory") == 1


def test_staged_hint_names_the_pending_dir_on_headless_surfaces(hermes_home, monkeypatch):
    from tools import write_approval as wa
    _set_approval("memory", True)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    r = _stage_one_memory_write()
    assert "review with /memory pending" not in r["message"], r["message"]
    assert "pending records live in" in r["message"]
    assert str(wa._pending_path(wa.MEMORY, "").parent) in r["message"]
    monkeypatch.delenv("HERMES_CRON_SESSION")

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-7")
    r = _stage_one_memory_write()
    assert "review with /memory pending" not in r["message"], r["message"]
    monkeypatch.delenv("HERMES_KANBAN_TASK")

    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "api_server")
    r = _stage_one_memory_write()
    assert "review with /memory pending" not in r["message"], r["message"]
    assert "pending records live in" in r["message"]
    monkeypatch.delenv("HERMES_SESSION_PLATFORM")
    assert wa.pending_count("memory") == 3


def test_staged_hint_keeps_command_for_chat_gateway_platform(hermes_home, monkeypatch):
    _set_approval("memory", True)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    r = _stage_one_memory_write()
    assert "/memory pending" in r["message"], r["message"]
