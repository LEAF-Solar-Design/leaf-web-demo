"""Run offline-authored catalog reports through the scoped integration service."""
import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import CASES, record
import tools_fallback


@pytest.mark.parametrize("ask,names,statuses", CASES)
def test_authored_dataset_report(rig, ask, names, statuses):
    # Author through the product dispatcher. Service authors the same source when run.
    package, _, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "project_dataset_discovery"
    visited = []
    def fetch(request):
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/projects/{rig.auth.project_id}/integrations/datasets"
        after = request.url.params.get("after")
        index = int(after[1:]) if after else 0
        visited.append(after)
        return httpx.Response(200, json={"records": [record(names[index], statuses[index])],
                "nextCursor": f"p{index + 1}" if index + 1 < len(names) else None})

    rig.svc.transport = httpx.MockTransport(fetch)
    result = rig.svc.run_authored_dataset_report(ask, rig.auth.tenant_id,
                                                 rig.auth.session_id, rig.auth.turn_id, limit=1)
    expected = list(zip(names, statuses))
    if ask == CASES[2][0]:
        expected = expected[:2]
    assert [(row["dataset_ref"]["id"], row["status"]) for row in result["datasets"]] == expected
    assert all(set(row) == {"dataset_ref", "title", "status", "needs_attention"} for row in result["datasets"])
    assert [row["needs_attention"] for row in result["datasets"]] == [
        status in ("stale", "unavailable") for _, status in expected]
    assert result["datasets_scanned"] == result["pages_read"] == len(names)
    assert visited == [None, *(f"p{i}" for i in range(1, len(names)))]
    assert all(args[3] == "read_integration_data" for args, _ in rig.gates)


def test_no_authored_report_on_denied_page(rig):
    rig.decision = "deny"
    with pytest.raises(service.ReadFailure, match="denied"):
        rig.svc.run_authored_dataset_report(CASES[0][0], rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id)
    assert not rig.requests


def test_authored_report_binds_each_page_fetch_to_starting_authority(rig):
    from dataclasses import replace
    from uuid import uuid4

    resolve = rig.svc.resolve
    other = replace(rig.current, authority=replace(rig.auth, project_id=str(uuid4())))
    def rebound(*headers):
        current = resolve(*headers)
        return other if rig.resolutions == 3 else current

    rig.svc.resolve = rebound
    with pytest.raises(service.ReadFailure, match="denied"):
        rig.svc.run_authored_dataset_report(CASES[0][0], rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id)
    assert not rig.requests and not rig.gates
