"""Check nested material copies against independently reconstructed JSON trees."""
from copy import deepcopy
import json

import pytest

from test_integration_tool_service import rig
from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


@pytest.mark.parametrize("ask,kind", [
    ('Edit the selected JSON material: copy "/0/template" to "/1/config", then copy '
     '"/0/template" to "/2/config", then set "/1/config/limits/0/value" to 42.', "object"),
    ('Patch my material records: copy "/0/parts" to "/1/parts", then copy "/1/parts" '
     'to "/2/parts", then set "/2/parts/1/names/0" to "south".', "array"),
    ('Update the imported JSON records: copy "/0/a~1b/~0profile" to "/1/slots/-", then '
     'copy "/0/a~1b/~0profile" to "/1/slots/0", then set "/1/slots/1/tags/0" to "new".', "escaped"),
])
def test_edit_one_copy_only(local, ask, kind):
    scope, ledger, materials = local
    rows = [{"id": "source"}, {"id": "first"}, {"id": "sibling"}]
    if kind == "object":
        rows[0]["template"] = {"limits": [{"value": 7}, {"value": 12}]}
    elif kind == "array":
        rows[0]["parts"] = [None, {"names": ["north", "west"]}]
    else:
        rows[0]["a/b"] = {"~profile": {"tags": ["old", "fixed"]}}
        rows[1]["slots"] = []
    (_, mid, context), raw = pin_selection(scope, 1, "items", rows, ledger, materials)
    expected = json.loads(raw)["items"]
    if kind == "object":
        expected[1]["config"] = deepcopy(expected[0]["template"])
        expected[2]["config"] = deepcopy(expected[0]["template"])
        expected[1]["config"]["limits"][0]["value"] = 42
    elif kind == "array":
        expected[1]["parts"] = deepcopy(expected[0]["parts"])
        expected[2]["parts"] = deepcopy(expected[0]["parts"])
        expected[2]["parts"][1]["names"][0] = "south"
    else:
        template = expected[0]["a/b"]["~profile"]
        expected[1]["slots"] = [deepcopy(template), deepcopy(template)]
        expected[1]["slots"][1]["tags"][0] = "new"
    intake = bind_selection({}, *scope, mid, context)
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_array_patch"
    status, result = tool_loader._run_source_in_sandbox(source, "copy_material.py", intake, {})
    assert status == "ok", result
    report, overlay = result
    assert overlay is None
    assert report == {"records": expected, "material_id": mid, "source_sha256": context["source"]["sha256"],
                      "records_pointer": "/items"}
    assert report["records"][0] == rows[0]
    assert originals.read_content(*scope[:2], materials[mid]) == raw
