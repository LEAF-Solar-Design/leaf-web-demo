"""Race authored edits at the conditional write boundary of a disposable revision store."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
from threading import Barrier

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


ASKS = (
    "Retitle this inspection table as Roof checks.",
    "Display four inspection rows per page.",
    "Sort the inspection amounts highest first.",
)


@pytest.mark.parametrize("first", range(3), ids=["title", "page-size", "sort"])
def test_one_successor_per_parent(save, tmp_path, first):
    path = tmp_path / "history.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE revisions (revision INTEGER PRIMARY KEY, element TEXT NOT NULL)")
    rendezvous = Barrier(2, timeout=15)
    parents = []

    def peer(request):
        parent = request.headers.get("If-Match")
        if request.method == "PUT":
            parents.append(parent)
            rendezvous.wait()
        else:
            assert request.method == "POST" and parent is None
        revision = int(parent or 0) + 1
        element = json.loads(request.content)
        with sqlite3.connect(path, timeout=10) as db:
            written = db.execute(
                "INSERT INTO revisions SELECT ?, ? WHERE "
                "COALESCE((SELECT MAX(revision) FROM revisions), 0) = ?",
                (revision, json.dumps(element), revision - 1))
            if written.rowcount != 1:
                return httpx.Response(409)
        return httpx.Response(200, json={"revision": revision, "element": element})

    save.svc.transport = httpx.MockTransport(peer)
    original = {**recipe(), "id": "inspection-table", "title": "Inspection table"}
    parent = save.save({"recipe": original})["result"]
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    edits = [deepcopy(original) for _ in ASKS]
    edits[0]["title"] = "Roof checks"
    edits[1]["props"]["row_limit"] = 4
    edits[2]["query"]["sort"] = [{"field": "amount", "direction": "desc"}]
    candidates = (first, (first + 1) % len(ASKS))
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(save.svc.edit_inspection_recipe, ASKS[i], parent, *scope)
                   for i in candidates]
        outcomes = [job.result() for job in pending]

    assert parents == ["1", "1"]
    winners = [i for i, result in enumerate(outcomes) if "result" in result]
    assert len(winners) == 1
    winner = winners[0]
    assert outcomes[1 - winner]["error"]["code"] == "conflict"
    expected = edits[candidates[winner]]
    assert outcomes[winner]["result"] == {"element": expected, "revision": 2}
    with sqlite3.connect(path) as db:
        history = [(rev, json.loads(row)) for rev, row in
                   db.execute("SELECT revision, element FROM revisions ORDER BY revision")]
    assert history == [(1, original), (2, expected)]
