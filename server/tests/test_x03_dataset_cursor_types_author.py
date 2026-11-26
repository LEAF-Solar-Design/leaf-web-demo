"""Provider continuation tokens have a string-or-null wire contract."""
import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List all of the project datasets across pages.", [True, False], "false"),
    ("Which ready datasets can this drawing read?", [0, 1.25], "0001"),
    ("Show my connected datasets in catalog order.", [[], {"after": "one"}], "Page_02-End"),
]


@pytest.mark.parametrize("ask,invalid,valid", CASES)
def test_cursor_wire_type(rig, ask, invalid, valid):
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    requests = []
    def provider(request):
        cursor = request.url.params.get("after")
        requests.append(cursor)
        if cursor is not None:
            assert cursor == valid
        return httpx.Response(200, json={
            "records": [record("first" if cursor is None else "second", "ready")],
            "nextCursor": issued if cursor is None else None})

    rig.svc.transport = httpx.MockTransport(provider)
    for issued in invalid:
        with pytest.raises(service.ReadFailure, match="^invalid_response$"):
            rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
        assert requests == [None]
        requests.clear()
    issued = valid
    report = rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert requests == [None, valid]
    assert [row["dataset_ref"]["id"] for row in report["datasets"]] == ["first", "second"]
