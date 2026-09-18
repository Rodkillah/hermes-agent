"""Behavioral tests for the Iron Rod A2A hardening contract."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request

import pytest

from plugins.platforms.a2a import protocol, security, tools


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _post(url: str, body: dict):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode())


def _body(method: str, text: str = "hello") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": method,
        "params": {
            "message": protocol.text_message(protocol.ROLE_USER, text),
        },
    }


def test_strict_peer_requires_scope_aware_token_env_and_blocks_direct_url(monkeypatch):
    monkeypatch.setattr(
        tools,
        "_load_config",
        lambda: {
            "a2a": {"iron_rod_mode": True, "allow_direct_urls": False},
            "a2a_agents": {
                "peer": {
                    "url": "http://peer.example",
                    "auth": {"type": "bearer", "token_env": "PEER_TOKEN"},
                },
                "literal": {
                    "url": "http://literal.example",
                    "auth": {"type": "bearer", "token": "literal-config-value"},
                },
            },
        },
    )
    monkeypatch.setenv("PEER_TOKEN", "process-value")

    from agent import secret_scope

    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope({"PEER_TOKEN": "scoped-value"})
    try:
        peer = tools._resolve_peer("peer")
        assert peer["auth"] == {"type": "bearer", "token": "scoped-value"}
        assert tools._resolve_peer("http://peer.example") is None
        assert tools._resolve_peer("literal") is None
    finally:
        secret_scope.reset_secret_scope(token)
        secret_scope.set_multiplex_active(False)


def test_audit_is_metadata_only_and_file_is_owner_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    security.audit(
        "inbound",
        "peer-x",
        "task-1",
        "sensitive-content-marker",
        context_id="ctx-1",
        method="SendMessage",
        decision="accepted",
        state=protocol.STATE_WORKING,
        request_bytes=42,
    )
    audit_file = tmp_path / "a2a_audit.jsonl"
    record = json.loads(audit_file.read_text().strip())
    assert "summary" not in record
    assert "sensitive-content-marker" not in audit_file.read_text()
    assert record["context_id"] == "ctx-1"
    assert record["method"] == "SendMessage"
    assert stat.S_IMODE(audit_file.stat().st_mode) == 0o600


def test_persistence_can_be_disabled_and_enabled_files_are_private(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("A2A_PERSIST_CONVERSATIONS", "false")
    protocol.persist_message("ctx-off", "user", "private content", "task-1")
    assert not (tmp_path / "a2a_conversations").exists()

    monkeypatch.setenv("A2A_PERSIST_CONVERSATIONS", "true")
    protocol.persist_message("ctx-on", "user", "private content", "task-2")
    directory = tmp_path / "a2a_conversations"
    conversation = directory / "ctx-on.jsonl"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(conversation.stat().st_mode) == 0o600


def test_explicit_empty_advertised_toolsets_do_not_fall_back_to_registry(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.a2a.adapter import A2AAdapter
    from tools.registry import registry

    monkeypatch.setattr(registry, "get_registered_toolset_names", lambda: ["terminal"])
    monkeypatch.setattr(registry, "get_tool_names_for_toolset", lambda _: ["terminal"])
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"advertised_toolsets": []}))
    card = adapter._build_card()
    assert [skill["id"] for skill in card["skills"]] == ["general"]


def test_iron_rod_mode_defaults_to_empty_advertised_toolsets(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.a2a.adapter import A2AAdapter
    from tools.registry import registry

    monkeypatch.setattr(registry, "get_registered_toolset_names", lambda: ["terminal"])
    monkeypatch.setattr(registry, "get_tool_names_for_toolset", lambda _: ["terminal"])
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"iron_rod_mode": True}))
    card = adapter._build_card()
    assert [skill["id"] for skill in card["skills"]] == ["general"]


@pytest.mark.integration
def test_iron_rod_surface_and_transport_limits_are_enforced(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.a2a.adapter import A2AAdapter

    port = _free_port()
    adapter = A2AAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "port": port,
                "iron_rod_mode": True,
                "advertised_toolsets": [],
                "max_body_bytes": 256,
                "read_timeout": 10,
                "max_concurrency": 1,
            },
        )
    )
    adapter._message_handler = object()

    async def handle(event):
        await adapter.send(event.source.chat_id, "done", metadata={"notify": True})

    adapter.handle_message = handle

    async def run():
        assert await adapter.connect() is True
        try:
            card_request = urllib.request.Request(f"http://127.0.0.1:{port}/.well-known/agent-card.json")
            with urllib.request.urlopen(card_request, timeout=5) as response:
                card = json.loads(response.read().decode())
            assert card["skills"][0]["id"] == "general"
            assert card["capabilities"]["streaming"] is False
            assert card["capabilities"]["pushNotifications"] is False

            for method in ("message/stream", "tasks/pushNotificationConfig/create"):
                _, response = await asyncio.to_thread(_post, f"http://127.0.0.1:{port}/", _body(method))
                assert "error" in response

            oversized = _body("SendMessage", "x" * 1000)
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                await asyncio.to_thread(_post, f"http://127.0.0.1:{port}/", oversized)
            assert exc_info.value.code == 413
        finally:
            await adapter.disconnect()

    asyncio.run(run())


# ═════════════════════════════════════════════════════════════════════════════
# Strict mode must gate the fan-out path (a2a_orchestrate), not just a2a_call
# ═════════════════════════════════════════════════════════════════════════════

_STRICT_LITERAL_TOKEN = {
    "a2a": {"strict_mode": True},
    "a2a_agents": {
        "peer": {
            "url": "https://peer.invalid",
            "auth": {"type": "bearer", "token": "literal-forbidden-in-strict"},
            "capabilities": ["review"],
        }
    },
}


def _no_network(monkeypatch) -> list:
    """Fail the test if any path actually tries to reach a peer."""
    calls: list = []

    def fake_send(agent_label, peer, message, context_id):
        calls.append((agent_label, peer))
        return "ok", "ctx", "completed"

    monkeypatch.setattr(tools, "_send_task", fake_send)
    return calls


def test_strict_literal_token_is_refused_on_both_call_and_orchestrate(monkeypatch):
    """Both entry points must enforce the strict credential policy, with no network."""
    monkeypatch.setattr(tools, "_load_config", lambda: _STRICT_LITERAL_TOKEN)
    calls = _no_network(monkeypatch)

    assert tools.a2a_call({"agent": "peer", "message": "hi"}).startswith("Error: unknown agent")

    for mode in ("all", "first", "best"):
        out = tools.a2a_orchestrate({"capability": "review", "message": "hi", "mode": mode})
        assert out.startswith("Error:") or out.startswith("All peers failed:"), out
    assert calls == [], "strict mode must not attempt any outbound call"


def test_strict_unresolved_token_env_is_refused_fail_closed(monkeypatch):
    """A token_env that resolves to nothing must not silently fall back to no auth."""
    cfg = {
        "a2a": {"strict_mode": True},
        "a2a_agents": {
            "peer": {
                "url": "https://peer.invalid",
                "auth": {"type": "bearer", "token_env": "ABSENT_PEER_TOKEN"},
                "capabilities": ["review"],
            }
        },
    }
    monkeypatch.setattr(tools, "_load_config", lambda: cfg)
    monkeypatch.delenv("ABSENT_PEER_TOKEN", raising=False)
    calls = _no_network(monkeypatch)

    assert tools.a2a_call({"agent": "peer", "message": "hi"}).startswith("Error: unknown agent")
    out = tools.a2a_orchestrate({"capability": "review", "message": "hi"})
    assert out.startswith("Error:") or out.startswith("All peers failed:"), out
    assert calls == [], "an unresolved token_env must fail closed on the fan-out path"


def test_strict_resolved_token_env_reaches_the_peer_on_both_paths(monkeypatch):
    """Nominal strict case: a resolved token_env is forwarded on call and orchestrate."""
    cfg = {
        "a2a": {"strict_mode": True},
        "a2a_agents": {
            "peer": {
                "url": "https://peer.invalid",
                "auth": {"type": "bearer", "token_env": "PEER_TOKEN"},
                "capabilities": ["review"],
            }
        },
    }
    monkeypatch.setattr(tools, "_load_config", lambda: cfg)
    monkeypatch.setenv("PEER_TOKEN", "resolved-value")
    calls = _no_network(monkeypatch)

    assert not tools.a2a_call({"agent": "peer", "message": "hi"}).startswith("Error:")
    out = tools.a2a_orchestrate({"capability": "review", "message": "hi"})
    assert out.startswith("Orchestrated 'review' to 1 peer(s):"), out
    assert len(calls) == 2
    for _label, peer in calls:
        assert peer["auth"] == {"type": "bearer", "token": "resolved-value"}
        assert peer["url"] == "https://peer.invalid"


def test_non_strict_mode_keeps_fan_out_compatibility(monkeypatch):
    """Without strict mode the historical permissive fan-out behaviour is preserved."""
    cfg = {
        "a2a_agents": {
            "peer": {
                "url": "https://peer.invalid",
                "auth": {"type": "bearer", "token": "literal-ok-when-not-strict"},
                "capabilities": ["review"],
            }
        }
    }
    monkeypatch.setattr(tools, "_load_config", lambda: cfg)
    calls = _no_network(monkeypatch)

    out = tools.a2a_orchestrate({"capability": "review", "message": "hi"})
    assert out.startswith("Orchestrated 'review' to 1 peer(s):"), out
    assert len(calls) == 1
    assert calls[0][1]["auth"]["token"] == "literal-ok-when-not-strict"


def test_orchestrate_all_mode_reports_success_failures_mixed(monkeypatch):
    """A partially-failing fan-out still reports successes under the normal header."""
    cfg = {
        "a2a_agents": {
            "good": {"url": "http://good.example", "capabilities": ["review"]},
            "bad": {"url": "http://bad.example", "capabilities": ["review"]},
        }
    }
    monkeypatch.setattr(tools, "_load_config", lambda: cfg)

    def fake_send(agent_label, peer, message, context_id):
        if agent_label == "bad":
            raise urllib.error.URLError("connection refused")
        return "fine", "ctx", "completed"

    monkeypatch.setattr(tools, "_send_task", fake_send)
    out = tools.a2a_orchestrate({"capability": "review", "message": "hi"})
    assert out.startswith("Orchestrated 'review' to 2 peer(s):"), out
    assert "--- good ---" in out
    assert "Error:" in out
