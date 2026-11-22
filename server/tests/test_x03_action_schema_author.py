"""Recheck the discovered action form and validate submitted arguments at dispatch."""
from copy import deepcopy

import httpx
import pytest

from test_integration_tool_service import catalog, rig
from test_x03_action_discovery_author import action
from integration_tool_service import ReadFailure
from authored_action_revocation import AuthorizedLocalActions, select_action, run_selected_action


CASES = [
    ("Run the selected action using these inspection fields.",
     {"kind": "text", "min_length": 2, "max_length": 5}, "Roof", "X"),
    ("Execute this discovered action with the entered quantity.",
     {"kind": "number", "integer": True, "minimum": 0, "maximum": 8}, 0, True),
    ("Submit my chosen action with the selected status.",
     {"kind": "choice", "choices": [{"label": "Ready", "value": "ready"}]}, "ready", "unknown"),
]


@pytest.mark.parametrize("ask,field,good,bad", CASES)
def test_schema_bound_submission(catalog, ask, field, good, bad):
    service, auth = catalog.svc, catalog.auth
    scope = auth.tenant_id, auth.session_id, auth.turn_id
    record = action("survey", "record-finding", "Record finding", 3)
    record["form"]["fields"] = [
        {**field, "id": "input", "label": "Input", "required": True, "nullable": False}]
    visible = [record]
    service.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"actions": visible, "next_cursor": None}))
    selected = service.run_authored_action_report(
        "Which action can record a finding?", *scope, include_form_digest=True)["actions"][0]
    pins = {k: record[k] for k in ("connection_id", "connector_ref", "operation_ref")}
    calls = []
    host = AuthorizedLocalActions()
    host.register(auth.tenant_id, auth.project_id, pins, calls.append, form=record["form"])
    host.set_role_grants(auth.tenant_id, auth.project_id, "editor", [pins])
    host.assign_roles(auth.tenant_id, auth.project_id, auth.subject, ["editor"])

    def select(args):
        return select_action(service, ask, *scope, selected=selected, arguments=args)

    args = {"input": good}
    accepted = select(args)
    args.clear()
    run_selected_action(service, accepted, *scope, host=host)
    assert calls == [{"input": good}]
    calls.clear()
    for invalid in ({"input": bad}, {}, {"input": None}, {"input": good, "extra": True}):
        with pytest.raises(ReadFailure, match="invalid_input"):
            run_selected_action(service, select(invalid), *scope, host=host)

    updated = deepcopy(record)
    updated["form"]["fields"].append({"id": "flag", "label": "Flag", "kind": "boolean",
                                      "required": False, "nullable": True})
    visible[:] = [updated]
    with pytest.raises(ReadFailure, match="denied"):
        run_selected_action(service, accepted, *scope, host=host)
    # The catalog replica can also lag the installed adapter.
    visible[:] = [record]
    host.register(auth.tenant_id, auth.project_id, pins, calls.append, form=updated["form"])
    with pytest.raises(ReadFailure, match="denied"):
        run_selected_action(service, accepted, *scope, host=host)
    assert calls == []
