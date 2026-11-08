"""Exercise timestamp helpers without loading the Qt UI."""
import ast
import datetime
from pathlib import Path

import pytest


@pytest.fixture(params=[
    ("calendar_service.py", "_parse_iso", datetime.datetime),
    ("ui/pages/home.py", "parse_when", datetime.datetime),
    ("ui/pages/transcript_view.py", "when_text", datetime),
])
def parse_timestamp(request):
    path, name, datetime_binding = request.param
    source = Path(__file__).resolve().parents[1] / path
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = {"datetime": datetime_binding}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[name]


def test_utc_z_matches_explicit_offset(parse_timestamp):
    expected = parse_timestamp("2026-11-08T14:02:00+00:00")
    assert expected is not None and expected != ""
    assert parse_timestamp("2026-11-08T14:02:00Z") == expected


@pytest.mark.parametrize("value", ["", None, "not-a-timestamp"])
def test_invalid_timestamps_still_have_no_display(parse_timestamp, value):
    assert parse_timestamp(value) in (None, "")
