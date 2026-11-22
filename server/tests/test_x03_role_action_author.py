"""Compare two project role assignments through the ordinary author and read gate."""
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from test_integration_tool_service import catalog, rig
from test_x03_action_discovery_author import action
from integration_tool_service import ReadFailure
import tools_fallback


CASES = [
    "Compare the actions available to the inspector and the project manager without running any of them.",
    "Which connected actions do both the inspector and project manager share?",
    "Show the exclusive actions for the project manager versus the inspector.",
]


def setup_catalog(catalog):
    entries = [action("site", name, name, version) for name, version in (
        ("report", 2), ("triage", 1), ("approve", 8), ("archive", 4))]
    def reference(entry):
        return {key: entry[key] for key in ("connection_id", "connector_ref", "operation_ref")}
    refs = list(map(reference, entries))
    policy = {"tenant_id": catalog.auth.tenant_id, "project_id": catalog.auth.project_id,
              "subject": catalog.auth.subject, "roles": {"inspector": refs[:2], "project manager": refs[1:3]}}
    reads = []
    def fetch(request):
        reads.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json={"actions": entries, "next_cursor": None})
    catalog.svc.transport = httpx.MockTransport(fetch)
    return policy, refs, reads


@pytest.mark.parametrize("ask", CASES)
def test_role_action_comparison(catalog, ask):
    assert tools_fallback.author_tool(ask)[0]["engine_op"] == "role_action_comparison"
    policy, refs, reads = setup_catalog(catalog)
    report = catalog.svc.compare_role_actions(ask, catalog.auth.tenant_id, catalog.auth.session_id,
                                              catalog.auth.turn_id, policy=policy)
    if ask != CASES[2]:
        assert report["shared"] == [refs[1]]
    else:
        assert "shared" not in report
    if ask != CASES[1]:
        assert report["exclusive"] == {"inspector": [refs[0]], "project manager": [refs[2]]}
    else:
        assert "exclusive" not in report
    assert report["project_id"] == catalog.auth.project_id
    assert refs[3] not in report.get("shared", []) and all(refs[3] not in v for v in report.get("exclusive", {}).values())
    assert reads == [("GET", f"/api/v1/projects/{catalog.auth.project_id}/integrations/action-catalog", b"")]
    assert [args[3] for args, kw in catalog.gates] == ["read_integration_data"]


@pytest.mark.parametrize("damage", ["foreign_project", "foreign_subject", "foreign_operation", "wrong_version"])
def test_reject_wrong_scope_or_unavailable_role_grant(catalog, damage):
    policy, refs, reads = setup_catalog(catalog)
    if damage == "foreign_project":
        policy["project_id"] = str(uuid4())
    elif damage == "foreign_subject":
        policy["subject"] = "another-user"
    else:
        altered = deepcopy(refs[0])
        if damage == "foreign_operation":
            altered["operation_ref"]["id"] = "admin-only"
        else:
            altered["operation_ref"]["version"] += 1
        policy["roles"]["inspector"].append(altered)
    with pytest.raises(ReadFailure) as result:
        catalog.svc.compare_role_actions(CASES[0], catalog.auth.tenant_id, catalog.auth.session_id,
                                          catalog.auth.turn_id, policy=policy)
    assert str(result.value) == "denied"
    assert all(method == "GET" for method, path, body in reads)
