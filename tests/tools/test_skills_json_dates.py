"""YAML timestamps survive skill JSON output without accepting unknown objects."""
import json
from datetime import date, datetime, timezone

import pytest

from tools.skills_tool_plugin import _json


def test_frontmatter_timestamp_serializes_as_iso_text():
    payload = {"date": date(2026, 10, 9), "updated": datetime(2026, 10, 9, 8, tzinfo=timezone.utc)}
    assert json.loads(_json(payload)) == {"date": "2026-10-09", "updated": "2026-10-09T08:00:00+00:00"}


def test_unknown_frontmatter_object_remains_an_error():
    with pytest.raises(TypeError):
        _json({"unknown": object()})
