"""Verify that schema annotations preserve exact submitted JSON."""
from copy import deepcopy
import json

import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_string_length_author import pinned_dispatch


CASES = [
    ("Run the selected action to save the panel label I supplied.", "panel-label", "drawing", "title", "panel"),
    ("Submit my chosen action to file a beam reading with its optional note.", "beam-note", "span", "note", 2),
    ("Execute the discovered action to record the original meter tags.", "meter-tags", "meter", "tags", ["M"]),
]


@pytest.mark.parametrize("ask,operation,required,optional,value", CASES)
def test_inert_annotations(catalog, ask, operation, required, optional, value):
    annotations = {"default": {"source": "fallback"}, "examples": [42, None], "deprecated": True}
    schema = {"type": "object", "properties": {required: annotations, optional: annotations},
              "required": [required], "additionalProperties": False}
    original = deepcopy(schema)
    dispatch, calls = pinned_dispatch(catalog, ask, operation, schema)
    expected = []
    for payload in [{}, {optional: None}, {required: value}, {required: value, optional: None},
                    {required: value, optional: {"provided": ["original"]}}]:
        frozen = json.dumps(payload, sort_keys=True)
        if required in payload:
            dispatch(payload)
            expected.append(json.loads(frozen))
        else:
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                dispatch(payload)
        assert json.dumps(payload, sort_keys=True) == frozen
        assert calls == expected
        assert schema == original
