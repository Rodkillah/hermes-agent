#!/usr/bin/env bash
# Detail the two candidate-only failures. Runs only the listed node ids.
cd /home/rodrigue/hermes-candidates/t_704ceaa8 || exit 1
LINK=/home/rodrigue/.local/bin/hermes
ORIG=/mnt/usb-ext4/kanban-workspaces/iron-rod/t_c50ee5d3/.venv/bin/hermes
restore() { ln -sfn "$ORIG" "$LINK"; }

restore
echo "########## test_multiplex_busy_input_mode (candidate) ##########"
env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACE \
  HERMES_TEST_FILE_RETRIES=0 \
  scripts/run_tests.sh tests/gateway/test_multiplex_busy_input_mode.py -q --tb=short 2>&1 | tail -60
restore
echo
echo "########## test_kanban_notifier (candidate) ##########"
env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACE \
  HERMES_TEST_FILE_RETRIES=0 \
  scripts/run_tests.sh tests/gateway/test_kanban_notifier.py -q --tb=short 2>&1 | grep -A40 "FAILURES\|Short test summary" | head -60
restore
echo "launcher: $(readlink "$LINK")"
