"""Imported material must contain JSON numbers before an authored edit can run."""
import json

import pytest

from test_integration_tool_service import rig
from test_x03_material_revision_author import local
from test_x03_material_trailing_json_author import assert_import_and_patch


CASES = [
    ('Edit the selected JSON material: set "/0/tag" to "verified".', b"NaN", "verified"),
    ('Patch my material records: copy "/0/region" to "/0/tag".', b"Infinity", "east"),
    ('Update the imported JSON records: test "/0/tag" equals "draft", then set "/0/tag" to "ready".',
     b"-Infinity", "ready"),
]


@pytest.mark.parametrize("ask,token,tag", CASES)
def test_import_requires_json_numbers(local, tmp_path, monkeypatch, ask, token, tag):
    # A quoted spelling is a valid string. Only the unquoted number changes.
    row = {"id": "east-roof", "region": "east", "tag": "draft",
           "sample": {"reading": -2.75, "label": token.decode("ascii")}}
    finite = json.dumps({"items": [row]}).encode()
    invalid = finite.replace(b"-2.75", token)
    assert_import_and_patch(local, tmp_path, monkeypatch, ask, "items", row,
                            {**row, "tag": tag}, finite, invalid)
