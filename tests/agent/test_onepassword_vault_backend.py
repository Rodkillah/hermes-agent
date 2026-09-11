"""Hermetic tests for the 1Password vault backend's ``--vault`` resolution.

The real ``op`` binary is never invoked: ``run_cli`` is mocked so the suite
stays fast and offline-safe. Synthetic secrets only — no real credential ever
enters a test, an assertion, or a log.

The defect under test: ``list_items`` receives ``item.vault`` but produces the
legacy handle ``op:<item_id>``; ``resolve_password`` / ``resolve_otp`` then run
``op item get <id>`` without ``--vault``, which a service account rejects
("a vault query must be provided..."). The fix keeps the public handle as
``op:<item_id>`` and maintains an in-memory ``item_id -> vault`` index built
only from the non-sensitive ``item.vault`` metadata returned by ``list_items``.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from agent.vault_backends import onepassword as op_backend
from agent.vault_backends.onepassword import OnePasswordLoginBackend


# Synthetic fixtures (never real secrets).
_ITEMS = [
    {
        "id": "itemA",
        "title": "GitHub",
        "vault": {"id": "vault1", "name": "Personal"},
        "urls": [{"href": "https://github.com/login"}],
        "additional_information": "user@example.com",
        "created_at": "2026-01-01T00:00:00Z",
    },
    {
        "id": "itemB",
        "title": "Work",
        "vault": {"id": "vault2", "name": "Work"},
        "urls": [{"href": "https://work.example.com/login"}],
        "additional_information": "user@work.com",
        "created_at": "2026-01-01T00:00:00Z",
    },
]

_PASSWORDS = {"itemA": "synthetic-pw-A", "itemB": "synthetic-pw-B"}
_OTPS = {"itemA": "123456"}  # itemB has no OTP field


def _make_fake_run(items, passwords, otps, calls):
    """A stand-in for ``run_cli`` that records argv and answers the three op commands."""

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["item", "list"]:
            return Mock(returncode=0, stdout=json.dumps(items), stderr="")
        if argv[1:3] == ["item", "get"]:
            item_id = argv[3]
            if "--otp" in argv:
                code = otps.get(item_id)
                if code is None:
                    return Mock(returncode=1, stdout="", stderr="no one-time password field")
                return Mock(returncode=0, stdout=code + "\n", stderr="")
            if "--fields" in argv:
                pw = passwords.get(item_id)
                if pw is None:
                    return Mock(returncode=1, stdout="", stderr="item not found")
                return Mock(returncode=0, stdout=pw + "\n", stderr="")
        return Mock(returncode=1, stdout="", stderr="unexpected command")

    return fake_run


@pytest.fixture
def backend_factory(tmp_path, monkeypatch):
    def _make(items=_ITEMS, passwords=_PASSWORDS, otps=_OTPS):
        op_bin = tmp_path / "op"
        op_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        op_bin.chmod(0o755)
        monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "synthetic-service-token")
        calls = []
        monkeypatch.setattr(op_backend, "run_cli", _make_fake_run(items, passwords, otps, calls))
        backend = OnePasswordLoginBackend({"binary_path": str(op_bin)})
        return backend, calls

    return _make


def _get_calls(calls):
    return [c for c in calls if c[1:3] == ["item", "get"]]


def test_list_items_keeps_legacy_handles_and_never_leaks_secrets(backend_factory):
    backend, _calls = backend_factory()
    metas = backend.list_items()
    assert [m.id for m in metas] == ["op:itemA", "op:itemB"]
    # No vault identity leaks into the public handle.
    assert all("vault" not in h for h in (m.id for m in metas))
    # Metadata never carries a secret value.
    dumped = json.dumps([m.to_dict() for m in metas])
    assert "synthetic-pw-A" not in dumped
    assert "synthetic-pw-B" not in dumped


def test_resolve_password_adds_correct_vault_per_item(backend_factory):
    backend, calls = backend_factory()
    backend.list_items()  # populate the index
    assert backend.resolve_password("op:itemA") == "synthetic-pw-A"
    assert backend.resolve_password("op:itemB") == "synthetic-pw-B"
    get_calls = [c for c in _get_calls(calls) if "--fields" in c]
    assert len(get_calls) == 2
    vaults = {c[3]: c[c.index("--vault") + 1] for c in get_calls}
    assert vaults == {"itemA": "vault1", "itemB": "vault2"}


def test_resolve_password_recovers_legacy_handle_via_bounded_list(backend_factory):
    backend, calls = backend_factory()
    # No prior list_items: the index is empty, resolve must recover via item list.
    assert backend.resolve_password("op:itemA") == "synthetic-pw-A"
    assert any(c[1:3] == ["item", "list"] for c in calls)
    get_calls = _get_calls(calls)
    assert get_calls and "--vault" in get_calls[0]


def test_missing_vault_fails_closed_without_unbounded_get(backend_factory):
    items = [dict(_ITEMS[0], vault={})]  # itemA with no vault identity
    backend, calls = backend_factory(items=items)
    with pytest.raises(RuntimeError, match="vault"):
        backend.resolve_password("op:itemA")
    # Fail closed before any unbounded item get.
    assert not _get_calls(calls)


def test_every_item_get_carries_vault(backend_factory):
    backend, calls = backend_factory()
    backend.list_items()
    backend.resolve_password("op:itemA")
    backend.resolve_otp("op:itemA")
    get_calls = _get_calls(calls)
    assert get_calls
    assert all("--vault" in c for c in get_calls)


def test_resolve_otp_adds_vault_and_returns_code(backend_factory):
    backend, calls = backend_factory()
    backend.list_items()
    assert backend.resolve_otp("op:itemA") == "123456"
    otp_calls = [c for c in _get_calls(calls) if "--otp" in c]
    assert otp_calls and otp_calls[0][otp_calls[0].index("--vault") + 1] == "vault1"


def test_resolve_otp_without_field_returns_none(backend_factory):
    backend, _calls = backend_factory()
    backend.list_items()
    assert backend.resolve_otp("op:itemB") is None


def test_resolve_otp_missing_vault_is_not_swallowed_as_no_otp(backend_factory):
    items = [dict(_ITEMS[0], vault={})]
    backend, _calls = backend_factory(items=items)
    with pytest.raises(RuntimeError, match="vault"):
        backend.resolve_otp("op:itemA")


def test_vault_name_fallback_when_no_id(backend_factory):
    items = [dict(_ITEMS[0], vault={"name": "Personal"})]
    backend, calls = backend_factory(items=items)
    backend.list_items()
    assert backend.resolve_password("op:itemA") == "synthetic-pw-A"
    get_calls = _get_calls(calls)
    assert get_calls[0][get_calls[0].index("--vault") + 1] == "Personal"


def test_no_synthetic_secret_in_errors(backend_factory):
    items = [dict(_ITEMS[0], vault={})]
    backend, _calls = backend_factory(items=items)
    with pytest.raises(RuntimeError) as exc:
        backend.resolve_password("op:itemA")
    assert "synthetic-pw-A" not in str(exc.value)
    assert "123456" not in str(exc.value)


def test_locked_backend_raises_unlock_required_not_no_vault(backend_factory, monkeypatch):
    from agent.vault_backends import unlock as _unlock
    from agent.vault_backends.base import UnlockRequired

    backend, _calls = backend_factory()
    # No service token and no session token -> locked.
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    backend._service_token = ""
    _unlock.lock("onepassword")
    with pytest.raises(UnlockRequired):
        backend.resolve_password("op:itemA")
    with pytest.raises(UnlockRequired):
        backend.resolve_otp("op:itemA")


def test_conflicting_vault_metadata_fails_closed_without_item_get(backend_factory):
    # The same item_id appears under two distinct vaults: no last-wins, fail closed.
    items = [
        dict(_ITEMS[0], vault={"id": "vault-a", "name": "A"}),
        dict(_ITEMS[0], vault={"id": "vault-b", "name": "B"}),
    ]
    backend, calls = backend_factory(items=items)
    backend.list_items()
    with pytest.raises(RuntimeError, match="vault"):
        backend.resolve_password("op:itemA")
    with pytest.raises(RuntimeError, match="vault"):
        backend.resolve_otp("op:itemA")
    assert not _get_calls(calls)


def test_metadata_refresh_drops_stale_vault_before_resolution(backend_factory):
    # A later authoritative refresh no longer carries a vault identity: the old
    # resolution must be dropped, not silently reused.
    items = [dict(_ITEMS[0], vault={"id": "vault-a", "name": "A"})]
    backend, calls = backend_factory(items=items)
    backend.list_items()
    items[0]["vault"] = {}
    backend.list_items()
    with pytest.raises(RuntimeError, match="vault"):
        backend.resolve_password("op:itemA")
    with pytest.raises(RuntimeError, match="vault"):
        backend.resolve_otp("op:itemA")
    assert not _get_calls(calls)


def test_ambiguous_vault_after_refresh_fails_closed_without_item_get(backend_factory):
    # A refresh that turns a single identity into two distinct ones must block
    # resolution entirely (no last-wins), for both password and OTP.
    items = [dict(_ITEMS[0], vault={"id": "vault-a", "name": "A"})]
    backend, calls = backend_factory(items=items)
    backend.list_items()
    items.append(dict(_ITEMS[0], vault={"id": "vault-b", "name": "B"}))
    backend.list_items()
    with pytest.raises(RuntimeError, match="vault"):
        backend.resolve_password("op:itemA")
    with pytest.raises(RuntimeError, match="vault"):
        backend.resolve_otp("op:itemA")
    assert not _get_calls(calls)
