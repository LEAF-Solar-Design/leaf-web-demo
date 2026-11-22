"""Exercise scoped action discovery from ordinary authoring through the sandbox."""
from hashlib import sha256

import httpx
import pytest

from test_integration_tool_service import catalog, rig
import tools_fallback


CASES = [
    ("Which inspection action can file a roof defect?", "roof", "file-defect", "File roof defect", 2),
    ("Show the pinned action for scheduling a meter check.", "meter", "schedule-check", "Schedule meter check", 7),
    ("Which connected action can reopen a beam inspection?", "steel", "reopen-inspection", "Reopen beam inspection", 4),
]
LIST_ASK = "Show which connected actions my drawing checker can call before I choose one."


def action(connection, operation, title, version):
    def ref(name):
        return {"id": name, "version": version, "digest": sha256(f"{name}:{version}".encode()).hexdigest()}

    return {"connection_id": connection, "connection_title": connection.title(),
            "connector_ref": ref(connection), "operation_ref": ref(operation), "title": title,
            "form": {"submit_label": "Submit", "fields": []}}


@pytest.mark.parametrize("case", [0, 1, 2, None])
def test_authored_action_catalog(catalog, case):
    ask = LIST_ASK if case is None else CASES[case][0]
    package, _, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "project_action_discovery"
    actions = [action(*entry[1:]) for entry in CASES]
    requests = []
    def fetch(request):
        requests.append(request)
        after = request.url.params.get("after")
        index = int(after[1:]) if after else 0
        return httpx.Response(200, json={"actions": [actions[index]],
                                        "next_cursor": f"p{index + 1}" if index < 2 else None})
    catalog.svc.transport = httpx.MockTransport(fetch)
    catalog.svc.resolve = lambda *args: pytest.fail("Action discovery requires project edit authority")

    report = catalog.svc.run_authored_action_report(ask, catalog.auth.tenant_id,
                                                    catalog.auth.session_id, catalog.auth.turn_id, limit=1)
    assert report["actions_scanned"] == report["pages_read"] == 3
    expected = actions if case is None else [actions[case]]
    assert report["actions"] == [{k: v for k, v in row.items() if k != "form"} for row in expected]
    assert [r.url.params.get("after") for r in requests] == [None, "p1", "p2"]
    assert all(r.method == "GET" and r.content == b"" and
               r.url.path == f"/api/v1/projects/{catalog.auth.project_id}/integrations/action-catalog" for r in requests)
    assert len(catalog.gates) == 3
    assert all(args[3] == "read_integration_data" and args[4]["operation"] == "action_catalog"
               for args, _ in catalog.gates)
