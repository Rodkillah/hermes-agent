"""A scoped policy read may itself log unresolved config references."""
import io
import logging

import pytest

from agent import redact, secret_scope
from hermes_cli import config
from hermes_cli.config_read_errors import get_active_config_parse_failure
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.mark.parametrize("enabled", [True, False])
def test_scoped_redaction_policy_warning_does_not_reenter_config(tmp_path, monkeypatch, enabled):
    root = tmp_path / ".hermes"
    home = root / "profiles" / "worker"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (home / "config.yaml").write_text(
        f"security:\n  redact_secrets: {str(enabled).lower()}\n"
        "a2a_agents:\n  synthetic:\n    token: ${env:SYNTHETIC_UNSET_TOKEN}\n",
        encoding="utf-8",
    )
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setFormatter(redact.RedactingFormatter("%(message)s"))
    logger = logging.getLogger("hermes_cli.config")
    logger.addHandler(handler)
    monkeypatch.setattr(logger, "level", logging.WARNING)
    monkeypatch.setattr(redact, "_REDACT_ENABLED_BY_HOME", {})
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    home_token = set_hermes_home_override(str(home))
    secret_token = secret_scope.set_secret_scope({}, profile_home=str(home))
    try:
        assert redact._redact_enabled() is enabled
        assert sink.getvalue().count("SYNTHETIC_UNSET_TOKEN is not set") == 1
        assert get_active_config_parse_failure() is None
        assert config.load_config()["a2a_agents"]["synthetic"]["token"] == "${env:SYNTHETIC_UNSET_TOKEN}"
    finally:
        secret_scope.reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)
        logger.removeHandler(handler)
        handler.close()
