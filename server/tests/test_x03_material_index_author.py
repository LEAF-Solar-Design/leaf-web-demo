"""Interpret a pointer token according to the actual JSON container."""
import json

import pytest

from test_integration_tool_service import rig
from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


@pytest.mark.parametrize("valid,invalid,wanted", [
    ('Edit the selected JSON material: set "/0/obj/01" to "edited".',
     'Edit the selected JSON material: set "/0/obj/01" to "edited", then set "/0/arr/01" to "bad".',
     {"obj": {"01": "edited", "-": "dash"}, "arr": ["a", "b"]}),
    ('Patch my material records: set "/0/obj/-" to "ok".',
     'Patch my material records: set "/0/obj/-" to "ok", then test "/0/arr/-" equals "b".',
     {"obj": {"01": "zero-one", "-": "ok"}, "arr": ["a", "b"]}),
    ('Update the imported JSON records: copy "/0/obj/-" to "/0/arr/-", then test "/0/arr/2" equals "dash".',
     'Update the imported JSON records: copy "/0/obj/-" to "/0/arr/-", then copy "/0/arr/+1" to "/0/obj/-".',
     {"obj": {"01": "zero-one", "-": "dash"}, "arr": ["a", "b", "dash"]}),
])
def test_container_specific_index(local, valid, invalid, wanted):
    rows = [{"id": "row", "obj": {"01": "zero-one", "-": "dash"}, "arr": ["a", "b"]}]
    scope, ledger, materials = local
    (_, mid, context), raw = pin_selection(scope, 1, "items", rows, ledger, materials)
    intake = bind_selection({}, *scope, mid, context)
    for ask, expected in ((invalid, False), (valid, True)):
        package, source, _ = tools_fallback.author_tool(ask)
        assert package["engine_op"] == "material_array_patch"
        status, result = tool_loader._run_source_in_sandbox(source, "container_index.py", intake, {})
        if expected:
            assert status == "ok", result
            assert result[0]["records"] == [{"id": "row", **wanted}]
        else:
            assert status == "tool_error" and isinstance(result, str)
        stored = originals.read_content(*scope[:2], materials[mid])
        assert stored == raw and json.loads(stored)["items"] == rows
