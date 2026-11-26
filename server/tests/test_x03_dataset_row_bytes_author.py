"""Budget compact UTF-8 provider records independently of the report projection."""
from copy import deepcopy
from json.encoder import encode_basestring

import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List datasets in my project with columns title.", "title", "x" * 40),
    ("Show all ready datasets and include columns status.", "title", "\u5c4b\U0001f331" * 10),
    ("Which datasets can I read? Return columns title and fields.", "label", "Nested sensor"),
]


def compact_size(value):
    # Count scalar UTF-8 bytes, literal separators, and container delimiters.
    if isinstance(value, (dict, list)):
        sizes = ([compact_size(k) + 1 + compact_size(v) for k, v in value.items()]
                 if isinstance(value, dict) else [compact_size(item) for item in value])
        return 2 + sum(sizes) + max(0, len(sizes) - 1)
    if isinstance(value, str):
        return len(encode_basestring(value).encode("utf-8"))
    if value is None:
        return 4
    return len(str(value).lower())


@pytest.mark.parametrize("ask,location,value", CASES)
def test_provider_row_byte_budgets(rig, monkeypatch, ask, location, value):
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    rows = [deepcopy(record(name, "ready")) for name in ("a", "b")]
    target = rows[1] if location == "title" else rows[1]["fields"][0]
    target[location] = value
    sizes = [compact_size(row) for row in rows]
    assert sizes[1] > sizes[0]
    seen = []
    def provider(request):
        after = request.url.params.get("after")
        seen.append(after)
        return httpx.Response(200, json={"records": [rows[int(after is not None)]],
                                    "nextCursor": None if after else "next"})
    rig.svc.transport = httpx.MockTransport(provider)
    def scan():
        return rig.svc.run_authored_dataset_report(ask, *scope, limit=1)

    for row_limit, total_limit in ((max(sizes) - 1, sum(sizes)), (max(sizes), sum(sizes) - 1)):
        monkeypatch.setattr(service, "DATASET_ROW_BYTE_BUDGET", row_limit)
        monkeypatch.setattr(service, "DATASET_TOTAL_BYTE_BUDGET", total_limit)
        with pytest.raises(service.IncompleteInventory) as failure:
            scan()
        assert failure.value.pages_read == 1 and seen == [None, "next"]
        seen.clear()
    monkeypatch.setattr(service, "DATASET_TOTAL_BYTE_BUDGET", sum(sizes))
    report = scan()
    assert [r["dataset_ref"]["id"] for r in report["datasets"]] == ["a", "b"]
    assert report["pages_read"] == 2 and seen == [None, "next"]
