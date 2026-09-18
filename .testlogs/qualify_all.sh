#!/usr/bin/env bash
# Decisive qualification of every red: each FILE run in isolation, once on the
# candidate and once on the pristine upstream base. A red that reproduces on
# the base is pre-existing/environmental; one that only appears on the
# candidate is a merge regression and must be fixed.
LINK=/home/rodrigue/.local/bin/hermes
ORIG=/mnt/usb-ext4/kanban-workspaces/iron-rod/t_c50ee5d3/.venv/bin/hermes
restore() { ln -sfn "$ORIG" "$LINK"; }

FILES=$(sed 's/::.*//' /tmp/all_reds.txt | sort -u)
echo "distinct files: $(echo "$FILES" | wc -l)"
echo

probe() { # $1=worktree $2=label
  W="$1"; LABEL="$2"
  cd "$W" || return
  for f in $FILES; do
    [ -f "$f" ] || { printf "%-6s %-58s ABSENT\n" "$LABEL" "$(basename "$f")"; continue; }
    restore
    out=$(env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_WORKSPACE \
      HERMES_TEST_FILE_RETRIES=0 \
      scripts/run_tests.sh "$f" -q --tb=no 2>&1)
    s=$(printf '%s\n' "$out" | grep -E "^=== Summary:" | head -1 | sed 's/=== Summary: //;s/ (100% complete).*//')
    printf "%-6s %-58s %s\n" "$LABEL" "$(basename "$f")" "$s"
    restore
  done
}

echo "################ CANDIDATE 7944c5fe07 ################"
probe /home/rodrigue/hermes-candidates/t_704ceaa8 CAND
echo
echo "################ UPSTREAM BASE 77ecc72b ################"
probe /home/rodrigue/hermes-candidates/t704base-up BASE
restore
echo
echo "final launcher: $(readlink "$LINK")"
