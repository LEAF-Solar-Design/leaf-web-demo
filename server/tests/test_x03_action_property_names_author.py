"""Check literal JSON member names through authored action dispatch."""
import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_string_length_author import pinned_dispatch
from test_x03_action_extra_fields_author import closed


CASES = [
    ("Run the selected action to label a beam by its drawing path.", "beam-path",
     {"enum": ["beam/mark", "beam~mark"]}, {"beam/mark", "beam~mark"}, ["beam~1mark", "beam%2Fmark"]),
    ("Execute the discovered action to attach the quoted panel reference.", "panel-reference",
     {"const": 'panel"ref'}, {'panel"ref'}, ['panel\\"ref', "PANEL"]),
    ("Submit my chosen action to store the meter tags with their original accents.", "meter-tags",
     {"enum": ["e\u0301", "meter\\id"]}, {"e\u0301", "meter\\id"}, ["\u00e9", "meter/id"]),
]


@pytest.mark.parametrize("ask,operation,rule,allowed,other", CASES)
def test_property_names(catalog, ask, operation, rule, allowed, other):
    schema = closed(labels={"type": "object", "propertyNames": rule})
    dispatch, calls = pinned_dispatch(catalog, ask, operation, schema)
    names = {key: "tag" for key in sorted(allowed)}
    expected = []
    for labels in [names, *({**names, key: "tag"} for key in other), {}]:
        payload = {"labels": labels}
        if set(labels) <= allowed:
            dispatch(payload)
            expected.append(payload)
        else:
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                dispatch(payload)
        assert calls == expected
