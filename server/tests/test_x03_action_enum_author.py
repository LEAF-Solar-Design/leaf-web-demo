"""Check enum and const using type-sensitive JSON equality at action dispatch."""
import httpx
import pytest

from test_integration_tool_service import catalog, rig
from authored_action_revocation import AuthorizedLocalActions, run_selected_action, select_action
from integration_tool_service import ReadFailure
from test_x03_action_discovery_author import LIST_ASK, action
from test_x03_action_extra_fields_author import closed, schema_digest


def json_equal(left, right):
    if type(left) in (float, int) and type(right) in (float, int):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(json_equal(left[k], right[k]) for k in left)
    return left == right


CASES = [
    ("Run the selected action to record the meter decision.", "meter-decision",
     {"enum": [False, 1]}, [0, True, "1", False, 1.0]),
    ("Execute the discovered action to confirm the panel flag.", "panel-flag",
     {"const": True}, [1, 1.0, False, True]),
    ("Submit my chosen action to record the inspection flags and grade.", "inspection-grade",
     {"const": {"flags": [False, 1], "grade": 0}},
     [{"flags": [0, True], "grade": 0}, {"grade": False, "flags": [False, 1]},
      {"grade": 0.0, "flags": [False, 1.0]}]),
]


@pytest.mark.parametrize("ask,operation,rule,samples", CASES)
def test_pinned_json_equality(catalog, ask, operation, rule, samples):
    service, auth = catalog.svc, catalog.auth
    scope = auth.tenant_id, auth.session_id, auth.turn_id
    project = auth.tenant_id, auth.project_id
    schema = closed(value=rule)
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
    allowed = rule["enum"] if "enum" in rule else [rule["const"]]
    expected = []
    for value in samples:
        payload = {"value": value}
        chosen = select_action(service, ask, *scope, selected=selected, arguments=payload)
        if any(json_equal(value, option) for option in allowed):
            run_selected_action(service, chosen, *scope, host=host)
            expected.append(payload)
        else:
            with pytest.raises(ReadFailure, match="invalid_input"):
                run_selected_action(service, chosen, *scope, host=host)
        assert json_equal(calls, expected)
