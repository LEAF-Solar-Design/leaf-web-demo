"""An identical move looks up its source without mutating the selected records."""
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
    ('Edit the selected JSON material: move "" to "".', "/0/absent"),
    ('Patch my material records: move "/0/id" to "/0/id".', "/0/missing"),
    ('Update the imported JSON records: move "/1" to "/1".', "/2"),
]


@pytest.mark.parametrize("ask,missing", CASES)
def test_identical_move(local, ask, missing):
    scope, ledger, materials = local
    rows = [{"id": "B", "mark": 0}, {"id": "A", "mark": 1}]
    (_, mid, context), raw = pin_selection(scope, 1, "records", rows, ledger, materials)
    intake = bind_selection({}, *scope, mid, context)
    def run(prompt):
        package, source, _ = tools_fallback.author_tool(prompt)
        assert package["engine_op"] == "material_array_patch"
        return tool_loader._run_source_in_sandbox(source, "material_move.py", intake, {})

    status, result = run(ask)
    assert status == "ok", result
    report, overlay = result
    assert overlay is None and report["records"] == json.loads(raw)["records"]
    assert report["source_sha256"] == sha256(raw).hexdigest()
    bad = f'Edit the selected JSON material: set "/0/mark" to 9, then move "{missing}" to "{missing}".'
    status, error = run(bad)
    assert status == "tool_error" and "absent" in str(error)
    assert intake["checker_material_selection"]["record_values"] == rows
    assert originals.read_content(*scope[:2], materials[mid]) == raw
