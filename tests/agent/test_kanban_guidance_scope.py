"""Task-scoped lifecycle guidance through the actual agent tool-loading path."""
from types import SimpleNamespace

import pytest

from agent.agent_init import _load_tools
from agent.prompt_builder import KANBAN_GUIDANCE


@pytest.fixture
def load_guidance(monkeypatch):
    monkeypatch.setattr("hermes_cli.left_core_migration.recover_at_startup", lambda **kw: None)
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr("toolsets.agent_tool_drops", lambda agent: set())
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)

    def load(names):
        tools = [{"function": {"name": name}} for name in names]
        monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kw: tools)
        agent = SimpleNamespace(quiet_mode=True, platform="cli")
        _load_tools(agent, None, None)
        return agent._kanban_worker_guidance

    return load


def test_orchestrator_chat_does_not_receive_worker_guidance(monkeypatch, load_guidance):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert load_guidance({"kanban_show", "kanban_create"}) == ""


def test_dispatcher_worker_receives_worker_guidance(monkeypatch, load_guidance):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_direct_scope")
    assert load_guidance({"kanban_show", "kanban_complete"}) == KANBAN_GUIDANCE


def test_delegated_child_without_kanban_tools_gets_no_guidance(monkeypatch, load_guidance):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    assert load_guidance({"terminal", "read_file"}) == ""


def test_delegated_child_with_tools_does_not_own_parent_task(monkeypatch, load_guidance):
    from agent.delegation_context import delegated_child_context

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    with delegated_child_context():
        assert load_guidance({"kanban_show", "kanban_complete"}) == ""
