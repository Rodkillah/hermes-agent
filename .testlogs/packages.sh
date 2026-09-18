#!/usr/bin/env bash
# Run the remaining targeted test packages SEQUENTIALLY (one at a time) to
# avoid the load-induced flakes already qualified on this host.
cd /home/rodrigue/hermes-candidates/t_704ceaa8 || exit 1
L=.testlogs
export HERMES_TEST_FILE_RETRIES=0
unset HERMES_KANBAN_TASK HERMES_KANBAN_DB HERMES_KANBAN_BOARD HERMES_KANBAN_WORKSPACE

run() {
  name="$1"; shift
  echo "########## $name ##########"
  env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACE \
    HERMES_TEST_FILE_RETRIES=0 \
    scripts/run_tests.sh "$@" -q --tb=line > "$L/pkg_$name.log" 2>&1
  echo "rc=$?"
  grep -E "^=== Summary:|^FAILED|^  ║ FAILED" "$L/pkg_$name.log" | head -30
  echo
}

# Wait for the hermes_cli suite to finish first (same runner, shared caches).
while pgrep -f run_tests_parallel.py >/dev/null; do sleep 20; done
echo ">>> hermes_cli suite finished, starting targeted packages"

run a2a          tests/plugins/test_a2a_hardening.py tests/plugins/test_a2a_plugin.py tests/plugins/test_a2a_phase23.py tests/plugins/test_a2a_schema_registration.py tests/plugins/test_a2a_tools_gate.py tests/tools/test_a2a_reason_roundtrip.py
run auth_vision  tests/agent/test_auxiliary_auth_rung_fallthrough.py tests/agent/test_copilot_native_vision_headers.py tests/agent/test_outbound_stale_vision.py tests/agent/test_vision_routing.py
run vault        tests/agent/test_onepassword_vault_backend.py tests/agent/test_vault_backends.py tests/agent/test_onepassword_secrets.py tests/tui_gateway/test_vault_methods.py
run startup_env  tests/tools/test_startup_latency_regressions.py tests/tools/environments/test_local_path_completion.py tests/agent/test_subprocess_env_guard.py
run cron_state   tests/cron/ tests/hermes_state/
run notify_extra tests/hermes_cli/test_kanban_default_notify_targets.py tests/hermes_cli/test_kanban_notify_authorities.py

echo ">>> ALL TARGETED PACKAGES DONE"
