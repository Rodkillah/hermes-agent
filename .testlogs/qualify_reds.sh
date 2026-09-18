#!/usr/bin/env bash
# Qualify the two remaining reds against the pristine bases.
LINK=/home/rodrigue/.local/bin/hermes
ORIG=/mnt/usb-ext4/kanban-workspaces/iron-rod/t_c50ee5d3/.venv/bin/hermes
restore() { ln -sfn "$ORIG" "$LINK"; }

for W in /home/rodrigue/hermes-candidates/t704base-up \
         /home/rodrigue/hermes-candidates/t704base-589; do
  [ -d "$W" ] || continue
  cd "$W" || continue
  echo "############ $(basename "$W") ############"
  restore
  echo "--- doctor_structural_corruption ---"
  env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACE \
    HERMES_TEST_FILE_RETRIES=0 \
    scripts/run_tests.sh tests/hermes_cli/test_doctor_structural_corruption.py -q --tb=no 2>&1 \
    | grep -E "^=== Summary:|FAILED"
  restore
  echo "--- kanban_notify ---"
  env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACE \
    HERMES_TEST_FILE_RETRIES=0 \
    scripts/run_tests.sh tests/hermes_cli/test_kanban_notify.py -q --tb=no 2>&1 \
    | grep -E "^=== Summary:|FAILED"
  restore
  echo
done
restore
echo "final launcher: $(readlink "$LINK")"
