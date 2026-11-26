"""Bound user-facing diagnostic count and bytes while preserving failure."""
from copy import deepcopy

import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_extra_fields_author import closed
from test_x03_action_string_length_author import pinned_dispatch


CASES = [
    ("Run the selected action to review the tagged rows of the panel schedule.", "panel-rows", "panel", 8),
    ("Execute the discovered action to file the results for each surveyed beam.", "beam-results", "beam", 12),
    ("Submit my chosen action to check the approved fields in this meter sheet.", "meter-sheet", "meter", 16),
]


@pytest.mark.parametrize("ask,operation,kind,count", CASES)
def test_bounded_submission_diagnostics(catalog, ask, operation, kind, count):
    section = kind * 20
    keys = [f"{kind}-{i:02d}" for i in range(count)]
    row = closed(**{key: {"type": "integer", "minimum": 0} for key in keys})
    schema = closed(**{section: {"type": "array", "items": row}})
    dispatch, calls = pinned_dispatch(catalog, ask, operation, schema)
    valid = {section: [{key: 0 for key in keys} for _ in range(2)]}
    many_bad = {section: [{key: -1 for key in keys} for _ in range(2)]}
    final_bad = deepcopy(valid)
    final_bad[section][-1][keys[-1]] = -1
    for payload, expected_errors in [(many_bad, 2 * count), (final_bad, 1)]:
        assert sum(number < 0 for entry in payload[section] for number in entry.values()) == expected_errors
        with pytest.raises(ReadFailure) as rejected:
            dispatch(payload)
        # Local actions expose one category, independent of the number or size of paths.
        assert rejected.value.args == ("invalid_input",)
        assert len(str(rejected.value).encode("utf-8")) <= 13
        assert calls == []
    dispatch(valid)
    assert calls == [valid]
