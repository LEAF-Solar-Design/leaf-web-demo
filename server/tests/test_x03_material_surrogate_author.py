"""Accept escaped surrogate pairs and reject lone halves throughout a material patch."""
from hashlib import sha256

import pytest

from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_integration_tool_service import rig
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


PAIR = r"\ud83d\ude80"
ROCKET = "\U0001f680"
CASES = [
    (r'Edit the selected JSON material: set "/0/mark" to 1, then set "/0/data" to "\ud800".',
     r"\ud800", {"data": ROCKET}),
    (r'Patch my material records: set "/0/mark" to 1, then set "/0/data" to [{"\udfff":"tag"}].',
     r"\udfff", {"data": [{ROCKET: "tag"}]}),
    (r'Update the imported JSON records: set "/0/mark" to 1, then copy "/0/mark" to "/0/\udbff".',
     r"\udbff", {ROCKET: 1}),
]


@pytest.mark.parametrize("ask,lone,edits", CASES)
def test_surrogate_patch(local, ask, lone, edits):
    scope, ledger, materials = local
    original = {"id": "east", "mark": 0, "data": "", ROCKET: ROCKET}
    raw = br'{"items":[{"id":"east","mark":0,"data":"","\ud83d\ude80":"\ud83d\ude80"}]}'
    (_, mid, context), _ = pin_selection(scope, 1, "items", [original], ledger, materials, raw=raw)
    intake = bind_selection({}, *scope, mid, context)
    for phrase, valid in ((ask, False), (ask.replace(lone, PAIR), True)):
        package, source, _ = tools_fallback.author_tool(phrase)
        assert package["engine_op"] == "material_array_patch"
        status, result = tool_loader._run_source_in_sandbox(source, "surrogate_patch.py", intake, {})
        if valid:
            assert status == "ok", result
            assert result[0]["records"] == [{**original, "mark": 1, **edits}]
            assert result[0]["source_sha256"] == sha256(raw).hexdigest()
        else:
            assert status == "tool_error" and "unpaired surrogate" in str(result).lower()
        assert intake["checker_material_selection"]["record_values"] == [original]
        assert originals.read_content(*scope[:2], materials[mid]) == raw

    # Both the property name and its value in the raw control use the escaped pair.
    for needle in (b',"' + PAIR.encode() + b'":', b'":"' + PAIR.encode() + b'"'):
        bad_raw = raw.replace(needle, needle.replace(PAIR.encode(), lone.encode()), 1)
        assert bad_raw != raw
        with pytest.raises(originals.InvalidReference, match="(?i)unpaired surrogate"):
            originals.prepare_and_store(*scope[:2], "bad.json", "application/json", bad_raw)
