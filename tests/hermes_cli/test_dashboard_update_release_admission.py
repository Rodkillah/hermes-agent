"""Dashboard apply refuses before spawn through the shared native admission gate."""

import asyncio
import json
from unittest.mock import Mock

import pytest

from hermes_cli import update_contract as contract
from hermes_cli import update_release_policy as policy
from hermes_cli.web_routers import actions


@pytest.fixture
def apply_boundary(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setattr(actions, "_server_path", lambda name: root)
    completed = Mock()
    receipt = Mock()
    admission = Mock(wraps=contract.evaluate_update_admission)
    spawn = Mock(side_effect=AssertionError("updater must not spawn"))
    monkeypatch.setattr(actions, "_record_completed_action", completed)
    monkeypatch.setattr(contract, "record_refusal_receipt", receipt)
    monkeypatch.setattr(contract, "evaluate_update_admission", admission)
    monkeypatch.setattr(actions, "_spawn_hermes_action", spawn)
    return root, completed, receipt, spawn, admission


def _stamp_commit_build(root):
    (root / "install-stamp.json").write_text(json.dumps({
        "source": "commit-build", "distribution": "desktop-app", "payload": "bundled",
        "updateMechanism": "external", "commit": "a" * 40,
        "displayVersion": "1.2.3+gabcdef12", "baseVersion": "1.2.3", "channel": "canary",
    }), encoding="utf-8")


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("commit_build", [False, True])
def test_marker_refuses_dashboard_apply_before_spawn(
    apply_boundary, monkeypatch, managed, commit_build,
):
    root, completed, receipt, spawn, admission = apply_boundary
    (root / policy.MARKER).write_text("release-only", encoding="utf-8")
    if commit_build:
        _stamp_commit_build(root)
    managed_probe = Mock(return_value=managed)
    monkeypatch.setattr(actions, "_dashboard_local_update_managed_externally", managed_probe)

    result = asyncio.run(actions.update_hermes())

    assert result == {
        "ok": False, "pid": None, "name": "hermes-update",
        "error": "release_update_managed", "message": policy.MESSAGE,
        "update_command": policy.COMMAND,
    }
    completed.assert_called_once_with("hermes-update", policy.MESSAGE, exit_code=1)
    receipt.assert_called_once_with(contract.UpdateRefusal("release-only", policy.MESSAGE, policy.COMMAND))
    managed_probe.assert_not_called()
    spawn.assert_not_called()
    admission.assert_called_once_with(root)


def test_commit_build_still_refuses_dashboard_apply_before_managed_probe(
    apply_boundary, monkeypatch,
):
    root, completed, receipt, spawn, admission = apply_boundary
    _stamp_commit_build(root)
    managed_probe = Mock(return_value=True)
    monkeypatch.setattr(actions, "_dashboard_local_update_managed_externally", managed_probe)

    result = asyncio.run(actions.update_hermes())

    assert result["ok"] is False and result["pid"] is None
    assert result["message"] == contract.COMMIT_BUILD_UPDATE_MESSAGE
    completed.assert_called_once_with("hermes-update", contract.COMMIT_BUILD_UPDATE_MESSAGE, exit_code=1)
    receipt.assert_called_once_with(contract.UpdateRefusal("commit-build", contract.COMMIT_BUILD_UPDATE_MESSAGE, ""))
    managed_probe.assert_not_called()
    spawn.assert_not_called()
    admission.assert_called_once_with(root)


def test_managed_dashboard_still_refuses_apply_after_shared_admission(
    apply_boundary, monkeypatch,
):
    from hermes_cli import config, image_provenance, steward
    import hermes_constants

    root, completed, receipt, spawn, admission = apply_boundary
    monkeypatch.setattr(image_provenance, "read_image_provenance", lambda: None)
    monkeypatch.setattr(steward, "sealed_steward", lambda root: None)
    monkeypatch.setattr(hermes_constants, "is_termux", lambda: False)
    monkeypatch.setattr(config, "detect_install_method", lambda root: "git")
    managed_probe = Mock(return_value=True)
    monkeypatch.setattr(actions, "_dashboard_local_update_managed_externally", managed_probe)

    result = asyncio.run(actions.update_hermes())

    assert result["ok"] is False and result["pid"] is None
    assert result["error"] == "dashboard_update_managed_externally"
    completed.assert_called_once_with("hermes-update", result["message"], exit_code=1)
    managed_probe.assert_called_once_with()
    receipt.assert_not_called()
    spawn.assert_not_called()
    admission.assert_called_once_with(root)
