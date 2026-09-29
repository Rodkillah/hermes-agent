"""Host-wide release-only policy for a locally maintained Hermes checkout.

The deployment marker is shared by every profile. Checking never fetches git or
creates snapshots; applying a release requires an isolated patch reconciliation.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
MARKER = ".hermes-release-only"
RELEASE_API = "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest"
COMMAND = "Reconcile the official release in an isolated candidate, preserving local patches."
MESSAGE = "Release-only managed checkout: automatic branch updates are disabled. " + COMMAND
_cache = None


def release_policy_active(project_root=None):
    return os.path.lexists(Path(project_root or _ROOT) / MARKER)


def _release_date(value):
    match = re.fullmatch(r"v?(\d{4})\.(\d{1,2})\.(\d{1,2})", value)
    if not match:
        raise ValueError("Unsupported official release tag")
    return date(*(int(part) for part in match.groups()))


def release_status(*, force=False):
    """Stable-release status; network/schema failure never falls back to main."""
    global _cache
    from hermes_cli import __version__, __release_date__
    now = time.monotonic()
    if not force and _cache and now - _cache[0] < 300:
        return dict(_cache[1])
    result = {"install_method": "git", "current_version": __version__,
              "channel": "release", "behind": None, "update_available": False,
              "can_apply": False, "update_command": COMMAND, "message": MESSAGE}
    try:
        request = urllib.request.Request(RELEASE_API, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "Hermes-release-policy"})
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.load(response)
        if data.get("draft") is not False or data.get("prerelease") is not False:
            raise ValueError("Expected a published stable release")
        tag = data["tag_name"]
        available = _release_date(tag) > _release_date(__release_date__)
        result.update(latest_release=tag,
                      release_url="https://github.com/NousResearch/hermes-agent/releases/tag/" + tag,
                      update_available=available, behind=-1 if available else 0,
                      message=("A newer official release is available. " + MESSAGE if available else
                               "Already on the latest official release. " + MESSAGE))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result["message"] = "Official release check unavailable; no branch fallback. " + MESSAGE
    _cache = (now, dict(result))
    return result
