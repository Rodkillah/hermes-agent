"""Iron Rod A2A hardening kept on top of the upstream plugin (29/09/2026 slimming).

Only two bricks survive the return to the native plugin:

1. ``platforms.a2a.extra.allowed_methods``: optional JSON-RPC allow-list, matched by handler.
2. Refused requests (401, 403, 413, method refused) leave a metadata-only audit line.

Everything else (peer tokens, trust gate, secret references, persistence, live-session
injection) is upstream behavior and is covered by ``test_a2a_plugin.py``.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request

import pytest

from plugins.platforms.a2a import protocol


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _adapter(extra=None):
    from gateway.config import PlatformConfig
    from plugins.platforms.a2a.adapter import A2AAdapter

    return A2AAdapter(PlatformConfig(enabled=True, extra=dict(extra or {})))


def _handler(method: str) -> str:
    from plugins.platforms.a2a.adapter import _METHODS

    return _METHODS[method][0]


class TestAllowedMethods:
    def test_unset_keeps_every_upstream_method(self):
        adapter = _adapter()
        assert adapter._method_allowed(_handler("SendMessage"))
        assert adapter._method_allowed(_handler("GetTask"))
        assert adapter._method_allowed(_handler("CreateTaskPushNotificationConfig"))

    def test_allow_list_matches_by_handler(self):
        adapter = _adapter({"allowed_methods": ["SendMessage"]})
        assert adapter._method_allowed(_handler("SendMessage"))
        # The pre-1.0 alias shares the handler, so it stays allowed.
        assert adapter._method_allowed(_handler("message/send"))
        assert not adapter._method_allowed(_handler("GetTask"))
        assert not adapter._method_allowed(_handler("SendStreamingMessage"))
        assert not adapter._method_allowed(_handler("CreateTaskPushNotificationConfig"))

    def test_string_value_is_accepted(self):
        adapter = _adapter({"allowed_methods": "SendMessage"})
        assert adapter._method_allowed(_handler("SendMessage"))
        assert not adapter._method_allowed(_handler("ListTasks"))

    def test_unknown_names_allow_nothing(self):
        adapter = _adapter({"allowed_methods": ["NoSuchMethod"]})
        assert not adapter._method_allowed(_handler("SendMessage"))


class TestGatewayAuthorization:
    """The retired Iron Rod ``role_authorized=True`` patch is covered by upstream
    ``authorization_is_upstream``: an authenticated peer passes the gateway authz layer."""

    @pytest.fixture(autouse=True)
    def _no_allowlists(self, monkeypatch):
        for key in ("A2A_ALLOWED_USERS", "A2A_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS"):
            monkeypatch.delenv(key, raising=False)

    @staticmethod
    def _runner(adapter):
        from unittest.mock import MagicMock

        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.adapters = {adapter.platform: adapter}
        runner.pairing_store = MagicMock()
        runner.pairing_store.is_approved.return_value = False
        runner.pairing_store._is_rate_limited.return_value = False
        return runner

    def test_authenticated_peer_is_authorized_without_role_flag(self):
        adapter = _adapter()
        src = adapter.build_source(chat_id="ctx-1", chat_name="a2a:cockpit", chat_type="dm",
                                   user_id="cockpit", user_name="cockpit")
        assert getattr(src, "role_authorized", False) is False
        assert self._runner(adapter)._is_user_authorized(src) is True

    def test_same_source_is_denied_when_adapter_does_not_delegate(self):
        from types import SimpleNamespace

        adapter = _adapter()
        src = adapter.build_source(chat_id="ctx-1", chat_name="a2a:cockpit", chat_type="dm",
                                   user_id="cockpit", user_name="cockpit")
        fake = SimpleNamespace(platform=adapter.platform, authorization_is_upstream=False,
                               enforces_own_access_policy=False)
        assert self._runner(fake)._is_user_authorized(src) is False


def _post(url, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _send_message(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {"message": protocol.text_message(protocol.ROLE_USER, text)},
    }


def _audit_lines(home) -> list[dict]:
    path = home / "a2a_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestLiveRefusals:
    @pytest.fixture
    def live(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("A2A_PEER_TOKENS", "cockpit:tok-cockpit")
        monkeypatch.setenv("A2A_HOST", "127.0.0.1")
        port = _free_port()
        monkeypatch.setenv("A2A_PORT", str(port))
        adapter = _adapter({"port": port, "allowed_methods": ["SendMessage"]})

        async def fake_handle_message(event):
            await adapter.send(event.source.chat_id, "ECHO: " + event.text, metadata={"notify": True})

        adapter.handle_message = fake_handle_message  # type: ignore[assignment]
        adapter._message_handler = object()
        return adapter, f"http://127.0.0.1:{port}/", tmp_path

    def test_refused_method_is_rejected_and_traced_without_content(self, live):
        adapter, url, home = live

        async def run():
            assert await adapter.connect() is True
            try:
                body = {"jsonrpc": "2.0", "id": 2, "method": "GetTask",
                        "params": {"id": "task-x", "note": "SECRET-CANARY"}}
                status, resp = await asyncio.to_thread(_post, url, body, "tok-cockpit")
                assert status == 200
                assert resp["error"]["code"] == protocol.ERR_METHOD_NOT_FOUND
                assert "not enabled" in resp["error"]["message"]

                status, resp = await asyncio.to_thread(_post, url, _send_message("hello"), "tok-cockpit")
                assert status == 200 and "error" not in resp
            finally:
                await adapter.disconnect()

        asyncio.run(run())
        lines = _audit_lines(home)
        rejected = [r for r in lines if "rejected" in r.get("summary", "")]
        assert len(rejected) == 1
        assert rejected[0]["peer"] == "cockpit"
        assert "method=GetTask" in rejected[0]["summary"]
        assert "SECRET-CANARY" not in (home / "a2a_audit.jsonl").read_text(encoding="utf-8")

    def test_unauthenticated_request_is_traced_by_ip(self, live):
        adapter, url, home = live

        async def run():
            assert await adapter.connect() is True
            try:
                status, resp = await asyncio.to_thread(_post, url, _send_message("SECRET-CANARY"), "wrong-token")
                assert status == 401
                assert resp["error"]["code"] == protocol.ERR_UNAUTHORIZED
            finally:
                await adapter.disconnect()

        asyncio.run(run())
        rejected = [r for r in _audit_lines(home) if "rejected" in r.get("summary", "")]
        assert len(rejected) == 1
        assert rejected[0]["peer"] == "ip:127.0.0.1"
        assert "http=401" in rejected[0]["summary"]
        assert "SECRET-CANARY" not in (home / "a2a_audit.jsonl").read_text(encoding="utf-8")
