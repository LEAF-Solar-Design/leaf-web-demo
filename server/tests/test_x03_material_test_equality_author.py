"""Compare JSON patch guards by JSON type, object members and array position."""
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
    ('Edit the selected JSON material: set "/0/mark" to 1, then test "/0/check" equals true.',
     True, 1),
    ('Patch my material records: set "/0/mark" to 2, then test "/0/check" equals {"b":2.0,"a":false}.',
     {"a": False, "b": 2}, {"a": 0, "b": 2}),
    ('Update the imported JSON records: set "/0/mark" to 3, then test "/0/check" equals ["a",2,{"n":null}].',
     ["a", 2, {"n": None}], [2, "a", {"n": None}]),
]


def canonical(value):
    if type(value) is dict:
        return ("object", tuple(sorted((key, canonical(item)) for key, item in value.items())))
    if type(value) is list:
        return ("array", tuple(map(canonical, value)))
    if type(value) in (int, float):
        return ("number", value)
    return (type(value).__name__, value)


@pytest.mark.parametrize("ask,matching,other", CASES)
def test_material_test_equality(local, ask, matching, other):
    scope, ledger, materials = local
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_array_patch"
    wanted = json.loads(ask.rsplit(" equals ", 1)[1][:-1])
    mark = int(ask.split('"/0/mark" to ', 1)[1].split(",", 1)[0])
    for revision, candidate in enumerate((other, matching), 1):
        original = [{"id": "r", "mark": 0, "check": candidate}]
        (_, mid, context), raw = pin_selection(scope, revision, "items", original, ledger, materials)
        intake = bind_selection({}, *scope, mid, context)
        status, result = tool_loader._run_source_in_sandbox(source, "material_guard.py", intake, {})
        matches = canonical(candidate) == canonical(wanted)
        assert matches == (revision == 2)
        if matches:
            assert status == "ok", result
            assert canonical(result[0]["records"]) == canonical([{**original[0], "mark": mark}])
            assert result[0]["source_sha256"] == sha256(raw).hexdigest()
        else:
            assert status == "tool_error" and "Material test failed" in str(result)
        assert originals.read_content(*scope[:2], materials[mid]) == raw
        assert canonical(intake["checker_material_selection"]["record_values"]) == canonical(original)
