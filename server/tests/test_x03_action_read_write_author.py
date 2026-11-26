"""Access annotations describe fields and leave validation and handler arguments intact."""
from copy import deepcopy
import json

import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_extra_fields_author import closed
from test_x03_action_dynamic_refs_author import selected_operation


CASES = [
    ("Run the selected action to attach the panel reference and its field notes.", "panel-notes",
     "panel", "notes", "readOnly", "writeOnly"),
    ("Submit my chosen action to save the beam mark and survey entry.", "beam-survey",
     "mark", "survey", "writeOnly", "readOnly"),
    ("Execute the discovered action to register the meter tag and its source.", "meter-source",
     "tag", "source", "readOnly", "readOnly"),
]


@pytest.mark.parametrize("ask,operation,outer,inner,outer_hint,inner_hint", CASES)
def test_read_write_metadata(catalog, ask, operation, outer, inner, outer_hint, inner_hint):
    schema = closed(**{outer: {"type": "string", outer_hint: True},
                       "details": closed(**{inner: {"type": "string", inner_hint: True}})})
    original_schema = deepcopy(schema)
    register, dispatch, calls = selected_operation(catalog, ask, operation, schema)
    register()
    valid = {outer: "ref-4", "details": {inner: "field-8"}}
    expected = []
    for payload in [{"details": valid["details"]}, {outer: valid[outer], "details": {}},
                    {outer: "ref-4", "details": {inner: 5}}, valid]:
        before = json.dumps(payload, sort_keys=True)
        if payload == valid:
            dispatch(payload)
            expected.append(json.loads(before))
        else:
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                dispatch(payload)
        assert calls == expected
        assert json.dumps(payload, sort_keys=True) == before and schema == original_schema
