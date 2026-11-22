"""Use benign text patterns and independently specified submit decisions."""
from copy import deepcopy

import httpx
import pytest
from jsonschema.exceptions import SchemaError

from test_integration_tool_service import catalog, rig
from authored_action_revocation import AuthorizedLocalActions, run_selected_action, select_action
from integration_tool_service import ReadFailure
from test_x03_action_discovery_author import LIST_ASK, action
from test_x03_action_extra_fields_author import closed, schema_digest


CASES = [
    ("Run the selected action to register a panel tag.", "panel-tag",
     closed(tag={"type": "string", "pattern": "^P-[0-9]{2}$"}),
     [{"tag": "P-07"}], [{"tag": "P-7"}, {"tag": "XP-07"}]),
    ("Execute the discovered action to save numbered meter readings.", "meter-readings",
     {"type": "object", "patternProperties": {"^meter_[0-9]+$": {"type": "number"}},
      "additionalProperties": False},
     [{"meter_1": 0, "meter_22": 3}], [{"meter_x": 1}, {"meter_1": "3"}]),
    ("Submit my chosen action to record the beam labels.", "beam-labels",
     {"type": "object", "patternProperties": {"^beam_": {"type": "string", "pattern": "north"}},
      "additionalProperties": False},
     [{"beam_end": "upper north face"}], [{"beam_end": "south"}, {"girder_end": "north"}]),
]


@pytest.mark.parametrize("ask,operation,schema,valid,invalid", CASES)
def test_pinned_patterns(catalog, ask, operation, schema, valid, invalid):
    service, auth = catalog.svc, catalog.auth
    scope = auth.tenant_id, auth.session_id, auth.turn_id
    project = auth.tenant_id, auth.project_id
    record = action("inspection", operation, operation.replace("-", " ").title(), 3)
    service.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"actions": [record], "next_cursor": None}))
    host = AuthorizedLocalActions()
    calls = []

    def discover(definition):
        record["operation_ref"]["digest"] = schema_digest(definition)
        selected, = service.run_authored_action_report(LIST_ASK, *scope, include_form_digest=True)["actions"]
        pins = {k: selected[k] for k in ("connection_id", "connector_ref", "operation_ref")}
        host.set_role_grants(*project, "editor", [pins])
        host.assign_roles(*project, auth.subject, ["editor"])
        return selected, pins

    def dispatch(selected, payload):
        chosen = select_action(service, ask, *scope, selected=selected, arguments=payload)
        run_selected_action(service, chosen, *scope, host=host)

    malformed = deepcopy(schema)
    if "patternProperties" in schema:
        malformed["patternProperties"] = {"[": {"type": "string"}}
    else:
        malformed["properties"]["tag"]["pattern"] = "["
    selected, pins = discover(malformed)
    with pytest.raises(SchemaError):
        host.register(*project, pins, calls.append, argument_schema=malformed)
    with pytest.raises(ReadFailure, match="denied"):
        dispatch(selected, valid[0])
    selected, pins = discover(schema)
    host.register(*project, pins, calls.append, argument_schema=schema)
    for payload in invalid:
        with pytest.raises(ReadFailure, match="invalid_input"):
            dispatch(selected, payload)
    assert calls == []
    for payload in valid:
        dispatch(selected, payload)
    assert calls == valid
