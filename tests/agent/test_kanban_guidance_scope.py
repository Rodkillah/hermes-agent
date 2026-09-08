"""Regression tests for task-scoped Kanban lifecycle guidance."""

from agent.agent_init import _resolve_kanban_worker_guidance
from agent.prompt_builder import KANBAN_GUIDANCE


def test_orchestrator_chat_does_not_receive_worker_guidance(monkeypatch):
    """Kanban tools in a normal profile chat do not make it a task worker."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    assert _resolve_kanban_worker_guidance({"kanban_show", "kanban_create"}) == ""


def test_dispatcher_worker_receives_worker_guidance(monkeypatch):
    """A real dispatcher worker keeps the task lifecycle prompt."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_direct_scope")

    assert (
        _resolve_kanban_worker_guidance({"kanban_show", "kanban_complete"})
        == KANBAN_GUIDANCE
    )


def test_delegated_child_without_kanban_tools_gets_no_guidance(monkeypatch):
    """An inherited marker cannot inject guidance after tools are stripped."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")

    assert _resolve_kanban_worker_guidance({"terminal", "read_file"}) == ""
