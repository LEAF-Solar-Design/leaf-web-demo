"""Independently distinguish presence and JSON types in selected revision fields."""
import json

import pytest

from test_integration_tool_service import rig
from authored_material_revision import bind_material_revisions
from test_x03_material_revision_author import local, pin_selection
import tools_fallback
import tool_loader


CASES = [
    ("Compare missing and null fields in the two imported JSON revisions.",
     {}, {"key": None}, "absent", "null"),
    ("Diff the boolean and numeric values across my material versions.",
     {"key": False}, {"key": 0}, "false", "0"),
    ("Show which nested fields changed between these project material revisions.",
     {"key": None}, {}, "null", "absent"),
]


@pytest.mark.parametrize("ask,left,right,old,new", CASES)
def test_authored_field_presence(local, ask, left, right, old, new):
    scope, ledger, materials = local

    def row(fields):
        return {"id": "target", "a/b": {"~": [fields]}, "unchanged": None}

    fixed = {"id": "unchanged"}
    prior, _ = pin_selection(scope, 1, "roof/map", [fixed, row(left)], ledger, materials)
    current, _ = pin_selection(scope, 2, "roof/map", [row(right), fixed], ledger, materials)
    intake = bind_material_revisions({}, ledger, *scope, "inspection", prior, current,
                                     description=ask)
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_revision_diff"
    status, payload = tool_loader._run_source_in_sandbox(source, "fields.py", intake, {})
    assert status == "ok", payload
    changes = {r["id"]: r for r in payload[0]["changes"]}
    field, = changes["target"]["fields"]
    assert changes["unchanged"]["fields"] == []
    for side, i, expected in (("before", 1, old), ("after", 0, new)):
        state = field[side]
        assert state["pointer"] == f"/roof~1map/{i}/a~1b/~0/0/key"
        # JSON text preserves false versus zero in the independent expected states.
        if expected == "absent":
            assert state == {"pointer": state["pointer"], "present": False}
        else:
            assert state["present"] is True
            assert json.dumps(state["value"]) == expected
