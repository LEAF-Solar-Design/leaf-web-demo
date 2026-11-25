"""Check submitted arguments against independently enumerated conditional decisions."""
import httpx
import pytest

from test_integration_tool_service import catalog, rig
from authored_action_revocation import AuthorizedLocalActions, run_selected_action, select_action
from integration_tool_service import ReadFailure
from test_x03_action_discovery_author import LIST_ASK, action
from test_x03_action_extra_fields_author import schema_digest


TEXT = {"type": "string"}
SWITCH = {"type": "boolean"}
CASES = [
    ("Run the selected action to route this inspection to the right crew.", "route-inspection",
     {"properties": {"urgent": SWITCH, "on_call": TEXT, "crew": TEXT},
      "if": {"required": ["urgent"], "properties": {"urgent": {"const": True}}},
      "then": {"required": ["on_call"]}, "else": {"required": ["crew"]}},
     [
         ({"urgent": True, "crew": "day"}, False), ({"urgent": False, "on_call": "A"}, False),
         ({"on_call": "A"}, False), ({"urgent": True, "on_call": "A"}, True),
         ({"urgent": False, "crew": "day"}, True), ({"crew": "day"}, True),
     ]),
    ("Execute the discovered action to record the meter calibration decision.", "record-calibration",
     {"properties": {"approved": SWITCH, "reviewer": TEXT, "meter": TEXT},
      "dependentRequired": {"approved": ["reviewer", "meter"]}},
     [
         ({"approved": False, "meter": "M"}, False), ({"approved": True, "reviewer": "R"}, False),
         ({"approved": False, "meter": "M", "reviewer": "R"}, True),
         ({"reviewer": "R"}, True), ({"meter": "M"}, True),
     ]),
    ("Submit my chosen action to assign this repair and its optional follow-up.", "assign-repair",
     {"properties": {"remote": SWITCH, "room": TEXT, "site": TEXT, "follow_up": SWITCH, "owner": TEXT},
      "required": ["remote"], "if": {"properties": {"remote": {"const": True}}},
      "then": {"required": ["room"]}, "else": {"required": ["site"]},
      "dependentRequired": {"follow_up": ["owner"]}},
     [
         ({"remote": True, "room": "R", "follow_up": False}, False),
         ({"remote": False, "site": "S", "follow_up": True}, False),
         ({"remote": True, "site": "S", "owner": "A"}, False),
         ({"remote": True, "room": "R", "follow_up": False, "owner": "A"}, True),
         ({"remote": False, "site": "S"}, True),
     ]),
]


@pytest.mark.parametrize("ask,operation,rules,decisions", CASES)
def test_pinned_conditional_submission(catalog, ask, operation, rules, decisions):
    service, auth = catalog.svc, catalog.auth
    scope = auth.tenant_id, auth.session_id, auth.turn_id
    project = auth.tenant_id, auth.project_id
    schema = {"type": "object", "additionalProperties": False, **rules}
    record = action("inspection", operation, operation.replace("-", " ").title(), 3)
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

    expected_calls = []
    for payload, permitted in decisions:
        selection = select_action(service, ask, *scope, selected=selected, arguments=payload)
        if permitted:
            run_selected_action(service, selection, *scope, host=host)
            expected_calls.append(payload)
        else:
            with pytest.raises(ReadFailure, match="invalid_input"):
                run_selected_action(service, selection, *scope, host=host)
        assert calls == expected_calls
