"""Reject duplicate schema bindings while allowing reuse of names across resources."""
from copy import deepcopy
import urllib.request

import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_dynamic_refs_author import selected_operation
from test_x03_action_extra_fields_author import closed


CASES = [
    ("Run the selected action to check a panel reference in its local catalog.", "panel", "anchor"),
    ("Submit my chosen action to compare the beam codes from two referenced forms.", "beam", "id"),
    ("Execute the discovered action to validate the nested meter marks.", "meter", "relative-id"),
]


def field(resource_id, tag):
    return {"$id": resource_id, "$ref": "#mark", "$defs": {"tag": {"$anchor": "mark", "const": tag}}}


@pytest.mark.parametrize("ask,operation,collision", CASES)
def test_local_resource_and_anchor_identity(catalog, monkeypatch, ask, operation, collision):
    def forbid_fetch(*args, **kwargs):
        pytest.fail("Local schema resource resolution attempted a network fetch")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbid_fetch)
    schema = closed(left={"$ref": "#/$defs/left"}, right={"$ref": "#/$defs/right"})
    base = "https://example.invalid/forms/"
    schema.update({"$id": base + "root", "$defs": {"left": field("left", "L"), "right": field("right", "R")}})
    invalid = deepcopy(schema)
    left, right = invalid["$defs"]["left"], invalid["$defs"]["right"]
    if collision == "anchor":
        left["$defs"]["another"] = {"$anchor": "mark", "const": "S"}
    elif collision == "id":
        right["$id"] = left["$id"]
    else:
        right["$id"] = base + "left"
    register, dispatch, calls = selected_operation(catalog, ask, operation, invalid)
    with pytest.raises(ValueError, match="Ambiguous local schema"):
        register()
    with pytest.raises(ReadFailure, match="^denied$"):
        dispatch({"left": "L", "right": "R"})
    assert calls == []

    register, dispatch, calls = selected_operation(catalog, ask, operation, schema)
    register()
    with pytest.raises(ReadFailure, match="^invalid_input$"):
        dispatch({"left": "R", "right": "L"})
    assert calls == []
    dispatch({"left": "L", "right": "R"})
    assert calls == [{"left": "L", "right": "R"}]
