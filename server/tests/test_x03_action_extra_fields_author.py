"""Exercise structured local arguments through authored selection and dispatch."""
from copy import deepcopy
from hashlib import sha256
import json

import httpx
import pytest

from test_integration_tool_service import catalog, rig
from authored_action_revocation import AuthorizedLocalActions, run_selected_action, select_action
from integration_tool_service import ReadFailure
from test_x03_action_discovery_author import action


def closed(**properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def many(items):
    return {"type": "array", "items": items}


NAME = {"type": "string"}
CASES = [
    ("Run the selected action to file this roof inspection.",
     closed(roof=closed(zone=NAME)), {"roof": {"zone": "north"}}, ("roof",)),
    ("Execute the discovered action to schedule these meter checks.",
     closed(meters=many(closed(name=NAME))), {"meters": [{"name": "A"}, {"name": "B"}]},
     ("meters", 1)),
    ("Submit my chosen action to record the defects for each beam.",
     closed(beams=many(closed(defects=many(closed(code=NAME))))),
     {"beams": [{"defects": [{"code": "joint"}]}]}, ("beams", 0, "defects", 0)),
]


def schema_digest(schema):
    return sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("ask,schema,valid,extra_path", CASES)
def test_pinned_structured_arguments(catalog, ask, schema, valid, extra_path):
    service, auth = catalog.svc, catalog.auth
    scope = auth.tenant_id, auth.session_id, auth.turn_id
    record = action("inspection", "record-finding", "Record finding", 3)
    record["operation_ref"]["digest"] = schema_digest(schema)
    service.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"actions": [record], "next_cursor": None}))
    discovered, = service.run_authored_action_report(
        "Which action can record a finding?", *scope, include_form_digest=True)["actions"]
    pins = {key: discovered[key] for key in ("connection_id", "connector_ref", "operation_ref")}
    calls = []
    host = AuthorizedLocalActions()
    project = auth.tenant_id, auth.project_id
    host.register(*project, pins, calls.append, form=record["form"], argument_schema=schema)
    host.set_role_grants(*project, "editor", [pins])
    host.assign_roles(*project, auth.subject, ["editor"])

    def select(values):
        return select_action(service, ask, *scope, selected=discovered, arguments=values)

    invalid = deepcopy(valid)
    node = invalid
    for part in extra_path:
        node = node[part]
    node["unlisted"] = True
    with pytest.raises(ReadFailure, match="invalid_input"):
        run_selected_action(service, select(invalid), *scope, host=host)
    assert calls == []

    chosen = select(valid)
    changed = deepcopy(schema)
    changed["properties"]["new_field"] = NAME
    with pytest.raises(ValueError, match="pinned operation digest"):
        host.register(*project, pins, calls.append, form=record["form"], argument_schema=changed)
    run_selected_action(service, chosen, *scope, host=host)
    assert calls == [valid]

    record["operation_ref"]["digest"] = schema_digest(changed)
    with pytest.raises(ReadFailure, match="denied"):
        run_selected_action(service, chosen, *scope, host=host)
    assert calls == [valid]
