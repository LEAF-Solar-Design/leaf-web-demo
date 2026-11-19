"""Only current, scoped grants and exact catalog versions may reach an action handler."""
from dataclasses import replace

import httpx
import pytest
from test_integration_tool_service import catalog, rig
from test_x03_action_discovery_author import action
from integration_tool_service import ReadFailure
import tools_fallback


CASES = [
    ("Run the selected action if my project permission is still current.", "role"),
    ("Execute this discovered action only while its pinned version is available.", "version"),
    ("Submit the chosen action after rechecking access in this project.", "scope"),
]


@pytest.mark.parametrize("ask,change", CASES)
def test_revoked_between_discovery_and_dispatch(catalog, ask, change):
    assert tools_fallback.author_tool(ask)[0]["engine_op"] == "guarded_action_selection"
    from authored_action_revocation import AuthorizedLocalActions, select_action, run_selected_action

    auth = catalog.auth
    selected = action("survey", "record-finding", "Record finding", 4)
    visible = [selected]
    catalog.svc.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"actions": visible, "next_cursor": None})
    )
    service = catalog.svc
    scope = auth.tenant_id, auth.session_id, auth.turn_id
    discovered = service.run_authored_action_report(
        "Which action can record a finding?", *scope)
    assert discovered["actions_scanned"] == 1 and len(discovered["actions"]) == 1
    selection = select_action(service, ask, *scope, selected=discovered["actions"][0])
    pins = {k: selected[k] for k in ("connection_id", "connector_ref", "operation_ref")}
    calls = []
    host = AuthorizedLocalActions()
    host.register(auth.tenant_id, auth.project_id, pins, lambda: calls.append("recorded") or "receipt")
    host.set_role_grants(auth.tenant_id, auth.project_id, "inspector", [pins])
    host.assign_roles(auth.tenant_id, auth.project_id, auth.subject, ["inspector"])
    # This control uses the same discovered selection and actual local grant boundary.
    assert run_selected_action(service, selection, *scope, host=host) == "receipt"
    calls.clear()
    if change == "role":
        host.assign_roles(auth.tenant_id, auth.project_id, auth.subject, [])
    elif change == "version":
        visible[:] = [action("survey", "record-finding", "Record finding", 5)]
    else:
        catalog.current = replace(catalog.current, authority=replace(auth, subject="different-viewer"))
    with pytest.raises(ReadFailure, match="denied"):
        run_selected_action(service, selection, *scope, host=host)
    assert calls == []
    if change == "version":
        # A lagging read replica can still advertise v4 after the action owner upgrades.
        updated = {k: visible[0][k] for k in pins}
        host.register(auth.tenant_id, auth.project_id, updated, lambda: calls.append("v5"))
        visible[:] = [selected]
        with pytest.raises(ReadFailure, match="denied"):
            run_selected_action(service, selection, *scope, host=host)
        assert calls == []
