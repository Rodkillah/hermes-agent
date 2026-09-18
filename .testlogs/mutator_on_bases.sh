#!/usr/bin/env bash
# Does the pristine upstream base also repoint the live launcher?
LINK=/home/rodrigue/.local/bin/hermes
ORIG=/mnt/usb-ext4/kanban-workspaces/iron-rod/t_c50ee5d3/.venv/bin/hermes
restore() { ln -sfn "$ORIG" "$LINK"; }

for W in /home/rodrigue/hermes-candidates/t704base-up \
         /home/rodrigue/hermes-candidates/t704base-589 \
         /home/rodrigue/hermes-candidates/t704base-18e7; do
  [ -d "$W" ] || { echo "SKIP $W"; continue; }
  cd "$W" || continue
  restore
  before=$(readlink "$LINK")
  env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACE \
    HERMES_TEST_FILE_RETRIES=0 \
    scripts/run_tests.sh tests/hermes_cli/test_doctor.py -q --tb=no > /tmp/probe_base.log 2>&1
  rc=$?
  after=$(readlink "$LINK")
  echo "=== $(basename "$W") (rc=$rc) ==="
  if [ "$before" != "$after" ]; then
    echo "  MUTATES live launcher:"
    echo "    $before"
    echo " -> $after"
  else
    echo "  no mutation"
  fi
done
restore
echo "final: $(readlink "$LINK")"
