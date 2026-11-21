"""Exercise boolean schemas and negation on pinned local action arguments."""
import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_extra_fields_author import closed
from test_x03_action_string_length_author import pinned_dispatch


CASES = [
    ("Submit my chosen action to forward the drawing metadata.", "forward-metadata",
     [(True, [({}, True), ({"labels": ["a", 2]}, True)]),
      (False, [({}, False), ({"labels": ["a", 2]}, False)])]),
    ("Run the selected action to record the inspection and omit the retired field.", "inspection",
     [({**closed(note=True), "properties": {"note": True, "retired": False}},
       [({"note": None}, True), ({"note": {"tags": []}}, True),
        ({"note": "", "retired": False}, False), ({}, False)])]),
    ("Execute the discovered action to save the eligible beam state.", "beam-state",
     [({**closed(state={"not": {"type": "number"}}), "not": {"properties": {"state": {"const": "retired"}}}},
       [({"state": True}, True), ({"state": 0}, False),
        ({"state": "retired"}, False), ({"state": "active"}, True)])]),
]


@pytest.mark.parametrize("ask,operation,checks", CASES)
def test_action_boolean_schemas(catalog, ask, operation, checks):
    for schema, truth_table in checks:
        dispatch, calls = pinned_dispatch(catalog, ask, operation, schema)
        expected = []
        for payload, valid in truth_table:
            if valid:
                dispatch(payload)
                expected.append(payload)
            else:
                with pytest.raises(ReadFailure, match="^invalid_input$"):
                    dispatch(payload)
            assert calls == expected
