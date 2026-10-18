"""Apply-only guard while this checkout still carries Iron Rod patches.

Release discovery belongs to Hermes' native source_check/update_channel modules.
The native stable updater checks out the release tag, so applying it would stop
serving committed local guards. Reconcile those separately before lifting this
host-wide mutation gate.
"""
from pathlib import Path

MARKER = ".hermes-release-only"
COMMAND = "Reconcile the official release in an isolated candidate, preserving local patches."
MESSAGE = "Release-only managed checkout: automatic branch updates are disabled. " + COMMAND
_ROOT = Path(__file__).resolve().parent.parent


def release_policy_active(project_root=None):
    return (Path(project_root) if project_root is not None else _ROOT).joinpath(MARKER).is_file()
