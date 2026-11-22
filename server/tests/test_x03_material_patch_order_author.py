"""Apply dependent edits with list slicing as an independent array-order oracle."""
from copy import deepcopy
import json

import pytest

from test_integration_tool_service import rig
from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


X = {"id": "X", "value": 9}
CASES = [
    ('Edit the selected JSON material: insert {"id":"X","value":9} at index 1, then '
     'move "C" from index 3 to index 0, then remove "A" at index 1.',
     [("insert", 1, X), ("move", 3, 0), ("remove", 1)], 'remove "A" at index 1', 'remove "A" at index 2'),
    ('Patch my material records: remove "B" at index 1, then move "A" from index 0 '
     'to index 2, then insert {"id":"X","value":9} at index 2.',
     [("remove", 1), ("move", 0, 2), ("insert", 2, X)], 'at index 2.', 'at index 5.'),
    ('Update the imported JSON records: move "D" from index 3 to index 1, then '
     'insert {"id":"X","value":9} at index 0, then remove "B" at index 3.',
     [("move", 3, 1), ("insert", 0, X), ("remove", 3)], 'remove "B" at index 3', 'move "B" from index 3 to index 5'),
]


def apply_steps(rows, steps):
    for action, start, *extra in steps:
        if action == "insert":
            rows = rows[:start] + [extra[0]] + rows[start:]
        else:
            original = rows[start]
            rows = rows[:start] + rows[start+1:]
            if action == "move":
                end, = extra
                rows = rows[:end] + [original] + rows[end:]
    return rows


@pytest.mark.parametrize("ask,steps,valid,stale", CASES)
def test_ordered_patch_or_atomic_refusal(local, ask, steps, valid, stale):
    scope, ledger, materials = local
    rows = [{"id": name, "value": i} for i, name in enumerate("ABCD")]
    (_, mid, context), raw = pin_selection(scope, 1, "items", rows, ledger, materials)
    intake = bind_selection({}, *scope, mid, context)
    initial = deepcopy(intake)
    for request, accepted in ((ask, True), (ask.replace(valid, stale), False)):
        package, source, _ = tools_fallback.author_tool(request)
        assert package["engine_op"] == "material_array_patch"
        status, result = tool_loader._run_source_in_sandbox(source, "ordered_patch.py", intake, {})
        if accepted:
            assert status == "ok", result
            edited, overlay = result
            assert overlay is None
            assert edited == {"material_id": mid, "source_sha256": context["source"]["sha256"],
                              "records_pointer": "/items", "records": apply_steps(rows, steps)}
        else:
            assert status == "tool_error" and "Stale" in str(result)
        assert intake == initial
        assert originals.read_content(*scope[:2], materials[mid]) == raw
        assert json.loads(raw)["items"] == rows
