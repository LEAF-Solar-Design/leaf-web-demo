"""One deadline covers every page, response cleanup, and the final authored report."""
import json

import httpx
import pytest
import tool_loader

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List every dataset in this drawing project.", "before", {}, []),
    ("Show the ready datasets connected to this project.", "during", {}, [None]),
    ("Which project datasets can my checker read?", "after", {"page_size": 2}, [None, "p2"]),
]


@pytest.mark.parametrize("ask,phase,options,expected", CASES)
def test_dataset_report_deadline(rig, monkeypatch, ask, phase, options, expected):
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    deadline = 1005.0
    now = deadline if phase == "before" else 1001.0
    rig.svc.clock = lambda: now
    requests, closed = [], []
    crossed = False
    def provider(request):
        nonlocal now
        assert now < deadline
        cursor = request.url.params.get("after")
        requests.append(cursor)
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal now
                yield b'{"records":['
                if crossed and phase == "during":
                    now = deadline + 0.25
                row = record("west" if cursor else "east", "ready")
                yield json.dumps(row).encode() + b'],"nextCursor":' + (b'null}' if cursor else b'"p2"}')

            async def aclose(self):
                nonlocal now
                closed.append(cursor)
                if crossed and phase == "after" and cursor:
                    now = deadline

        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Stream())

    rig.svc.transport = httpx.MockTransport(provider)
    crossed = True
    with pytest.raises(service.ReadFailure, match="^timeout$"):
        rig.svc.run_authored_dataset_report(ask, *scope, limit=1, deadline=deadline, **options)
    assert requests == closed == expected
    crossed = False
    requests.clear()
    closed.clear()
    now = deadline - 0.5
    report = rig.svc.run_authored_dataset_report(ask, *scope, limit=1, deadline=deadline, **options)
    assert requests == closed == [None, "p2"]
    assert [row["dataset_ref"]["id"] for row in report["datasets"]] == ["east", "west"]
    if phase == "after":
        run = tool_loader._run_source_in_sandbox
        def finish_at_deadline(*args, **kwargs):
            nonlocal now
            result = run(*args, **kwargs)
            now = deadline
            return result
        monkeypatch.setattr(tool_loader, "_run_source_in_sandbox", finish_at_deadline)
        with pytest.raises(service.ReadFailure, match="^timeout$"):
            rig.svc.run_authored_dataset_report(ask, *scope, limit=1, deadline=deadline, **options)
