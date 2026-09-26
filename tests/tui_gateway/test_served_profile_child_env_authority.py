"""A routed profile's child never receives a launch-profile credential — whatever its provenance.

Two authority edges of ``served_profile_child_env`` (review of #111617):

* a credential injected into the LAUNCH process by systemd / Compose / the shell is in no ``.env``
  and no source snapshot, so a name-based strip cannot see it; the child for routed profile B must
  still not carry it when B does not define the same name (``get_secret``'s multiplex contract:
  a scoped miss is *no credential*, never ambient fallback);
* the Desktop/dashboard backend serves ``?profile=B`` by installing a HERMES_HOME override WITHOUT
  the gateway-wide multiplex flag — the strip must key on "this task serves a routed home", not on
  that flag; the browser passthrough must not fall through to the launch key on a B miss either.
"""

import os
import sys
from pathlib import Path

import pytest

from agent.secret_scope import UnscopedSecretError, set_multiplex_active
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.environments.local import served_profile_child_env


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Launch home A (its .env in os.environ) plus an AMBIENT-only OPENAI key A never wrote to a file;
    served home B defines neither."""
    a = tmp_path / ".hermes"
    b = a / "profiles" / "b"
    b.mkdir(parents=True)
    (a / ".env").write_text("A_MARKER=a\nHERMES_MODEL=a-model\nFIRECRAWL_API_KEY=a-fc\n", encoding="utf-8")
    (b / ".env").write_text("B_MARKER=b\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(a))
    for key, val in (("A_MARKER", "a"), ("HERMES_MODEL", "a-model"), ("FIRECRAWL_API_KEY", "a-fc"),
                     ("OPENAI_API_KEY", "sk-ambient-launch-only")):
        monkeypatch.setenv(key, val)
    monkeypatch.delenv("B_MARKER", raising=False)
    set_multiplex_active(False)
    try:
        yield a, b
    finally:
        set_multiplex_active(False)


def test_ambient_only_launch_credential_never_reaches_a_routed_child(homes):
    """Multiplex on: A's ambient OPENAI_API_KEY (not in A's .env) is absent from B's credential-bearing
    child while B's own secrets are present; with no target and no scope bound the builder refuses."""
    a, b = homes
    set_multiplex_active(True)
    env = served_profile_child_env(target_home=b, inherit_credentials=True)
    assert env["HERMES_HOME"] == str(b) and env["B_MARKER"] == "b"
    assert "OPENAI_API_KEY" not in env and "A_MARKER" not in env and "FIRECRAWL_API_KEY" not in env
    # The raw-environ base used by the relay delivery child is scrubbed the same way.
    env = served_profile_child_env(base=os.environ, target_home=b, inherit_credentials=True)
    assert "OPENAI_API_KEY" not in env and env["B_MARKER"] == "b"
    # The launch profile's own child keeps its env (ambient injection is A's legitimate credential).
    assert served_profile_child_env(target_home=a, inherit_credentials=True)["OPENAI_API_KEY"] == "sk-ambient-launch-only"
    with pytest.raises(UnscopedSecretError):
        served_profile_child_env(inherit_credentials=True)


def test_routed_home_with_multiplex_flag_off_gets_no_launch_residue(homes, monkeypatch):
    """Desktop/dashboard topology: B served via the HERMES_HOME override only. The slash-worker /
    helper child sees B's env, and the browser passthrough resolves B's (absent) key as no key."""
    from tools.browser_tool import _build_browser_env

    a, b = homes
    token = set_hermes_home_override(str(b))
    try:
        env = served_profile_child_env(inherit_credentials=True)
        assert env["HERMES_HOME"] == str(b) and env["B_MARKER"] == "b"
        assert "A_MARKER" not in env and "HERMES_MODEL" not in env and "OPENAI_API_KEY" not in env
        browser_env = _build_browser_env()
        assert "FIRECRAWL_API_KEY" not in browser_env and browser_env["HERMES_HOME"] == str(b)
    finally:
        reset_hermes_home_override(token)
    # Control: the launch profile's own browser keeps its key and its settings.
    assert _build_browser_env()["FIRECRAWL_API_KEY"] == "a-fc"
    assert served_profile_child_env(inherit_credentials=True)["HERMES_MODEL"] == "a-model"


def test_real_child_observes_only_the_routed_profile(homes):
    """Observed from inside a real child spawned for B with the flag off."""
    import json
    import subprocess

    a, b = homes
    token = set_hermes_home_override(str(b))
    try:
        env = served_profile_child_env(inherit_credentials=True)
    finally:
        reset_hermes_home_override(token)
    probe = "import json,os;print(json.dumps({k:os.environ.get(k) for k in ('HERMES_HOME','A_MARKER','B_MARKER','OPENAI_API_KEY')}))"
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen == {"HERMES_HOME": str(b), "A_MARKER": None, "B_MARKER": "b", "OPENAI_API_KEY": None}
    assert Path(seen["HERMES_HOME"]) == b

def test_served_profile_cli_resolves_after_target_path_overlay(homes, tmp_path, monkeypatch):
    """An A2A-style bare ``hermes`` spawn works when both profile PATHs omit its bin dir."""
    import subprocess
    from tools.environments import local

    a, b = homes
    bin_dir = tmp_path / "cli-bin"
    bin_dir.mkdir()
    shim = bin_dir / "hermes"
    shim.write_text("#!/bin/sh\nprintf 'CLI_PATH_OK\\n'\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setattr(local, "_HERMES_BIN_DIR", str(bin_dir))
    for home in (a, b):
        with (home / ".env").open("a", encoding="utf-8") as dotenv:
            dotenv.write("PATH=/usr/bin:/bin\n")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    for home in (a, b, a):
        env = served_profile_child_env(target_home=home, inherit_credentials=True)
        assert env["PATH"].split(os.pathsep)[0] == str(bin_dir)
        assert env["PATH"].split(os.pathsep).count(str(bin_dir)) == 1
        assert env["PATH"].endswith("/usr/bin:/bin")
        result = subprocess.run(["hermes", "--version"], env=env, capture_output=True,
                                text=True, encoding="utf-8", timeout=10, check=True)
        assert result.stdout.strip() == "CLI_PATH_OK"
        assert env["HERMES_HOME"] == str(home)
        assert ("A_MARKER" in env) == (home == a)
        assert ("B_MARKER" in env) == (home == b)
        assert ("OPENAI_API_KEY" in env) == (home == a)


def test_raw_base_served_child_keeps_secret_isolation_and_path(homes, tmp_path, monkeypatch):
    """A relay-style raw base cannot leak A's credential into B while repairing PATH."""
    from tools.environments import local

    _, b = homes
    bin_dir = tmp_path / "cli-bin"
    bin_dir.mkdir()
    monkeypatch.setattr(local, "_HERMES_BIN_DIR", str(bin_dir))
    raw = dict(os.environ, PATH="/usr/bin:/bin")
    env = served_profile_child_env(base=raw, target_home=b, inherit_credentials=True)
    assert env["PATH"].split(os.pathsep) == [str(bin_dir), "/usr/bin", "/bin"]
    assert env["HERMES_HOME"] == str(b)
    assert env["B_MARKER"] == "b"
    assert "OPENAI_API_KEY" not in env and "A_MARKER" not in env
    assert raw["PATH"] == "/usr/bin:/bin"
