#!/usr/bin/env bash
# Qualify the shutdown-memory flake against the pristine upstream base.
cd /home/rodrigue/hermes-candidates/t704base-up
for i in 1 2 3; do
  echo "===== upstream 77ecc attempt $i ====="
  env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACE \
    HERMES_TEST_FILE_RETRIES=0 \
    scripts/run_tests.sh tests/hermes_cli/test_cli_shutdown_memory_messages.py -q --tb=line 2>&1 \
    | grep -E "Summary:|FAILED" || true
done
