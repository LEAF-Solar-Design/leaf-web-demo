"""Track keys contributed by separate allOf branches before enforcing closure."""
import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_string_length_author import pinned_dispatch


CASES = [
    ("Run the selected action to record the panel and its inspection zone.", "panel-zone",
     ({"panel": 1}, {"zone": "east"}), ["note"]),
    ("Execute the discovered action to file a beam inspection with its signoff.", "beam-signoff",
     ({"beam": "A"}, {"signed": True, "grade": 2}), ["beam_id", "comment"]),
    ("Submit my chosen action to index this meter by level and room.", "meter-index",
     ({"meter": "M"}, {"level": 2}, {"room": "north"}), ["floor"]),
]


@pytest.mark.parametrize("ask,operation,groups,extras", CASES)
def test_action_allof_evaluated_keys(catalog, ask, operation, groups, extras):
    branches = [{"properties": {key: {"const": value} for key, value in group.items()},
                 "required": list(group)} for group in groups]
    schema = {"type": "object", "allOf": branches, "unevaluatedProperties": False}
    dispatch, calls = pinned_dispatch(catalog, ask, operation, schema)
    combined = {key: value for group in groups for key, value in group.items()}
    expected = []
    samples = [combined, *({**combined, key: 1} for key in extras),
               {**combined, next(iter(groups[-1])): None}, {}]
    for payload in samples:
        # Only matching branches contribute their evaluated keys to the union.
        matched = [group for group in groups if group.items() <= payload.items()]
        evaluated = {key for group in matched for key in group}
        if len(matched) == len(groups) and payload.keys() <= evaluated:
            dispatch(payload)
            expected.append(payload)
        else:
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                dispatch(payload)
        assert calls == expected
