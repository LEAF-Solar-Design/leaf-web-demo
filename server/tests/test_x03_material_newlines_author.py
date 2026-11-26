"""Pin original JSON bytes while exposing their decoded newline values to authored tools."""
from hashlib import sha256
import json

import pytest

from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_integration_tool_service import rig
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


CASES = [
    ('Patch my material records: copy "/0/note" to "/0/preview".', "preview"),
    ('Edit the selected JSON material: move "/0/note" to "/0/review".', "review"),
    (r'Update the imported JSON records: test "/0/note" equals "line 1\nline 2\r\nline 3\\n", then set "/0/checked" to true.', "checked"),
]


@pytest.mark.parametrize("ask,destination", CASES)
def test_newline_identity(local, ask, destination):
    scope, ledger, materials = local
    before = {"id": "east", "note": "line 1\nline 2\r\nline 3\\n", "checked": False}
    text = json.dumps({"items": [before]}, indent=2)
    encodings = [text.encode(), text.replace("\n", "\r\n").encode(),
                 text.replace(r"\r\n", r"\u000d\u000a").encode()]
    assert len(set(encodings)) == 3
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_array_patch"
    receipts = []
    for version, raw in enumerate(encodings, start=1):
        (_, mid, context), _ = pin_selection(scope, version, "items", [before], ledger, materials, raw=raw)
        intake = bind_selection({}, *scope, mid, context)
        status, payload = tool_loader._run_source_in_sandbox(source, "newlines.py", intake, {})
        assert status == "ok", payload
        result, _ = payload
        expected = dict(before)
        if destination == "review":
            expected[destination] = expected.pop("note")
        else:
            expected[destination] = True if destination == "checked" else before["note"]
        assert result["records"] == [expected]
        assert originals.read_content(*scope[:2], materials[mid]) == raw
        receipts.append(result["source_sha256"])
    assert receipts == [sha256(raw).hexdigest() for raw in encodings]
    assert len(set(receipts)) == 3
