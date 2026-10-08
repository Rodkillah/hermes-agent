"""Cooperative Office scope refuses before transport; no real mandate or RPC."""
import json

import pytest

from tools import mcp_tool_handlers as handlers


@pytest.mark.parametrize("tool,args", [
    ("create_drive_file", {"file_name": "synthetic.docx"}),
    ("update_drive_file", {"file_id": "synthetic", "content": "replacement"}),
])
def test_office_without_bound_mandate_never_acquires_transport(monkeypatch, tool, args):
    monkeypatch.setattr(handlers, "_ruth_office_identity", lambda server: True)
    monkeypatch.delenv("IRONROD_RUTH_GMAIL_OFFICE_MANDATE_JSON", raising=False)
    acquired = []
    def acquire(*a):
        acquired.append(True)
        return None, "synthetic transport"
    monkeypatch.setattr(handlers, "_acquire_call_server", acquire)
    result = handlers._make_tool_handler("gmail-pilot", tool, 1)(args)
    assert not acquired
    assert "RUTH_OFFICE_SCOPE_BLOCKED" in json.loads(result)["error"]


def test_native_trust_refusal_survives_office_raccord(monkeypatch):
    monkeypatch.setattr(handlers, "_ruth_office_identity", lambda server: False)
    monkeypatch.setattr(handlers, "_trust_gate_check", lambda *a: '{"error":"synthetic denied"}')
    acquired = []
    monkeypatch.setattr(handlers, "_acquire_call_server", lambda *a: acquired.append(True))
    result = handlers._make_tool_handler("synthetic", "write", 1, native_images=True)({})
    assert json.loads(result)["error"] == "synthetic denied"
    assert not acquired
