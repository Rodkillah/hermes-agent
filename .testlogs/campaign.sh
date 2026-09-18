#!/usr/bin/env bash
# Resume the test campaign. NOTE: upstream test_doctor.py self-heals the REAL
# ~/.local/bin/hermes to the running checkout (hermes_cli/doctor_platform.py
# _check_command_installation, should_fix=True, HOME not isolated). The live
# launcher is restored at the end and re-verified.
cd /home/rodrigue/hermes-candidates/t_704ceaa8 || exit 1
L=.testlogs
LINK=/home/rodrigue/.local/bin/hermes
ORIG=/mnt/usb-ext4/kanban-workspaces/iron-rod/t_c50ee5d3/.venv/bin/hermes

run() {
  name="$1"; shift
  echo "########## $name ##########"
  env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACE \
    HERMES_TEST_FILE_RETRIES=0 \
    scripts/run_tests.sh "$@" -q --tb=line > "$L/run_$name.log" 2>&1
  echo "rc=$?"
  grep -E "^=== Summary:|^FAILED|^  ║ FAILED" "$L/run_$name.log" | head -30
  ln -sfn "$ORIG" "$LINK"
  echo
}

run hermes_cli_full   tests/hermes_cli/
run a2a               tests/plugins/test_a2a_hardening.py tests/plugins/test_a2a_plugin.py tests/plugins/test_a2a_phase23.py tests/plugins/test_a2a_schema_registration.py tests/plugins/test_a2a_tools_gate.py tests/tools/test_a2a_reason_roundtrip.py
run auth_vision       tests/agent/test_auxiliary_auth_rung_fallthrough.py tests/agent/test_copilot_native_vision_headers.py tests/agent/test_outbound_stale_vision.py tests/agent/test_vision_routing.py
run vault             tests/agent/test_onepassword_vault_backend.py tests/agent/test_vault_backends.py tests/agent/test_onepassword_secrets.py tests/tui_gateway/test_vault_methods.py
run startup_env       tests/tools/test_startup_latency_regressions.py tests/tools/environments/test_local_path_completion.py tests/agent/test_subprocess_env_guard.py
run gateway           tests/gateway/
run cron_state        tests/cron/ tests/hermes_state/

ln -sfn "$ORIG" "$LINK"
echo ">>> CAMPAIGN DONE; live launcher restored to $(readlink "$LINK")"
