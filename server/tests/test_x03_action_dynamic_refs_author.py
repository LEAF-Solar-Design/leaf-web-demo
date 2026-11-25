"""Try a scoped dynamic binding and its pinned static counterpart offline."""
import urllib.request

import httpx
import pytest

from test_integration_tool_service import catalog, rig
from authored_action_revocation import AuthorizedLocalActions, run_selected_action, select_action
from integration_tool_service import ReadFailure
from test_x03_action_discovery_author import LIST_ASK, action
from test_x03_action_extra_fields_author import closed, schema_digest


def selected_operation(catalog, ask, operation, schema):
    service, auth = catalog.svc, catalog.auth
    scope = auth.tenant_id, auth.session_id, auth.turn_id
    project = auth.tenant_id, auth.project_id
    record = action("drawings", operation, operation.title(), 1)
    record["operation_ref"]["digest"] = schema_digest(schema)
    service.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"actions": [record], "next_cursor": None}))
    selected, = service.run_authored_action_report(LIST_ASK, *scope, include_form_digest=True)["actions"]
    pins = {key: selected[key] for key in ("connection_id", "connector_ref", "operation_ref")}
    calls = []
    host = AuthorizedLocalActions()
    host.set_role_grants(*project, "editor", [pins])
    host.assign_roles(*project, auth.subject, ["editor"])

    def register():
        host.register(*project, pins, calls.append, argument_schema=schema)

    def dispatch(payload):
        selection = select_action(service, ask, *scope, selected=selected, arguments=payload)
        run_selected_action(service, selection, *scope, host=host)

    return register, dispatch, calls


CASES = [
    ("Run the selected action to apply the panel grade defined by this drawing.", "panel-grade", "A", "B"),
    ("Submit my chosen action to apply the beam survey's local unit.", "beam-units", "feet", "meters"),
    ("Execute the discovered action to use the meter's project precision.", "meter-precision", 2, 4),
]


@pytest.mark.parametrize("ask,operation,outer,inner", CASES)
def test_scoped_dynamic_references(catalog, monkeypatch, ask, operation, outer, inner):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda *args, **kwargs: pytest.fail("Schema resolution tried to fetch a URL"))
    schema = closed(value={"$ref": "#/$defs/field"})
    schema.update({"$id": "urn:local:form", "$defs": {
        "binding": {"$dynamicAnchor": "limit", "const": outer},
        "field": {"$id": "urn:local:field", "$dynamicRef": "#limit",
                  "$defs": {"binding": {"$dynamicAnchor": "limit", "const": inner}}}}})
    register, dispatch, calls = selected_operation(catalog, ask, operation, schema)
    with pytest.raises(ValueError, match="static draft 2020-12 references"):
        register()
    for value in (outer, inner):
        with pytest.raises(ReadFailure, match="^denied$"):
            dispatch({"value": value})
    assert calls == []

    static = closed(value={"$ref": "#/$defs/binding"})
    static["$defs"] = {"binding": {"const": outer}}
    register, dispatch, calls = selected_operation(catalog, ask, operation, static)
    register()
    with pytest.raises(ReadFailure, match="^invalid_input$"):
        dispatch({"value": inner})
    assert calls == []
    dispatch({"value": outer})
    assert calls == [{"value": outer}]
