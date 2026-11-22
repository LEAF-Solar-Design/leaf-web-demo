"""Studio discovers versioned action descriptions through one project-scoped GET."""
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest

from test_integration_tool_service import catalog, rig


@pytest.mark.parametrize("ask,connection,operation,version", [
    ("Which inspection action can file a roof defect?", "roof", "file-defect", 2),
    ("Show the pinned action for scheduling a meter check.", "meter", "schedule-check", 7),
    ("Which connected action can reopen a beam inspection?", "steel", "reopen-inspection", 4),
])
def test_scoped_action_discovery(catalog, ask, connection, operation, version):
    def ref(name):
        return {"id": name, "version": version, "digest": sha256(f"{name}:{version}".encode()).hexdigest()}

    connector_ref, operation_ref = ref(connection), ref(operation)
    action = {"connection_id": connection, "connection_title": connection.title(),
              "connector_ref": connector_ref, "operation_ref": operation_ref, "title": operation,
              "form": {"submit_label": "Submit", "fields": []}}
    project = catalog.auth.project_id
    foreign_project = str(uuid4())
    foreign_action = {**action, "connection_id": "another-project"}
    requests = []

    def transport(request):
        requests.append((request.method, request.url.path, request.content))
        pages = {f"/api/v1/projects/{project}/integrations/action-catalog": [action],
                 f"/api/v1/projects/{foreign_project}/integrations/action-catalog": [foreign_action]}
        return httpx.Response(200, json={"actions": pages[request.url.path], "next_cursor": None})

    catalog.svc.transport = httpx.MockTransport(transport)
    page = catalog.catalog_call({"limit": 1})["result"]
    assert page == {"actions": [action], "next_cursor": None}, ask
    assert page["actions"][0]["connector_ref"] == connector_ref
    assert page["actions"][0]["operation_ref"] == operation_ref
    assert catalog.catalog_call({"operation_ref": foreign_action["operation_ref"],
                                 "connection_id": foreign_action["connection_id"]})["error"]["code"] == "invalid_input"
    # A valid turn grant for one project cannot fetch the other project's catalog.
    catalog.current = replace(catalog.current, authority=replace(catalog.auth, project_id=foreign_project))
    assert catalog.catalog_call()["error"]["code"] == "denied"
    assert requests == [("GET", f"/api/v1/projects/{project}/integrations/action-catalog", b"")]
    assert [gate[0][3] for gate in catalog.gates] == ["read_integration_data"]
