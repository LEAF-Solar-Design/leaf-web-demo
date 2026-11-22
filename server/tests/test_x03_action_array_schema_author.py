"""Pinned array arguments through discovery, authored selection and dispatch."""
import httpx
import pytest

from test_integration_tool_service import catalog, rig
from authored_action_revocation import AuthorizedLocalActions, run_selected_action, select_action
from integration_tool_service import ReadFailure
from test_x03_action_discovery_author import LIST_ASK, action
from test_x03_action_extra_fields_author import closed, schema_digest


CASES = [
    ("Run the selected action to tag the panel and record its row number.", "tag-panel",
     {"prefixItems": [{"type": "string"}, {"type": "integer"}], "items": False, "minItems": 2},
     [([3, "A"], False), (["A"], False), (["A", 3, 4], False), (["A", 0], True)]),
    ("Execute the discovered action to book a crew with one or two leads.", "book-crew",
     {"contains": {"const": "lead"}, "minContains": 1, "maxContains": 2},
     [(["crew"], False), (["lead"] * 3, False), (["crew", "lead"], True), (["lead", "lead"], True)]),
    ("Submit my chosen action to record distinct inspection readings.", "record-readings",
     {"uniqueItems": True},
     [([1, 1.0], False), ([{"a": 1, "b": 2}, {"b": 2, "a": 1}], False),
      ([True, 1], True), ([[1, 2], [2, 1]], True)]),
]


@pytest.mark.parametrize("ask,operation,rules,decisions", CASES)
def test_pinned_array_submission(catalog, ask, operation, rules, decisions):
    service, auth = catalog.svc, catalog.auth
    scope = auth.tenant_id, auth.session_id, auth.turn_id
    project = auth.tenant_id, auth.project_id
    schema = closed(entries={"type": "array", **rules})
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
    for entries, permitted in decisions:
        payload = {"entries": entries}
        selection = select_action(service, ask, *scope, selected=selected, arguments=payload)
        if permitted:
            run_selected_action(service, selection, *scope, host=host)
            expected_calls.append(payload)
        else:
            with pytest.raises(ReadFailure, match="invalid_input"):
                run_selected_action(service, selection, *scope, host=host)
        assert calls == expected_calls
