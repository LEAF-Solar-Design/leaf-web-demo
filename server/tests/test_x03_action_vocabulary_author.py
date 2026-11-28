"""Gate local action capabilities at schema registration before any dispatch."""
import urllib.request

import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_dynamic_refs_author import selected_operation
from test_x03_action_extra_fields_author import closed


VALIDATION = "https://json-schema.org/draft/2020-12/vocab/validation"
CUSTOM = "https://example.invalid/schema/project-keywords"
CASES = [
    ("Run the selected action to grade this beam using the standard checks.", "beam-standard",
     {VALIDATION: True}, False),
    ("Submit my chosen action to file the panel survey with optional project hints.", "panel-hints",
     {VALIDATION: True, CUSTOM: False}, False),
    ("Execute the discovered action to classify this meter under its required local rules.", "meter-local",
     {CUSTOM: True}, True),
]


@pytest.mark.parametrize("ask,operation,declaration,nested", CASES)
def test_vocabulary_admission(catalog, monkeypatch, ask, operation, declaration, nested):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **kw: pytest.fail("URL retrieval"))
    schema = closed(grade={"$ref": "#/$defs/grade"})
    field = {"$id": "urn:local:grade", "enum": ["accepted", "deferred"]}
    schema["$defs"] = {"grade": field}
    resource = field if nested else schema
    resource["$vocabulary"] = declaration
    register, dispatch, calls = selected_operation(catalog, ask, operation, schema)
    if declaration.get(CUSTOM) is True:
        with pytest.raises(ValueError, match="Unsupported required action vocabulary"):
            register()
        with pytest.raises(ReadFailure, match="^denied$"):
            dispatch({"grade": "accepted"})
        assert calls == []
        resource["$vocabulary"] = {CUSTOM: False, VALIDATION: True}
        register, dispatch, calls = selected_operation(catalog, ask, operation, schema)
    register()
    with pytest.raises(ReadFailure, match="^invalid_input$"):
        dispatch({"grade": "unknown"})
    assert calls == []
    dispatch({"grade": "accepted"})
    assert calls == [{"grade": "accepted"}]
