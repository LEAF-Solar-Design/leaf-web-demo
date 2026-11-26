"""Import escaped string controls while rejecting raw C0 bytes."""
import pytest

from test_integration_tool_service import rig
from test_x03_material_revision_author import local
from test_x03_material_trailing_json_author import assert_import_and_patch


CASES = [
    ('Edit the selected JSON material: copy "/0/reading" to "/0/tag".',
     br"\u0000", b"\x00", "\x00", True),
    ('Patch my material records: set "/0/tag" to "reviewed".',
     br"\t", b"\t", "\t", False),
    ('Update the imported JSON records: test "/0/tag" equals "draft", then copy "/0/reading" to "/0/tag".',
     br"\u0000\t", b"\x00\t", "\x00\t", True),
]


@pytest.mark.parametrize("ask,escaped,raw,decoded,copy", CASES)
def test_controls_inside_strings(local, tmp_path, monkeypatch, ask, escaped, raw, decoded, copy):
    prefix = b'{"items":[{"id":"west","tag":"draft","reading":"left'
    suffix = b'right"}]}'
    before = {"id": "west", "tag": "draft", "reading": "left" + decoded + "right"}
    after = {**before, "tag": before["reading"] if copy else "reviewed"}
    assert_import_and_patch(local, tmp_path, monkeypatch, ask, "items", before, after,
                            prefix + escaped + suffix, prefix + raw + suffix)
