#!/usr/bin/env bash
# Qualify the load-induced flake in test_cli_shutdown_memory_messages.py.
cd /home/rodrigue/hermes-candidates/t_704ceaa8
for i in 1 2 3; do
  echo "===== attempt $i ====="
  env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACE \
    HERMES_TEST_FILE_RETRIES=0 \
    scripts/run_tests.sh tests/hermes_cli/test_cli_shutdown_memory_messages.py -q --tb=line 2>&1 \
    | grep -E "Summary:|FAILED|✗" || true
done
