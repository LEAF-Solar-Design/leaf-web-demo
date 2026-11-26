"""Exercise digest-pinned annotation and assertion policies on action inputs."""
from datetime import date
import re
import urllib.request

import pytest

from test_integration_tool_service import catalog, rig
from integration_tool_service import ReadFailure
from test_x03_action_dynamic_refs_author import selected_operation
from test_x03_action_extra_fields_author import closed


CASES = [
    ("Run the selected action to label the inspection date on this panel.", "panel-date", "annotation", "date"),
    ("Submit my chosen action to schedule the beam survey on a valid date.", "beam-date", "assertion", "date"),
    ("Execute the discovered action to record the meter's local date code.", "meter-date", "assertion", "local-day"),
]


def calendar_date(text):
    if not re.fullmatch("[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        return False
    try:
        date(*map(int, text.split("-")))
        return True
    except ValueError:
        return False


@pytest.mark.parametrize("ask,operation,policy,fmt", CASES)
def test_pinned_format_policy(catalog, monkeypatch, ask, operation, policy, fmt):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **kw: pytest.fail("URL retrieval"))
    schema = closed(day={"$ref": "#/$defs/field"})
    schema.update({"x-leaf-format-policy": policy, "$defs": {"field": {"type": "string", "format": fmt}}})
    register, dispatch, calls = selected_operation(catalog, ask, operation, schema)
    if fmt == "local-day":
        with pytest.raises(ValueError, match="Unsupported asserted action format"):
            register()
        with pytest.raises(ReadFailure, match="^denied$"):
            dispatch({"day": "2024-02-29"})
        assert calls == []
        schema["x-leaf-format-policy"] = policy = "annotation"
        register, dispatch, calls = selected_operation(catalog, ask, operation, schema)
    register()
    expected = []
    for text in ["2025-02-29", "2024-02-29", "20240229", "2024-04-31", "2024-04-30"]:
        payload = {"day": text}
        if policy == "annotation" or calendar_date(text):
            dispatch(payload)
            expected.append(payload)
        else:
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                dispatch(payload)
        assert calls == expected
