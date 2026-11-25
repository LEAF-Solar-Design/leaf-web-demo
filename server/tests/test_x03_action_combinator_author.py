"""Replay pinned action submissions against each combination of two branch matches."""
import httpx
import pytest

from authored_action_revocation import AuthorizedLocalActions, run_selected_action, select_action
from integration_tool_service import ReadFailure
from test_integration_tool_service import catalog, rig
from test_x03_action_discovery_author import action
from test_x03_action_extra_fields_author import schema_digest


CASES = [
    ("Run the selected action to locate the panel by tag or serial number.",
     "oneOf", "tag", "serial", {1}),
    ("Execute the discovered action to find inspections by zone or crew.",
     "anyOf", "zone", "crew", {1, 2}),
    ("Submit my chosen action to assign the repair to a crew at a site.",
     "allOf", "crew", "site", {2}),
]


@pytest.mark.parametrize("ask,combinator,left,right,accepted_counts", CASES)
def test_combinator_dispatch(catalog, ask, combinator, left, right, accepted_counts):
    service, auth = catalog.svc, catalog.auth
    scope = auth.tenant_id, auth.session_id, auth.turn_id
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {key: {"type": "string"} for key in (left, right)},
        combinator: [{"required": [left]}, {"required": [right]}],
    }
    record = action("inspection", "record-finding", "Record finding", 3)
    record["operation_ref"]["digest"] = schema_digest(schema)
    service.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"actions": [record], "next_cursor": None}))
    selected, = service.run_authored_action_report(
        "Which action can record a finding?", *scope, include_form_digest=True)["actions"]
    pins = {key: selected[key] for key in ("connection_id", "connector_ref", "operation_ref")}
    project = auth.tenant_id, auth.project_id
    calls = []
    host = AuthorizedLocalActions()
    host.register(*project, pins, calls.append, form=record["form"], argument_schema=schema)
    host.set_role_grants(*project, "editor", [pins])
    host.assign_roles(*project, auth.subject, ["editor"])

    expected_calls = []
    for fields in ((), (left,), (right,), (left, right)):
        payload = {field: field.upper() for field in fields}
        branch_matches = (left in payload, right in payload)
        selection = select_action(service, ask, *scope, selected=selected, arguments=payload)
        if sum(branch_matches) in accepted_counts:
            run_selected_action(service, selection, *scope, host=host)
            expected_calls.append(payload)
        else:
            with pytest.raises(ReadFailure, match="invalid_input"):
                run_selected_action(service, selection, *scope, host=host)
        assert calls == expected_calls
