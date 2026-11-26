"""Check explicit local-only reference support in nested identifier scopes."""
from copy import deepcopy
from urllib.parse import urljoin
import urllib.request

import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_dynamic_refs_author import selected_operation
from test_x03_action_extra_fields_author import closed


CASES = [
    ("Run the selected action to apply each wing's panel codebook.", "wing-code", "east/", "west/", "codes"),
    ("Execute the discovered action to read each bay's beam grade table.", "bay-grade", "bay/a/", "bay/b/", "spec/grade"),
    ("Submit my chosen action to validate meter marks by local drawing group.", "meter-mark",
     "meters/one/", "meters/two/", "../codes"),
]


@pytest.mark.parametrize("ask,operation,left,right,target", CASES)
def test_nested_resource_base(catalog, monkeypatch, ask, operation, left, right, target):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **kw: pytest.fail("URL retrieval"))
    base = "https://example.invalid/design/"
    # Both references must have distinct destinations after relative URI resolution.
    if target.startswith("../"):
        left += "sheet/"
        right += "sheet/"
    resolved = [urljoin(urljoin(base, prefix), target) for prefix in (left, right)]
    assert resolved[0] != resolved[1]
    schema = closed(a={"$ref": "#/$defs/a"}, b={"$ref": "#/$defs/b"})
    schema.update({"$id": base, "$defs": {}})
    for field, prefix, uri in zip(("a", "b"), (left, right), resolved):
        schema["$defs"][field] = {"$id": prefix, "$ref": target,
                                  "$defs": {"code": {"$id": uri, "const": field.upper()}}}
    register, dispatch, calls = selected_operation(catalog, ask, operation, schema)
    with pytest.raises(ValueError, match="references must be local"):
        register()
    with pytest.raises(ReadFailure, match="^denied$"):
        dispatch({"a": "A", "b": "B"})
    assert calls == []
    control = deepcopy(schema)
    for sub in control["$defs"].values():
        sub["$ref"] = "#/$defs/code"
    register, dispatch, calls = selected_operation(catalog, ask, operation, control)
    register()
    with pytest.raises(ReadFailure, match="^invalid_input$"):
        dispatch({"a": "B", "b": "A"})
    dispatch({"a": "A", "b": "B"})
    assert calls == [{"a": "A", "b": "B"}]
