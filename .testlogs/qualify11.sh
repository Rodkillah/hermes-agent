#!/usr/bin/env bash
# Qualify the 11 hermes_cli reds: run each in ISOLATION (no concurrent load)
# on the candidate and on the pristine upstream base, and compare verdicts.
LINK=/home/rodrigue/.local/bin/hermes
ORIG=/mnt/usb-ext4/kanban-workspaces/iron-rod/t_c50ee5d3/.venv/bin/hermes
restore() { ln -sfn "$ORIG" "$LINK"; }

FILES="
tests/hermes_cli/test_dashboard_auth_gate.py
tests/hermes_cli/test_doctor_structural_corruption.py
tests/hermes_cli/test_kanban_notify.py
tests/hermes_cli/test_profiles_sidebar_cache.py
tests/hermes_cli/test_relay_shared_metrics.py
tests/hermes_cli/test_serve_skill_maintenance.py
tests/hermes_cli/test_sessions_repair_check_only_exit.py
tests/hermes_cli/test_user_providers_model_switch.py
"

probe() { # $1=worktree $2=label
  W="$1"; LABEL="$2"
  cd "$W" || return
  for f in $FILES; do
    restore
    out=$(env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACE \
      HERMES_TEST_FILE_RETRIES=0 \
      scripts/run_tests.sh "$f" -q --tb=no 2>&1)
    line=$(printf '%s\n' "$out" | grep -E "^=== Summary:" | head -1)
    rc=$?
    printf "%-8s %-52s %s\n" "$LABEL" "$(basename "$f")" "$line"
    restore
  done
}

echo "########## CANDIDATE 7944c5fe07 ##########"
probe /home/rodrigue/hermes-candidates/t_704ceaa8 CAND
echo
echo "########## UPSTREAM BASE 77ecc72b ##########"
probe /home/rodrigue/hermes-candidates/t704base-up BASE
restore
echo
echo "final launcher: $(readlink "$LINK")"
