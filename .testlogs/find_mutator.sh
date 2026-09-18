#!/usr/bin/env bash
# Identify which test file mutates the REAL ~/.local/bin/hermes.
# The link is restored after each probe; the mutation is the evidence.
LINK=/home/rodrigue/.local/bin/hermes
ORIG=/mnt/usb-ext4/kanban-workspaces/iron-rod/t_c50ee5d3/.venv/bin/hermes
WORK=$1   # worktree to test
cd "$WORK" || exit 1

restore() { ln -sfn "$ORIG" "$LINK"; }
restore

suspects="
tests/hermes_cli/test_ensure_acp_launcher.py
tests/hermes_cli/test_linux_launcher_management.py
tests/hermes_cli/test_linux_desktop_entry.py
tests/hermes_cli/test_gui_command.py
tests/hermes_cli/test_doctor_command_install.py
tests/scripts/install/test_install_sh_acp_launcher.py
"

for f in $suspects; do
  [ -f "$f" ] || { echo "SKIP (absent): $f"; continue; }
  restore
  before=$(readlink "$LINK")
  env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACE \
    HERMES_TEST_FILE_RETRIES=0 \
    scripts/run_tests.sh "$f" -q --tb=no > /tmp/probe_test.log 2>&1
  rc=$?
  after=$(readlink "$LINK")
  if [ "$before" != "$after" ]; then
    echo ">>> MUTATOR: $f  (rc=$rc)"
    echo "      before: $before"
    echo "      after : $after"
  else
    echo "    clean : $f  (rc=$rc)"
  fi
done
restore
echo "final: $(readlink "$LINK")"
