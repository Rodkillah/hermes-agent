#!/usr/bin/env bash
# Build the SHA-256 manifest of the external backup.
set -uo pipefail
BK=$(cat /tmp/bk_path.txt | cut -d= -f2)
cd "$BK" || exit 1

{
  echo "hermes-reconcile backup manifest (task t_704ceaa8)"
  echo "host=$(hostname)"
  echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "dir=$BK"
  echo
  echo "## Candidate identity"
  echo "branch=ironrod/t_704ceaa8-hermes-reconcile"
  echo "candidate_sha=7944c5fe073233d85052117df49607cb66d9478d"
  echo
  echo "## Lineages preserved (all verified ancestors of candidate)"
  echo "upstream_pin_d177=d177b119e9c56c9ddc0b7379ffce52341ec06584"
  echo "upstream_head_77ecc=77ecc72bcdd5da0163cca21c8af0e95b26ba3426"
  echo "runtime_18e7=18e7e4885178e93950ba74b7b3d35cd80f5aa0fe"
  echo "launcher_589=58932c3df4209e72b2e62683b93d76b4025f61fd"
  echo "merge_base=814bff51a0c14e00a1270428d9d7507b72e526ce"
  echo
  echo "## Remote refs holding the lineages (private fork \`rod\`)"
  echo "rod/ironrod-backups/t_704ceaa8-candidate-20260918=7944c5fe073233d85052117df49607cb66d9478d"
  echo "rod/ironrod-backups/t_704ceaa8-runtime-18e7-rollback=18e7e4885178e93950ba74b7b3d35cd80f5aa0fe"
  echo "rod/ironrod-backups/t_704ceaa8-launcher-589=58932c3df4209e72b2e62683b93d76b4025f61fd"
  echo "rod/ironrod-backups/t_704ceaa8-upstream-77ecc-20260918=77ecc72bcdd5da0163cca21c8af0e95b26ba3426"
  echo "rod/ironrod-backups/t_704ceaa8-upstream-d177-20260918=d177b119e9c56c9ddc0b7379ffce52341ec06584"
  echo "rod/ironrod-backups/t_704ceaa8-upstream-f9e47-20260918=f9e47df22ec5e7dd19e7a9282103a21e91f2ad62"
  echo "rod/ironrod/t_704ceaa8-hermes-reconcile=7944c5fe073233d85052117df49607cb66d9478d"
  echo
  echo "## Untouched (verified after push)"
  echo "rod/main=18e7e4885178e93950ba74b7b3d35cd80f5aa0fe (unchanged)"
  echo "rod/ironrod-local-patches=18e7e4885178e93950ba74b7b3d35cd80f5aa0fe (unchanged)"
  echo "rod/ironrod/t_c50ee5d3-live-rebuild=58932c3df4209e72b2e62683b93d76b4025f61fd (unchanged)"
  echo
  echo "## Git bundle"
  echo "git_bundle=git/hermes-reconcile-t_704ceaa8.bundle"
  echo "git_bundle_list_heads:"
  git bundle list-heads git/hermes-reconcile-t_704ceaa8.bundle
  echo
  echo "## SQLite snapshots (VACUUM INTO, read-only source, never .backup)"
  echo "sqlite/state.db               <- /home/rodrigue/.hermes/state.db"
  echo "sqlite/iron-rod-kanban.db     <- /home/rodrigue/.hermes/kanban/boards/iron-rod/kanban.db"
  echo "sqlite/default-kanban.db      <- /home/rodrigue/.hermes/kanban.db"
  echo "sqlite/cron-executions.db     <- /home/rodrigue/.hermes/cron/executions.db"
  echo "Excluded by design: auth.json, .env, tokens, credentials, secrets."
  echo
  echo "## Config definitions captured (bounded, no secret values)"
  echo "config/systemd/hermes-gateway.service"
  echo "config/systemd/hermes-gateway-{carter,colbert,forge,julien,plume,ruth}.service"
  echo "config/launcher-symlink.txt"
  echo
  echo "## SHA-256"
} > MANIFEST.txt

find . -type f ! -name MANIFEST.txt ! -name '*.sha256' -print0 \
  | sort -z | xargs -0 sha256sum >> MANIFEST.txt

echo "--- MANIFEST written ---"
tail -20 MANIFEST.txt
chmod 600 MANIFEST.txt
chmod -R go-rwx "$BK"
