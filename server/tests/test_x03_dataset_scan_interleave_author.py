"""Interleave two catalog inventories at every page boundary on the same service."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from threading import Barrier, Event
from uuid import uuid4

import httpx
import pytest

from test_integration_tool_service import EncryptedGrant, rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List datasets in this drawing project.", "Show the datasets for this other project.", "project"),
    ("Show ready datasets filtered at the source.", "List stale datasets with provider-side filtering.", "filter"),
    ("Which datasets are in this snapshot?", "Show project datasets in one consistent snapshot.", "snapshot"),
]


@pytest.mark.parametrize("ask,other,dimension", CASES)
def test_independent_scans(rig, ask, other, dimension):
    west = rig.auth
    east = replace(west, project_id=str(uuid4()), session_id=str(uuid4())) if dimension == "project" else west
    if dimension == "project":
        binding = replace(rig.custody.binding, authority=east)
        grant = EncryptedGrant(binding, service.crypto.seal(b"local-test-token", binding, rig.keys))
        rig.svc.resolve = lambda tenant, session, turn: replace(
            rig.current, authority=east if session == east.session_id else west)
        rig.svc.store.load_active = lambda authority, now: grant if authority == east else rig.custody
    started = Event()
    meet = Barrier(2, timeout=10)
    seen = [[], []]
    closed = [[], []]

    def provider(request):
        cursor = request.url.params.get("after")
        if cursor is None:
            # The first request gets the first snapshot before we launch scan two.
            side = int(started.is_set())
            started.set()
        else:
            side = ["turn-west", "turn-east"].index(cursor)
        expected_project = (west, east)[side].project_id
        assert request.url.path == f"/api/v1/projects/{expected_project}/integrations/datasets"
        state = ("ready", "stale")[side] if dimension == "filter" else "ready"
        assert request.url.params.get("statuses") == (state if dimension == "filter" else None)
        seen[side].append(cursor)
        meet.wait()
        ref = ("west", "east")[side] + ("2" if cursor else "1")
        page = {"records": [record(ref, state)], "snapshotId": ("snapA", "snapB")[side],
                "nextCursor": None if cursor else ("turn-west", "turn-east")[side]}
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield json.dumps(page).encode()
            async def aclose(self):
                closed[side].append(cursor)
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Stream())

    rig.svc.transport = httpx.MockTransport(provider)
    def scan(prompt, auth):
        return rig.svc.run_authored_dataset_report(prompt, auth.tenant_id, auth.session_id, auth.turn_id, limit=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(scan, ask, west)
        assert started.wait(10)
        second = pool.submit(scan, other, east)
        for future, region in ((first, "west"), (second, "east")):
            result = future.result(timeout=30)
            assert [r["dataset_ref"]["id"] for r in result["datasets"]] == [region + "1", region + "2"]
            assert result["pages_read"] == 2
    assert seen == closed == [[None, "turn-west"], [None, "turn-east"]]
