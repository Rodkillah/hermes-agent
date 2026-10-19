"""Shared-listener callbacks must retain the dedicated adapter's query-secret privacy."""

import errno
import logging
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms import api_server
from gateway.platforms.bluebubbles import BlueBubblesAdapter
from gateway.platforms.shared_ingress import bind_listener


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_bind", [False, True])
async def test_shared_bluebubbles_callback_does_not_log_its_query_secret(
    tmp_path, monkeypatch, caplog, retry_bind
):
    root = tmp_path / ".hermes"
    profile_home = root / "profiles" / "worker"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: profile_home)
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", root), ("worker", profile_home)],
    )
    secret = "synthetic-callback-password-928347"
    callback = BlueBubblesAdapter(PlatformConfig(enabled=True, extra={
        "server_url": "http://127.0.0.1:1", "password": secret,
    }))
    callback._shared_listener_profile = "worker"
    listener = api_server.APIServerAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": 0, "key": "synthetic-gateway-api-key-928347",
    }))
    runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=True),
        adapters={},
        _profile_adapters={"worker": {Platform.BLUEBUBBLES: callback}},
    )
    listener.gateway_runner = callback.gateway_runner = runner
    app = web.Application()
    app.router.add_post(callback.webhook_path, callback._handle_webhook)
    await bind_listener(callback, app, "127.0.0.1", 0, callback.webhook_path, access_log=None)
    real_start = api_server.start_tcp_site
    attempts = []

    async def start(*args, **kwargs):
        attempts.append(True)
        if retry_bind and len(attempts) == 1:
            raise OSError(errno.EADDRINUSE, "synthetic bind collision")
        return await real_start(*args, **kwargs)

    monkeypatch.setattr(api_server, "start_tcp_site", start)
    caplog.set_level(logging.INFO, logger="aiohttp.access")
    try:
        assert await listener.connect()
        port = listener._site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"http://127.0.0.1:{port}/p/worker{callback.webhook_path}",
                params={"password": secret}, json={"type": "synthetic-non-message-event"},
            ) as response:
                assert response.status == 200
                await response.read()
    finally:
        await listener.disconnect()
    assert secret not in caplog.text
