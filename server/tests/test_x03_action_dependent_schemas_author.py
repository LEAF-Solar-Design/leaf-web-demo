"""Test which object a selected action checks when a dependency key is present."""
import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_string_length_author import pinned_dispatch


CASES = [
    ("Run the selected action to record the inspector for signed panels.", "sign-panels",
     "signature", "inspector", "Li"),
    ("Execute the discovered action to route tagged defects to their queue.", "route-defects",
     "tag", "queue", "steel"),
    ("Submit my chosen action to store the unit beside each meter reading.", "meter-unit",
     "reading", "unit", "volts"),
]


@pytest.mark.parametrize("ask,operation,trigger,required,value", CASES)
def test_whole_object_dependencies(catalog, ask, operation, trigger, required, value):
    schema = {"type": "object", "properties": {trigger: True, required: True},
              "additionalProperties": False,
              "dependentSchemas": {trigger: {"required": [required],
                                             "properties": {required: {"const": value}}}}}
    dispatch, calls = pinned_dispatch(catalog, ask, operation, schema)
    expected = []
    for payload in [{}, {required: "other"}, {trigger: True}, {trigger: None},
                    {trigger: None, required: value}, {trigger: False, required: "other"},
                    {trigger: {required: value}}, {trigger: False, required: value}]:
        if trigger not in payload or payload.get(required) == value:
            dispatch(payload)
            expected.append(payload)
        else:
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                dispatch(payload)
        assert calls == expected
