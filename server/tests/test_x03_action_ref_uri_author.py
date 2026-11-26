"""Check fragment decoding against literal schema names and local absolute pointers."""
import urllib.request

import pytest
from jsonschema.exceptions import SchemaError

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_dynamic_refs_author import selected_operation
from test_x03_action_extra_fields_author import closed


CASES = [
    ("Run the selected action to read the beam grade from the slash-named form.", "beam-slash",
     "#%2F%24defs%2Fbeam%7E1grade", "beam/grade", "beam~1grade", "#/$defs/bad~2key", "bad~2key"),
    ("Submit my chosen action to use the literal percent code on the panel.", "panel-percent",
     "#/$defs/panel%252Fgrade", "panel%2Fgrade", "panel/grade", "#/$defs/bad%GG", "bad%GG"),
    ("Execute the discovered action to check the meter's escaped tilde key.", "meter-tilde",
     "#/$defs/meter%7E01", "meter~1", "meter/", "#/$defs/bad%FF", "bad\ufffd"),
]


@pytest.mark.parametrize("ask,operation,reference,key,other,bad_ref,bad_key", CASES)
def test_local_fragment_targets(catalog, monkeypatch, ask, operation, reference, key, other, bad_ref, bad_key):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **kw: pytest.fail("URL retrieval"))
    schema = closed(value={"$ref": bad_ref})
    schema["$defs"] = {key: {"const": "target"}, other: {"const": "decoy"}, bad_key: True}
    register, dispatch, calls = selected_operation(catalog, ask, operation, schema)
    with pytest.raises((ValueError, SchemaError)):
        register()
    with pytest.raises(ReadFailure, match="^denied$"):
        dispatch({"value": "target"})
    assert calls == []
    schema["properties"]["value"]["$ref"] = reference
    register, dispatch, calls = selected_operation(catalog, ask, operation, schema)
    register()
    with pytest.raises(ReadFailure, match="^invalid_input$"):
        dispatch({"value": "decoy"})
    assert calls == []
    dispatch({"value": "target"})
    assert calls == [{"value": "target"}]
