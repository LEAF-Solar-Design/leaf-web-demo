"""Exercise ordinary patch quotas at their boundary on a disposable material."""
from copy import deepcopy
from hashlib import sha256

import pytest

from test_integration_tool_service import rig
from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


@pytest.mark.parametrize("kind", ["operation_count", "source_depth", "destination_depth"])
def test_preflight_bounded_patch(local, kind):
    scope, ledger, materials = local
    tree = {"v": {"extra": 7}}
    for _ in range(13):
        tree = {"x": tree}
    rows = [{"id": "row", "mark": 0, "tree": tree, "sample": 11}]
    (_, mid, context), raw = pin_selection(scope, 1, "rows", rows, ledger, materials)
    intake = bind_selection({}, *scope, mid, context)
    pointer = "/0/tree" + "/x"*13 + "/v"
    assert pointer.count("/") == 16
    expected = deepcopy(rows)
    if kind == "operation_count":
        steps = ['set "/0/mark" to 1'] * 100
        valid = "Edit the selected JSON material: " + ", then ".join(steps) + "."
        invalid = valid[:-1] + ', then set "/0/mark" to 1.'
        expected[0]["mark"] = 1
    elif kind == "source_depth":
        valid = f'Patch my material records: test "{pointer}" equals {{"extra":7}}, then set "/0/mark" to 2.'
        invalid = f'Patch my material records: test "{pointer}/extra" equals 7, then set "/0/mark" to 2.'
        expected[0]["mark"] = 2
    else:
        valid = f'Update the imported JSON records: set "/0/mark" to 3, then copy "/0/sample" to "{pointer}".'
        invalid = valid.replace(pointer, pointer + "/extra")
        expected[0]["mark"] = 3
        branch = expected[0]["tree"]
        for _ in range(13):
            branch = branch["x"]
        branch["v"] = 11
    for ask, within_limit in ((valid, True), (invalid, False)):
        package, source, _ = tools_fallback.author_tool(ask)
        assert package["engine_op"] == "material_array_patch"
        status, result = tool_loader._run_source_in_sandbox(source, "bounded_material.py", intake, {})
        if within_limit:
            assert status == "ok", result
            assert result[0]["records"] == expected
            assert result[0]["source_sha256"] == sha256(raw).hexdigest()
        else:
            assert status == "tool_error" and "budget" in str(result).lower()
        assert originals.read_content(*scope[:2], materials[mid]) == raw


def test_long_material_request_refused_before_reading_input():
    ask = "Patch my material records: " + ", then ".join(['set "/0/mark" to 1'] * 180)
    assert len(ask) > 4000
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_array_patch"
    status, error = tool_loader._run_source_in_sandbox(source, "long_material.py", {}, {})
    assert status == "tool_error" and "description budget" in str(error)
