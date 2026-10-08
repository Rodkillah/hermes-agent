"""Profile creation policy applies even when a stale schema dispatches a handler."""
import json

import pytest

from tools import kanban_tools as kt


@pytest.mark.parametrize("unreadable", [False, True])
@pytest.mark.parametrize("tool,args", [
    ("create", {"title": "proposal", "assignee": "expert"}),
    ("link", {"parent_id": "t_parent", "child_id": "t_child"}),
])
def test_creation_refused_before_board_access(monkeypatch, unreadable, tool, args):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: True)
    def config():
        if unreadable:
            raise OSError("synthetic unreadable configuration")
        return {"kanban": {"can_create": False}}
    monkeypatch.setattr(kt, "load_config", config)
    connections = []
    def connect(*a, **kw):
        connections.append(True)
        raise RuntimeError("synthetic board boundary reached")
    monkeypatch.setattr(kt, "_connect", connect)
    response = getattr(kt, "_handle_" + tool)(args)
    assert not connections
    payload = json.loads(response)
    assert "can_create" in payload["error"]
