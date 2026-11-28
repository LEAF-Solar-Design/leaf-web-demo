"""Keep same-named action validators bound to their project and exact revision."""
from dataclasses import replace
from uuid import uuid4

import pytest

from test_integration_tool_service import EncryptedGrant, TOKEN, catalog, rig, service
from authored_action_revocation import AuthorizedLocalActions, run_selected_action, select_action
from integration_tool_service import ReadFailure
from test_x03_action_discovery_author import LIST_ASK, action
from test_x03_action_extra_fields_author import closed, schema_digest


CASES = [
    ("Run the selected action to record the panel inspection grade.", "panel-grade", ("A", "B", "C")),
    ("Submit my chosen action to set the beam survey count.", "beam-count", (2, 5, 8)),
    ("Execute the discovered action to save the meter input unit.", "meter-unit", ("V", "A", "W")),
]


@pytest.mark.parametrize("ask,operation,values", CASES)
def test_pinned_validator_reuse(catalog, ask, operation, values):
    host = AuthorizedLocalActions()
    original = catalog.auth
    projects = [original, replace(original, project_id=str(uuid4()))]
    scope = original.tenant_id, original.session_id, original.turn_id
    calls = []
    records = {}

    def activate(project):
        authority = projects[project]
        schema_binding = replace(catalog.custody.binding, authority=authority)
        catalog.custody = EncryptedGrant(schema_binding, service.crypto.seal(TOKEN, schema_binding, catalog.keys))
        catalog.current = replace(catalog.current, authority=authority)
        catalog.response = {"actions": [records[project]], "next_cursor": None}
        selected, = catalog.svc.run_authored_action_report(LIST_ASK, *scope, include_form_digest=True)["actions"]
        return selected

    def install(project, revision, value):
        authority = projects[project]
        schema = closed(value={"const": value})
        record = action("survey", operation, operation.title(), revision)
        record["operation_ref"]["digest"] = schema_digest(schema)
        records[project] = record
        selected = activate(project)
        pins = {key: selected[key] for key in ("connection_id", "connector_ref", "operation_ref")}
        owner = authority.tenant_id, authority.project_id
        host.register(*owner, pins, lambda payload: calls.append((project, revision, payload)), argument_schema=schema)
        host.set_role_grants(*owner, "editor", [pins])
        host.assign_roles(*owner, authority.subject, ["editor"])
        return selected

    def choose(selected, value):
        return select_action(catalog.svc, ask, *scope, selected=selected, arguments={"value": value})

    def run(selection):
        run_selected_action(catalog.svc, selection, *scope, host=host)

    first, current, other = values
    stale = choose(install(0, 1, first), first)
    run(stale)
    expected = [(0, 1, {"value": first})]
    assert calls == expected
    install(0, 2, current)
    with pytest.raises(ReadFailure, match="^denied$"):
        run(stale)
    assert calls == expected
    install(1, 2, other)
    for project in (0, 1, 1, 0):
        selected = activate(project)
        wanted = (current, other)[project]
        for rejected in (first, (other, current)[project]):
            with pytest.raises(ReadFailure, match="^invalid_input$"):
                run(choose(selected, rejected))
            assert calls == expected
        run(choose(selected, wanted))
        expected.append((project, 2, {"value": wanted}))
        assert calls == expected
