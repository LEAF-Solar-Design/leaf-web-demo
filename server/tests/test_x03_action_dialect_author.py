"""Verify the same pinned dialect applies at root and referenced schema resources."""
import socket
import urllib.request

import httpx
import pytest

from test_integration_tool_service import catalog, rig
from authored_action_revocation import AuthorizedLocalActions, run_selected_action, select_action
from integration_tool_service import ReadFailure
from test_x03_action_discovery_author import LIST_ASK, action
from test_x03_action_extra_fields_author import closed, schema_digest
from test_x03_action_string_length_author import pinned_dispatch


DRAFT = "https://json-schema.org/draft/2020-12/schema"
CASES = [
    ("Run the selected action to record a panel reading under its declared form.", "panel",
     DRAFT, DRAFT),
    ("Execute the discovered action to submit the meter samples using its catalog schema.", "meter",
     "urn:local:unknown-dialect", DRAFT),
    ("Submit my chosen action to file the beam survey under its pinned rules.", "beam",
     DRAFT, "http://json-schema.org/draft-07/schema#"),
]


@pytest.mark.parametrize("ask,operation,root_dialect,child_dialect", CASES)
def test_dialect_pins(catalog, monkeypatch, ask, operation, root_dialect, child_dialect):
    connect = socket.socket.connect
    def forbid_network(sock, address):
        # Windows asyncio creates its own loopback socket pair.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return connect(sock, address)
        pytest.fail("Schema resolution attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda *args, **kwargs: pytest.fail("Attempted URL retrieval"))
    schema = closed(readings={"$ref": "#/$defs/readings"})
    schema.update({"$schema": root_dialect, "$defs": {"readings": {
        "$schema": child_dialect, "type": "array", "minItems": 1,
        "prefixItems": [{"const": 42}], "items": False}}})
    if root_dialect != DRAFT or child_dialect != DRAFT:
        service, auth = catalog.svc, catalog.auth
        scope = auth.tenant_id, auth.session_id, auth.turn_id
        project = auth.tenant_id, auth.project_id
        record = action("drawings", operation, operation.title(), 3)
        record["operation_ref"]["digest"] = schema_digest(schema)
        service.transport = httpx.MockTransport(
            lambda _: httpx.Response(200, json={"actions": [record], "next_cursor": None}))
        selected, = service.run_authored_action_report(LIST_ASK, *scope, include_form_digest=True)["actions"]
        pins = {key: selected[key] for key in ("connection_id", "connector_ref", "operation_ref")}
        host = AuthorizedLocalActions()
        host.set_role_grants(*project, "editor", [pins])
        host.assign_roles(*project, auth.subject, ["editor"])
        calls = []
        with pytest.raises(ValueError, match="Unsupported action schema dialect"):
            host.register(*project, pins, calls.append, argument_schema=schema)
        selection = select_action(service, ask, *scope, selected=selected, arguments={"readings": [42]})
        with pytest.raises(ReadFailure, match="^denied$"):
            run_selected_action(service, selection, *scope, host=host)
        assert calls == []
    schema["$schema"] = schema["$defs"]["readings"]["$schema"] = DRAFT
    dispatch, calls = pinned_dispatch(catalog, ask, operation, schema)
    for values in [[0], [42, 0]]:
        with pytest.raises(ReadFailure, match="^invalid_input$"):
            dispatch({"readings": values})
        assert calls == []
    dispatch({"readings": [42]})
    assert calls == [{"readings": [42]}]
