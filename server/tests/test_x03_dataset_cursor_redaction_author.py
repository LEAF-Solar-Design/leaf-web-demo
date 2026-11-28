"""Opaque cursor bytes reach only the paginated provider."""
import json
import logging

import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List the project datasets across every page.", "status"),
    ("Which ready datasets can this drawing use?", "exception"),
    ("Show my connected datasets in catalog order.", "invalid"),
]


@pytest.mark.parametrize("ask,kind", CASES)
def test_cursor_privacy(rig, caplog, ask, kind):
    cursor = "LOCAL_CURSOR_PRIVATE_" + kind
    detail = "LOCAL_PROVIDER_PRIVATE_" + kind
    received = []
    fail = True
    def fetch(request):
        after = request.url.params.get("after")
        received.append(after)
        if after is None:
            return httpx.Response(200, json={"records": [record("first", "ready")], "nextCursor": cursor})
        assert after == cursor
        if fail:
            if kind == "status":
                return httpx.Response(503, text=detail, extensions={"reason_phrase": detail.encode()})
            if kind == "exception":
                raise httpx.ReadError(detail)
            return httpx.Response(200, json={"records": detail, "nextCursor": cursor})
        return httpx.Response(200, json={"records": [record("second", "ready")], "nextCursor": None})

    rig.svc.transport = httpx.MockTransport(fetch)
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(service.ReadFailure) as stopped:
            rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
        failure = stopped.value
        assert str(failure) == ("invalid_response" if kind == "invalid" else "incomplete_inventory")
        fail = False
        report = rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
        logging.getLogger("httpx").info("unrelated request diagnostics remain visible")
    assert "unrelated request diagnostics remain visible" in caplog.text
    assert received == [None, cursor, None, cursor]
    assert [row["dataset_ref"]["id"] for row in report["datasets"]] == ["first", "second"]
    receipt = repr((failure.args, vars(failure))) + json.dumps(report)
    for private in (cursor, detail):
        assert private not in caplog.text and private not in receipt
