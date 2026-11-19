"""Check pinned numeric submissions against an independent decimal oracle."""
from decimal import Decimal

import httpx
import pytest

from test_integration_tool_service import catalog, rig
from authored_action_revocation import AuthorizedLocalActions, run_selected_action, select_action
from integration_tool_service import ReadFailure
from test_x03_action_discovery_author import LIST_ASK, action
from test_x03_action_extra_fields_author import closed, schema_digest


CASES = [
    ("Run the selected action to record panel spacing in tenths.", "panel-spacing",
     "0.1", "0", "1", ["0", "1", "0.31", "0.3", "0.9"]),
    ("Execute the discovered action to set the signed meter offset.", "meter-offset",
     "0.01", "-0.30", "0.30", ["-0.3", "0.3", "-0.125", "-0.29", "0"]),
    ("Submit my chosen action to adjust the beam clearance in twentieths.", "beam-clearance",
     "0.05", "0.10", "0.35", ["0.1", "0.35", "0.151", "0.15", "0.25"]),
]


@pytest.mark.parametrize("ask,operation,step,low,high,samples", CASES)
def test_pinned_decimal_submission(catalog, ask, operation, step, low, high, samples):
    service, auth = catalog.svc, catalog.auth
    scope = auth.tenant_id, auth.session_id, auth.turn_id
    project = auth.tenant_id, auth.project_id
    schema = closed(value={"type": "number", "multipleOf": float(step),
                           "exclusiveMinimum": float(low), "exclusiveMaximum": float(high)})
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
    expected = []
    for literal in samples:
        value = Decimal(literal)
        payload = {"value": float(literal)}
        chosen = select_action(service, ask, *scope, selected=selected, arguments=payload)
        if Decimal(low) < value < Decimal(high) and value % Decimal(step) == 0:
            run_selected_action(service, chosen, *scope, host=host)
            expected.append(payload)
        else:
            with pytest.raises(ReadFailure, match="invalid_input"):
                run_selected_action(service, chosen, *scope, host=host)
        assert calls == expected


def test_exact_number_limits():
    from authored_action_revocation import _exact_json_number

    for literal in ("NaN", "Infinity", "-Infinity", "1e-769", "1e309"):
        with pytest.raises(ValueError):
            _exact_json_number(literal)
    assert _exact_json_number("1e-768").denominator == 10**768
