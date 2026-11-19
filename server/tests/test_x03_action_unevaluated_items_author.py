"""Independently union indices annotated by successful array alternatives."""
import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_string_length_author import pinned_dispatch
from test_x03_action_extra_fields_author import closed


CASES = [
    ("Run the selected action to file the panel tag and its review marks.", "panel-marks",
     "P2", "green", "checked"),
    ("Execute the discovered action to index the beam survey tuple.", "beam-survey",
     "B4", 8, "verified"),
    ("Submit my chosen action to record a meter tag and optional signoff.", "meter-signoff",
     "M1", "approved", 7),
]


@pytest.mark.parametrize("ask,operation,tag,detail,marker", CASES)
def test_evaluated_indices(catalog, ask, operation, tag, detail, marker):
    rule = {"type": "array", "minItems": 1, "prefixItems": [{"const": tag}],
            "anyOf": [{"minItems": 2, "prefixItems": [True, {"const": detail}]},
                      {"contains": {"const": marker}}],
            "unevaluatedItems": False}
    dispatch, calls = pinned_dispatch(catalog, ask, operation, closed(entries=rule))
    samples = [[tag, detail, marker], [tag, detail], [tag, marker, marker],
               [tag, None, marker], [tag, detail, None], [tag], ["wrong", detail, marker]]
    expected = []
    for entries in samples:
        prefix_match = len(entries) >= 2 and entries[1] == detail
        marker_indices = {index for index, entry in enumerate(entries) if entry == marker}
        evaluated = {0} | marker_indices
        if prefix_match:
            evaluated.add(1)
        payload = {"entries": entries}
        if entries[0] == tag and (prefix_match or marker_indices) and evaluated == set(range(len(entries))):
            dispatch(payload)
            expected.append(payload)
        else:
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                dispatch(payload)
        assert calls == expected
