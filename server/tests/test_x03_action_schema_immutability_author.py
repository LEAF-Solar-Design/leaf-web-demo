"""Verify shared catalog rules across successful, failed and repeated action submissions."""
from copy import deepcopy

import pytest
from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_dynamic_refs_author import selected_operation
from test_x03_action_extra_fields_author import closed

CASES = [
    ("Run the selected action to pair panel labels from my approved list.", "panel-pair", "east", "west"),
    ("Submit my chosen action to assign a material to both beam ends.", "beam-ends", "steel", "wood"),
    ("Execute the discovered action to record two inspection outcomes.", "inspection-pair", "hold", "ready"),
]


@pytest.mark.parametrize("ask,operation,first,second", CASES)
def test_shared_schema_is_immutable(catalog, ask, operation, first, second):
    shared = {"type": "string", "enum": [first, second], "default": first}
    schema = closed(left=shared, right=shared)
    snapshot = deepcopy(schema)
    register, dispatch, calls = selected_operation(catalog, ask, operation, schema)
    register()
    assert schema == snapshot
    good = {"left": first, "right": second}
    bad = {"left": first, "right": "unknown"}
    missing = {"right": second}
    expected = []
    for sequence in ((good, bad, missing), (missing, bad, good)):
        for payload in sequence:
            before = deepcopy(payload)
            if set(payload) == {"left", "right"} and all(
                    value in (first, second) for value in payload.values()):
                dispatch(payload)
                expected.append(before)
            else:
                with pytest.raises(ReadFailure, match="^invalid_input$"):
                    dispatch(payload)
            assert schema == snapshot
            assert payload == before
            assert calls == expected
    shared["enum"].append("unknown")
    with pytest.raises(ReadFailure, match="^invalid_input$"):
        dispatch(bad)
    assert calls == expected
