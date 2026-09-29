"""Release checks and mutation admission must agree across profiles and surfaces."""
import asyncio
import io
import json
from argparse import Namespace

import pytest

from hermes_cli import __release_date__
from hermes_cli import update_release_policy as policy


@pytest.mark.parametrize("kind", ["current", "new", "older", "offline", "invalid", "prerelease"])
def test_release_checks_never_compare_main(tmp_path, monkeypatch, kind):
    from hermes_cli import banner, main, web_server
    from hermes_cli.web_routers import actions
    (tmp_path / policy.MARKER).write_text("release-only")
    monkeypatch.setattr(policy, "_ROOT", tmp_path)
    monkeypatch.setattr(policy, "_cache", None)
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(actions, "_dashboard_local_update_managed_externally", lambda: False)
    def fetch(request, **kwargs):
        assert request.full_url == policy.RELEASE_API
        if kind == "offline":
            raise OSError("network unavailable")
        tag = {"new": "v2099.1.1", "older": "v2020.1.1", "invalid": "main"}.get(kind, "v" + __release_date__)
        return io.BytesIO(json.dumps({"tag_name": tag, "draft": False,
                                     "prerelease": kind == "prerelease"}).encode())
    monkeypatch.setattr(policy.urllib.request, "urlopen", fetch)
    def forbidden(*a, **kw):
        pytest.fail("Development branch check or mutation was reached")
    monkeypatch.setattr(banner, "_check_via_local_git", forbidden)
    monkeypatch.setattr(main, "_resolve_update_branch", forbidden)
    for home in (tmp_path / "a", tmp_path / "b", tmp_path / "a"):
        monkeypatch.setenv("HERMES_HOME", str(home))
        result = asyncio.run(actions.check_hermes_update(force=True))
        assert result["can_apply"] is False
        assert result["update_available"] is (kind == "new")
        expected = None if kind in {"offline", "invalid", "prerelease"} else (-1 if kind == "new" else 0)
        assert result["behind"] == expected
        assert banner.check_for_updates() == expected
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
