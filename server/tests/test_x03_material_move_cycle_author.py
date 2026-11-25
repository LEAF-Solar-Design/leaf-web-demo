"""Array selection moves preserve the root and cannot attach a node under itself."""
from hashlib import sha256
import json

import pytest

from test_integration_tool_service import rig
from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


CASES = [
    ('Edit the selected JSON material: set "/0/mark" to 1, then move "/0/a~1b" to '
     '"/0/a~1b/child/new".', "descendant"),
    ('Patch my material records: set "/0/mark" to 1, then move "" to "/0/a~1b/child".', "root"),
    ('Update the imported JSON records: move "/0/a~1b" to "/0/a~1b2", then move '
     '"/0/items/0" to "/0/items/2".', None),
]


@pytest.mark.parametrize("ask,reason", CASES)
def test_subtree_move(local, ask, reason):
    scope, ledger, materials = local
    original = [{"id": "r", "mark": 0, "a/b": {"child": {"v": 2}}, "items": ["A", "B", "C"]}]
    (_, mid, context), raw = pin_selection(scope, 1, "rows", original, ledger, materials)
    intake = bind_selection({}, *scope, mid, context)
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_array_patch"
    status, result = tool_loader._run_source_in_sandbox(source, "subtree_move.py", intake, {})
    if reason:
        assert status == "tool_error" and reason in str(result).lower()
        assert isinstance(result, str)
    else:
        assert status == "ok", result
        report, overlay = result
        assert overlay is None
        assert report["records"] == [{"id": "r", "mark": 0, "a/b2": {"child": {"v": 2}},
                                     "items": ["B", "C", "A"]}]
        assert report["source_sha256"] == sha256(raw).hexdigest()
    stored = originals.read_content(*scope[:2], materials[mid])
    assert stored == raw and json.loads(stored)["rows"] == original
