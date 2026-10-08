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
from datetime import date, datetime, timezone
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


def _stable_version(value):
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if not match or int(match[1]) >= 2000:
        raise ValueError("Unsupported stable semantic version")
    return tuple(int(part) for part in match.groups())


def _newer_release(tag, current_version, current_date):
    if not isinstance(tag, str):
        raise ValueError("Official release tag must be a string")
    if re.fullmatch(r"v?\d{4}\.\d{1,2}\.\d{1,2}", tag):
        released = _release_date(tag)
        # Official stable-releases.md fixes the final CalVer at 0.21.5.
        # A semantic version has no calendar date; never fabricate one.
        if released > date(2026, 9, 24):
            raise ValueError("Dated tag beyond the official legacy release family")
        if _stable_version(current_version) >= (0, 21, 5):
            return False
        return released > _release_date(current_date)
    return _stable_version(tag) > _stable_version(current_version)


def release_status(*, force=False, current_version=None):
    """Check published stable identity; unknown data is an error, never main fallback.

    Historical dated tags compare real calendar dates. Semantic tags compare
    the native installed base version (which maps legacy tags to pyproject).
    No date is manufactured from a semantic version.
    """
    global _cache
    from hermes_cli import __release_date__
    from hermes_cli.version_info import get_version_info
    current = current_version if current_version is not None else get_version_info().base_version
    now = time.monotonic()
    if (not force and _cache and now - _cache[0] < 300
            and _cache[1]["current_version"] == current):
        return dict(_cache[1])
    result = {"install_method": "git", "current_version": current,
              "channel": "release", "behind": None, "update_available": False,
              "can_apply": False, "update_command": COMMAND, "message": MESSAGE}
    try:
        request = urllib.request.Request(RELEASE_API, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "Hermes-release-policy"})
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.load(response)
        if not isinstance(data, dict) or data.get("draft") is not False or data.get("prerelease") is not False:
            raise ValueError("Expected a published stable release")
        published = datetime.fromisoformat(data["published_at"].replace("Z", "+00:00"))
        if published.tzinfo is None or published > datetime.now(timezone.utc):
            raise ValueError("Invalid official publication timestamp")
        tag = data["tag_name"]
        available = _newer_release(tag, current, __release_date__)
        result.update(latest_release=tag, published_at=data["published_at"],
                      release_url="https://github.com/NousResearch/hermes-agent/releases/tag/" + tag,
                      update_available=available, behind=-1 if available else 0,
                      message=("A newer official release is available. " + MESSAGE if available else
                               "Already on the latest official release. " + MESSAGE))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result.update(error="release-check-failed",
                      message=f"Official release check unavailable ({type(exc).__name__}); no branch fallback. " + MESSAGE)
    _cache = (now, dict(result))
    return result
