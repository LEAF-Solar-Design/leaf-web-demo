"""Count schema evaluations per submission on tiny, independently counted cases."""
import pytest

from test_integration_tool_service import catalog, rig
import authored_action_revocation as actions
from integration_tool_service import ReadFailure
from test_x03_action_extra_fields_author import closed
from test_x03_action_string_length_author import pinned_dispatch


ALTERNATIVES = [{"const": 1}, {"const": 2}, {"const": 3}]
CASES = [
    ("Run the selected action to record the route class chosen for this panel.", "route-class",
     {"anyOf": [False, {"oneOf": ALTERNATIVES}, False]}, 3, 7),
    ("Execute the discovered action to sign off the meter review using its grade table.", "meter-grade",
     {"oneOf": [False, {"$ref": "#/$defs/grades"}, False]}, 3, 9),
    ("Submit my chosen action to store the beam labels that match the catalog choices.", "beam-labels",
     {"type": "array", "items": {"$ref": "#/$defs/grades"}}, [3, 3], 12),
]


@pytest.mark.parametrize("ask,operation,rule,value,work", CASES)
def test_runtime_work_ceiling(catalog, monkeypatch, ask, operation, rule, value, work):
    # Each visit counts once (root, field, branch, or reference target). Three
    # alternatives are enough to test nested branches and a repeated array item.
    schema = closed(value=rule)
    schema["$defs"] = {"grades": {"$schema": "https://json-schema.org/draft/2020-12/schema",
                                  "anyOf": ALTERNATIVES}}
    dispatch, calls = pinned_dispatch(catalog, ask, operation, schema)
    payload = {"value": value}
    monkeypatch.setattr(actions, "SCHEMA_EVALUATION_LIMIT", work - 1, raising=False)
    for _ in range(2):
        with pytest.raises(ReadFailure, match="^schema_work_limit$"):
            dispatch(payload)
        assert calls == []
    monkeypatch.setattr(actions, "SCHEMA_EVALUATION_LIMIT", work)
    dispatch(payload)
    assert calls == [payload]
