"""Check admission and small input boundaries before local action handlers run."""
import json

import pytest
from jsonschema.exceptions import SchemaError

from test_integration_tool_service import catalog, rig
import authored_action_revocation as actions
from integration_tool_service import ReadFailure
from test_x03_action_dynamic_refs_author import selected_operation
from test_x03_action_extra_fields_author import closed


CASES = [
    ("Run the selected action to check this panel's tag pattern.", "panel-tags", "value", "(?:panel)"),
    ("Execute the discovered action to file each numbered beam field.", "beam-fields", "key", "(beam)"),
    ("Submit my chosen action to apply the pinned meter code rule.", "meter-codes", "ref", "meter|gauge"),
]


@pytest.mark.parametrize("ask,operation,position,pattern", CASES)
def test_pattern_admission_and_input_bound(catalog, monkeypatch, ask, operation, position, pattern):
    def schema_for(expression):
        if position == "key":
            return {"type": "object", "patternProperties": {expression: True}, "additionalProperties": False}
        rule = {"type": "string", "pattern": expression}
        if position == "ref":
            root = closed(value={"$ref": "#/$defs/code"})
            root["$defs"] = {"code": rule}
            return root
        return closed(value=rule)

    # Benign examples of excluded group/alternation syntax need no costly match.
    register, dispatch, calls = selected_operation(catalog, ask, operation, schema_for(pattern))
    with pytest.raises(SchemaError, match="Unsupported action pattern"):
        register()
    with pytest.raises(ReadFailure, match="^denied$"):
        dispatch({"value": "panel"})
    assert calls == []

    rule = "^tag[0-9]+$"
    register, dispatch, calls = selected_operation(catalog, ask, operation, schema_for(rule))
    register()
    tag = "tag1234"
    payload = {tag: True} if position == "key" else {"value": tag}
    size = len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    monkeypatch.setattr(actions, "REGEX_ARGUMENT_LIMIT", size - 1)
    with pytest.raises(ReadFailure, match="^schema_regex_limit$"):
        dispatch(payload)
    assert calls == []
    monkeypatch.setattr(actions, "REGEX_ARGUMENT_LIMIT", size)
    invalid = {"bad": True} if position == "key" else {"value": "bad"}
    with pytest.raises(ReadFailure, match="^invalid_input$"):
        dispatch(invalid)
    assert calls == []
    dispatch(payload)
    assert calls == [payload]
