"""Run authored selections for shared and unusable locally pinned schema refs."""
from copy import deepcopy

import httpx
import pytest

from test_integration_tool_service import catalog, rig
from authored_action_revocation import AuthorizedLocalActions, run_selected_action, select_action
from integration_tool_service import ReadFailure
from test_x03_action_discovery_author import action
from test_x03_action_extra_fields_author import closed, schema_digest


CASES = [
    ("Run the selected action to record the source and destination panel tags.", "shared"),
    ("Execute the discovered action to link the start and end meter tags.", "unresolved"),
    ("Submit my chosen action to record the first and last beam tags.", "cyclic"),
]


def ref(name):
    return {"$ref": f"#/$defs/{name}"}


@pytest.mark.parametrize("ask,case", CASES)
def test_local_schema_ref_dispatch(catalog, ask, case):
    service, auth = catalog.svc, catalog.auth
    project = auth.tenant_id, auth.project_id
    scope = auth.tenant_id, auth.session_id, auth.turn_id
    record = action("survey", "record-finding", "Record finding", 3)
    service.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"actions": [record], "next_cursor": None}))
    schema = closed(start=ref("tag"), end=ref("tag"))
    schema["$defs"] = {"tag": {"type": "string", "minLength": 1}}
    host = AuthorizedLocalActions()
    calls = []
    payload = {"start": "A", "end": "B"}

    def discover(definition):
        record["operation_ref"]["digest"] = schema_digest(definition)
        selected, = service.run_authored_action_report(
            "Which action can record a finding?", *scope, include_form_digest=True)["actions"]
        pins = {k: selected[k] for k in ("connection_id", "connector_ref", "operation_ref")}
        host.set_role_grants(*project, "editor", [pins])
        host.assign_roles(*project, auth.subject, ["editor"])
        return selected, pins

    def dispatch(selected, args):
        selection = select_action(service, ask, *scope, selected=selected, arguments=args)
        run_selected_action(service, selection, *scope, host=host)

    if case != "shared":
        unusable = deepcopy(schema)
        unusable["$defs"]["tag"] = ref("alias")
        if case == "cyclic":
            unusable["$defs"]["alias"] = ref("tag")
        selected, pins = discover(unusable)
        error = "Cyclic" if case == "cyclic" else "Unresolved"
        with pytest.raises(ValueError, match=error):
            host.register(*project, pins, calls.append, argument_schema=unusable)
        with pytest.raises(ReadFailure, match="denied"):
            dispatch(selected, payload)
        assert calls == []

    selected, pins = discover(schema)
    host.register(*project, pins, calls.append, argument_schema=schema)
    for key in payload:
        with pytest.raises(ReadFailure, match="invalid_input"):
            dispatch(selected, {**payload, key: ""})
        assert calls == []
    dispatch(selected, payload)
    assert calls == [payload]


def test_local_schema_registration_limits():
    host = AuthorizedLocalActions()
    record = action("survey", "record-finding", "Record finding", 3)
    schema = closed(tag=ref("0"))
    schema["$defs"] = {str(i): ref(str(i+1)) for i in range(64)}
    schema["$defs"]["64"] = {"type": "string"}
    for definition, error in [(schema, "budget"), (closed(tag={"$ref": "other.json"}), "local")]:
        record["operation_ref"]["digest"] = schema_digest(definition)
        pins = {k: record[k] for k in ("connection_id", "connector_ref", "operation_ref")}
        with pytest.raises(ValueError, match=error):
            host.register("tenant", "project", pins, pytest.fail, argument_schema=definition)
