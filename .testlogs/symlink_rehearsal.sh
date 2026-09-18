#!/usr/bin/env bash
# Symlink repair rehearsal — TEMPORARY tree only. The active launcher
# ~/.local/bin/hermes is NEVER touched by this script.
set -uo pipefail

REHEARSE=$(mktemp -d /tmp/symlink_rehearsal_XXXXXX)
echo "rehearsal root: $REHEARSE"
BIN="$REHEARSE/bin"; mkdir -p "$BIN"

CAND=/home/rodrigue/hermes-candidates/t_704ceaa8
SERVED=/mnt/usb-ext4/hermes-agent-runtime

echo
echo "### case 1: nominal atomic repoint (candidate) ###"
ln -s "$SERVED/.venv/bin/hermes" "$BIN/hermes"
printf "  before : %s\n" "$(readlink "$BIN/hermes")"
# atomic + reversible: create temp link in same dir, then rename(2) over the target
ln -s "$CAND/.venv/bin/hermes" "$BIN/.hermes.new"
mv -Tf "$BIN/.hermes.new" "$BIN/hermes"
printf "  after  : %s\n" "$(readlink "$BIN/hermes")"
printf "  exists : %s\n" "$(test -x "$BIN/hermes" && echo yes || echo no)"

echo
echo "### case 2: rollback (restore served) ###"
ln -s "$SERVED/.venv/bin/hermes" "$BIN/.hermes.new"
mv -Tf "$BIN/.hermes.new" "$BIN/hermes"
printf "  restored: %s\n" "$(readlink "$BIN/hermes")"

echo
echo "### case 3: broken/missing target detected before use ###"
ln -s "$REHEARSE/nonexistent/runtime" "$BIN/broken_hermes"
if [ -e "$BIN/broken_hermes" ]; then
  echo "  FAIL: broken link reported as existing"
else
  echo "  OK: dangling link detected (target absent) -> refuse to commit the swap"
fi
echo "  readlink still shows intent: $(readlink "$BIN/broken_hermes")"

echo
echo "### case 4: wrong-target detection (points at stale runtime) ###"
ln -s "$SERVED/.venv/bin/hermes" "$BIN/wrong_hermes"
tgt=$(readlink -f "$BIN/wrong_hermes")
case "$tgt" in
  "$CAND"/*) echo "  would accept";;
  *) echo "  OK: refused — resolves to $tgt, not the validated candidate";;
esac

echo
echo "### proof the ACTIVE launcher was not touched ###"
ls -la /home/rodrigue/.local/bin/hermes
echo "  active resolves to: $(readlink -f /home/rodrigue/.local/bin/hermes)"

echo
echo "rehearsal artifacts left at: $REHEARSE"
