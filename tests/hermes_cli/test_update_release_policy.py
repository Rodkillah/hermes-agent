"""Release checks and mutation admission must agree across profiles and surfaces."""
import asyncio
import io
import json
from argparse import Namespace

import pytest

from hermes_cli import __release_date__
from hermes_cli import update_release_policy as policy


@pytest.mark.parametrize("kind", ["current", "new", "older", "offline", "invalid", "prerelease", "semver", "malformed", "bad_date"])
def test_release_checks_never_compare_main(tmp_path, monkeypatch, kind):
    from hermes_cli import banner, main, web_server, source_check, version_info
    from hermes_cli.web_routers import actions
    (tmp_path / policy.MARKER).write_text("release-only")
    monkeypatch.setattr(policy, "_ROOT", tmp_path)
    monkeypatch.setattr(policy, "_cache", None)
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(actions, "_dashboard_local_update_managed_externally", lambda: False)
    monkeypatch.setattr(version_info, "get_version_info", lambda: Namespace(base_version="0.21.5"))
    def fetch(request, **kwargs):
        assert request.full_url == policy.RELEASE_API
        if kind == "offline":
            raise OSError("network unavailable")
        tag = {"new": "v2099.1.1", "older": "v2020.1.1", "invalid": "main", "semver": "v0.21.6", "malformed": "v0.21.6-rc1", "bad_date": "v2026.13.1"}.get(kind, "v" + __release_date__)
        return io.BytesIO(json.dumps({"tag_name": tag, "draft": False,
                                     "prerelease": kind == "prerelease", "published_at": "2026-09-24T10:09:38Z"}).encode())
    monkeypatch.setattr(policy.urllib.request, "urlopen", fetch)
    def forbidden(*a, **kw):
        pytest.fail("Development branch check or mutation was reached")
    monkeypatch.setattr(source_check, "_read_checkout", forbidden)
    monkeypatch.setattr(main, "_resolve_update_branch", forbidden)
    for home in (tmp_path / "a", tmp_path / "b", tmp_path / "a"):
        monkeypatch.setenv("HERMES_HOME", str(home))
        result = asyncio.run(actions.check_hermes_update(force=True))
        assert result["can_apply"] is False
        assert result["update_available"] is (kind == "semver")
        expected = None if kind in {"offline", "invalid", "prerelease", "malformed", "bad_date", "new"} else (-1 if kind == "semver" else 0)
        assert result["behind"] == expected
        assert banner.check_for_updates() == expected
        if expected is None:
            assert result["error"] == "release-check-failed"
            with pytest.raises(SystemExit) as exc:
                main._update_preflight_handled(Namespace(check=True))
            assert exc.value.code == 1
        else:
            assert main._update_preflight_handled(Namespace(check=True)) is True


@pytest.mark.parametrize("branch", [None, "main", "v2099.1.1"])
def test_apply_refused_before_snapshot_or_spawn(tmp_path, monkeypatch, branch):
    from hermes_cli import main, web_server, update_cmd
    from hermes_cli.web_routers import actions
    from hermes_cli.update_contract import evaluate_update_admission
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(actions, "_dashboard_local_update_managed_externally", lambda: False)
    (tmp_path / policy.MARKER).write_text("release-only")
    def forbidden(*a, **kw):
        pytest.fail("Updater or snapshot started despite release-only policy")
    monkeypatch.setattr(update_cmd, "_cmd_update_impl", forbidden)
    monkeypatch.setattr(main, "_run_pre_update_backup", forbidden)
    monkeypatch.setattr(actions, "_spawn_hermes_action", forbidden)
    with pytest.raises(SystemExit) as exc:
        main.cmd_update(Namespace(check=False, branch=branch, force=True, yes=True))
    assert exc.value.code == 2
    result = asyncio.run(actions.update_hermes())
    assert result["ok"] is False and result["pid"] is None
    assert result["error"] == "release_update_managed"
    assert evaluate_update_admission(tmp_path).code == "release-only"


def test_release_only_marker_disables_dashboard_update_capability(tmp_path, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli import web_server_files

    (tmp_path / policy.MARKER).write_text("release-only")
    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_server_files, "_default_hermes_root_is_opt_data", lambda: False)

    assert web_server_files._dashboard_local_update_managed_externally() is True


@pytest.mark.parametrize("tag,current,expected", [
    ("v0.21.6", "0.21.5", True), ("v0.21.6", "0.21.6", False),
    ("v0.21.6", "0.22.0", False), ("v0.21.10", "0.21.9", True),
    ("v2026.9.24", "0.21.5", False), ("v2026.9.24", "0.21.6", False),
])
def test_release_order_uses_calendar_or_semantic_identity(tag, current, expected):
    assert policy._newer_release(tag, current, "2026.9.24") is expected


@pytest.mark.parametrize("tag", ["main", "v0.21", "v0.21.6-rc1", "v0.21.6+local", "v2026.2.30", "v2026.9.25"])
def test_invalid_release_identity_is_an_error(tag):
    with pytest.raises(ValueError):
        policy._newer_release(tag, "0.21.5", "2026.9.24")


def test_semver_with_unknown_installed_version_is_an_error():
    with pytest.raises(ValueError):
        policy._newer_release("v0.21.6", "unknown", "2026.9.24")


def test_release_cache_does_not_mix_explicit_checkout_versions(monkeypatch):
    monkeypatch.setattr(policy, "_cache", None)
    def fetch(*a, **kw):
        return io.BytesIO(json.dumps({"tag_name": "v0.21.6", "draft": False,
            "prerelease": False, "published_at": "2026-09-24T10:09:38Z"}).encode())
    monkeypatch.setattr(policy.urllib.request, "urlopen", fetch)
    assert policy.release_status(current_version="0.21.5")["update_available"] is True
    assert policy.release_status(current_version="0.21.6")["update_available"] is False


def test_source_check_cli_exposes_release_failure_with_nonzero_exit(monkeypatch, capsys, tmp_path):
    import sys
    from hermes_cli import source_check
    monkeypatch.setattr(sys, "argv", ["source_check", "--install-root", str(tmp_path), "--home", str(tmp_path)])
    monkeypatch.setattr(source_check, "check_for_updates", lambda **kw: {"error": "release-check-failed", "behind": None})
    with pytest.raises(SystemExit) as exc:
        source_check.main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "release-check-failed"
