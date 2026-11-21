"""The empty pointer addresses the entire selected value after each edit."""
from hashlib import sha256

import pytest

from test_integration_tool_service import rig
from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


CASES = [
    ('Edit the selected JSON material: set "" to {"":3,"name":"A"}, then set "/" to 4.',
     {"": 4, "name": "A"}),
    ('Patch my material records: replace "" with [3,{"tag":5}], then set "/1/tag" to 6.',
     [3, {"tag": 6}]),
    ('Update the imported JSON records: replace "" with false, then test "" equals false.',
     False),
]


@pytest.mark.parametrize("ask,expected", CASES)
def test_replace_selection_root(local, ask, expected):
    scope, ledger, materials = local
    original = [{"id": "east", "name": "unit"}]
    (_, mid, context), raw = pin_selection(scope, 1, "items", original, ledger, materials)
    intake = bind_selection({}, *scope, mid, context)
    for guarded in (True, False):
        prompt = ask[:-1] + ', then test "" equals "wrong".' if guarded else ask
        package, source, _ = tools_fallback.author_tool(prompt)
        assert package["engine_op"] == "material_array_patch"
        status, result = tool_loader._run_source_in_sandbox(source, "replace_root.py", intake, {})
        if guarded:
            assert status == "tool_error" and "Material test failed" in str(result)
        else:
            assert status == "ok", result
            report, overlay = result
            assert type(report["records"]) is type(expected) and report["records"] == expected
            assert overlay is None and report["records_pointer"] == "/items"
            assert report["source_sha256"] == sha256(raw).hexdigest()
        assert intake["checker_material_selection"]["record_values"] == original
        assert originals.read_content(*scope[:2], materials[mid]) == raw
