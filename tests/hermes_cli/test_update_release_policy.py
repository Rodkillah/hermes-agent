"""Local patch admission stays separate from native release discovery."""
import asyncio
from argparse import Namespace
import pytest
from hermes_cli import update_release_policy as policy


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


@pytest.mark.parametrize("error", [None, "release-unavailable"])
def test_managed_check_uses_native_release_result(tmp_path, monkeypatch, capsys, error):
    from hermes_cli import main, source_check
    (tmp_path / policy.MARKER).write_text("release-only", encoding="utf-8")
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    expected = {"supported": True, "channel": "stable", "behind": 0, "error": error}
    calls = []
    def native_check(**kwargs):
        calls.append(kwargs)
        return expected
    monkeypatch.setattr(source_check, "check_for_updates", native_check)
    if error:
        with pytest.raises(SystemExit) as exc:
            main._update_preflight_handled(Namespace(check=True))
        assert exc.value.code == 1
    else:
        assert main._update_preflight_handled(Namespace(check=True)) is True
    import json
    assert json.loads(capsys.readouterr().out) == expected
    assert calls == [{"force": True}]
