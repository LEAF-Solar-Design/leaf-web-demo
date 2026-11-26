"""Refuse malformed argument schemas before granting them a working handler."""
import pytest
from jsonschema.exceptions import SchemaError

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_dynamic_refs_author import selected_operation
from test_x03_action_extra_fields_author import closed


CASES = [
    ("Run the selected action to confirm the panel code has enough characters.", "panel-minimum",
     {"type": "string", "minLength": -1}, {"type": "string", "minLength": 2}, "A", "AB"),
    ("Execute the discovered action to save the beam labels within the item limit.", "beam-limit",
     {"type": "array", "maxItems": "2"}, {"type": "array", "maxItems": 2}, [1, 2, 3], [1, 2]),
    ("Submit my chosen action to record the required meter field.", "meter-field",
     {"type": "object", "required": "meter"}, {"type": "object", "required": ["meter"]}, {}, {"meter": "M"}),
]


@pytest.mark.parametrize("ask,operation,malformed,control,bad,good", CASES)
def test_pinned_meta_validation(catalog, ask, operation, malformed, control, bad, good):
    def pinned(rule):
        schema = closed(value={"$ref": "#/$defs/constraint"})
        schema["$defs"] = {"constraint": rule}
        return selected_operation(catalog, ask, operation, schema)

    register, dispatch, calls = pinned(malformed)
    with pytest.raises(SchemaError):
        register()
    with pytest.raises(ReadFailure, match="^denied$"):
        dispatch({"value": good})
    assert calls == []

    register, dispatch, calls = pinned(control)
    register()
    with pytest.raises(ReadFailure, match="^invalid_input$"):
        dispatch({"value": bad})
    assert calls == []
    dispatch({"value": good})
    assert calls == [{"value": good}]
