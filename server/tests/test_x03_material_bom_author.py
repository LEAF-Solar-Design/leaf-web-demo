"""Accept one optional UTF-8 signature at byte zero and preserve U+FEFF in keys."""
from codecs import BOM_UTF8

import pytest

from test_integration_tool_service import rig
from test_x03_material_revision_author import local
from test_x03_material_trailing_json_author import assert_import_and_patch


CASES = [
    ('Edit the selected JSON material: set "/0/tag" to "signed".',
     BOM_UTF8, "", {"tag": "signed"}),
    (r'Patch my material records: copy "/0/\ufefftag" to "/0/tag".',
     b"", "\ufeff", {"tag": "invisible"}),
    (r'Update the imported JSON records: set "/0/t\ufeffag" to "inside".',
     BOM_UTF8, "", {"t\ufeffag": "inside"}),
]


@pytest.mark.parametrize("ask,prefix,key_prefix,changed", CASES)
def test_bom_boundary(local, tmp_path, monkeypatch, ask, prefix, key_prefix, changed):
    raw = (b'{"' + key_prefix.encode("utf-8") + b'items":[{"id":"east","tag":"plain","'
           + BOM_UTF8 + b'tag":"invisible","t' + BOM_UTF8 + b'ag":"middle"}]}')
    before = {"id": "east", "tag": "plain", "\ufefftag": "invisible", "t\ufeffag": "middle"}
    assert_import_and_patch(local, tmp_path, monkeypatch, ask, key_prefix + "items",
                            before, {**before, **changed}, prefix + raw, BOM_UTF8 * 2 + raw)
