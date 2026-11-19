"""Validate pinned string lengths against the Unicode code points in each action input."""
import httpx
import pytest

from test_integration_tool_service import catalog, rig
from authored_action_revocation import AuthorizedLocalActions, run_selected_action, select_action
from integration_tool_service import ReadFailure
from test_x03_action_discovery_author import LIST_ASK, action
from test_x03_action_extra_fields_author import closed, schema_digest


def pinned_dispatch(catalog, ask, operation, schema):
    service, auth = catalog.svc, catalog.auth
    scope = auth.tenant_id, auth.session_id, auth.turn_id
    project = auth.tenant_id, auth.project_id
    record = action("drawings", operation, operation.replace("-", " ").title(), 3)
    record["operation_ref"]["digest"] = schema_digest(schema)
    service.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"actions": [record], "next_cursor": None}))
    selected, = service.run_authored_action_report(LIST_ASK, *scope, include_form_digest=True)["actions"]
    pins = {key: selected[key] for key in ("connection_id", "connector_ref", "operation_ref")}
    host = AuthorizedLocalActions()
    calls = []
    host.register(*project, pins, calls.append, argument_schema=schema)
    host.set_role_grants(*project, "editor", [pins])
    host.assign_roles(*project, auth.subject, ["editor"])

    def dispatch(payload):
        selection = select_action(service, ask, *scope, selected=selected, arguments=payload)
        run_selected_action(service, selection, *scope, host=host)

    return dispatch, calls


CASES = [
    ("Run the selected action to label the panel with a single symbol.", "panel-symbol",
     1, 1, ["\U0001f4a1", "", "e\u0301", "\u00e9"]),
    ("Submit my chosen action to save the two-character beam mark.", "beam-mark",
     2, 2, ["e\u0301", "\u00e9", "\U0001f6e0\ufe0f", "\U0001f469\u200d\U0001f527"]),
    ("Execute the discovered action to tag the inspection note.", "inspection-note",
     2, 3, ["\U0001f469\u200d\U0001f527", "\U0001f1e8\U0001f1e6", "A", "Ae\u0301B"]),
]


@pytest.mark.parametrize("ask,operation,minimum,maximum,samples", CASES)
def test_action_string_lengths_in_code_points(catalog, ask, operation, minimum, maximum, samples):
    schema = closed(text={"type": "string", "minLength": minimum, "maxLength": maximum})
    dispatch, calls = pinned_dispatch(catalog, ask, operation, schema)
    expected = []
    for text in samples:
        # Each Unicode scalar starts with exactly one non-continuation byte in UTF-8.
        code_points = sum(byte & 0xC0 != 0x80 for byte in text.encode("utf-8"))
        payload = {"text": text}
        if minimum <= code_points <= maximum:
            dispatch(payload)
            expected.append(payload)
        else:
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                dispatch(payload)
        assert calls == expected
