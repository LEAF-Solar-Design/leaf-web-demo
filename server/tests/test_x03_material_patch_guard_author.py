"""Test operations examine the working selection in order and fail atomically."""
from hashlib import sha256
import json

import pytest

from test_integration_tool_service import rig
from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


@pytest.mark.parametrize("bad,control,expected", [
    ('Edit the selected JSON material: test "/0/id" equals "B", then set "/0/count" to 5.',
     '"B"', '"A"'),
    ('Patch my material records: set "/0/count" to 5, then test "/0/count" equals 1.',
     'equals 1', 'equals 5.0'),
    ('Update the imported JSON records: set "/0/count" to 5, then test "/0/a~1b" equals {"flags":[1]}.',
     '[1]', '[true]'),
])
def test_guard_and_control(local, bad, control, expected):
    scope, ledger, materials = local
    original = [{"id": "A", "count": 1, "a/b": {"flags": [True]}}]
    (_, mid, context), raw = pin_selection(scope, 1, "rows", original, ledger, materials)
    digest = sha256(raw).hexdigest()
    intake = bind_selection({}, *scope, mid, context)
    for phrase, success in [(bad, False), (bad.replace(control, expected), True)]:
        package, source, _ = tools_fallback.author_tool(phrase)
        assert package["engine_op"] == "material_array_patch"
        status, output = tool_loader._run_source_in_sandbox(source, "guarded_material.py", intake, {})
        if success:
            assert status == "ok", output
            report, overlay = output
            assert overlay is None and report["source_sha256"] == digest
            assert report["records"] == [{"id": "A", "count": 5, "a/b": {"flags": [True]}}]
        else:
            assert status == "tool_error" and "test failed" in str(output).lower()
            assert "records" not in output
        stored = originals.read_content(*scope[:2], materials[mid])
        assert sha256(stored).hexdigest() == digest and json.loads(stored)["rows"] == original
