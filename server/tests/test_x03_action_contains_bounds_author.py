"""Count matching action inputs at each bound."""
import pytest
from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_extra_fields_author import closed
from test_x03_action_string_length_author import pinned_dispatch

CASES = [
    ("Run the selected action to submit a panel list with no retired entries.", "panels", "retired", 0, 0),
    ("Submit my chosen action to schedule one or two beam inspections.", "beams", "inspect", 1, 2),
    ("Execute the discovered action to reserve exactly two meter test points.", "meters", "test", 2, 2),
]


@pytest.mark.parametrize("ask,operation,tag,lower,upper", CASES)
def test_matching_item_bounds(catalog, ask, operation, tag, lower, upper):
    rule = {"type": "array", "contains": closed(tag={"const": tag}),
            "minContains": lower, "maxContains": upper}
    dispatch, calls = pinned_dispatch(catalog, ask, operation, closed(entries=rule))
    match, other = {"tag": tag}, {"tag": "other"}
    expected = []
    for entries in ([], [other], [match, other], [other, match, match], [match] * 3):
        count = sum(entry["tag"] == tag for entry in entries)
        payload = {"entries": entries}
        if lower <= count <= upper:
            dispatch(payload)
            expected.append(payload)
        else:
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                dispatch(payload)
        assert calls == expected
