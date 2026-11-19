"""Offline natural-language recipe edits through the guarded Studio save rail."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


ASKS = [
    "Rename my inspection table to Roof punch list without overwriting another editor.",
    "Show two inspection rows per page and warn me if the table changed.",
    "Sort the inspection amounts highest first unless another editor saved first.",
]


@pytest.mark.parametrize("ask,field,expected", [
    (ASKS[0], "title", "Roof punch list"),
    (ASKS[1], "row_limit", 2),
    (ASKS[2], "sort", [{"field": "amount", "direction": "desc"}]),
])
def test_inspection_recipe_author(save, tmp_path, ask, field, expected):
    original = {**recipe(), "id": "inspection-table", "title": "Inspection table"}
    wanted = deepcopy(original)
    target = {"title": wanted, "row_limit": wanted["props"], "sort": wanted["query"]}[field]
    target[field] = expected
    scope = f"/api/v1/projects/{save.auth.project_id}/integrations/elements"
    writes = []
    with closing(sqlite3.connect(tmp_path / "elements.sqlite")) as db:
        db.execute("CREATE TABLE history(revision INTEGER PRIMARY KEY, content TEXT NOT NULL)")
        def peer(request):
            version = request.headers.get("If-Match")
            writes.append((request.method, request.url.path, version))
            current = db.execute("SELECT coalesce(max(revision),0) FROM history").fetchone()[0]
            if (request.method, request.url.path, version) == ("POST", scope, None):
                accepted = current == 0
            else:
                accepted = ((request.method, request.url.path) == ("PUT", scope + "/inspection-table")
                            and version == str(current))
            if not accepted:
                return httpx.Response(409)
            element = json.loads(request.content)
            with db:
                db.execute("INSERT INTO history VALUES (?,?)", (current + 1, json.dumps(element)))
            return httpx.Response(200, json={"revision": current + 1, "element": element})

        save.svc.transport = httpx.MockTransport(peer)
        snapshot = save.save({"recipe": original})["result"]
        scope_args = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
        result = save.svc.edit_inspection_recipe(ask, snapshot, *scope_args)
        assert result["result"] == {"element": wanted, "revision": 2}, result
        assert snapshot == {"element": original, "revision": 1}
        stale = save.svc.edit_inspection_recipe(ask, snapshot, *scope_args)
        assert stale["error"]["code"] == "conflict" and "result" not in stale
        assert [(v, json.loads(row)) for v, row in db.execute(
            "SELECT revision, content FROM history ORDER BY revision")] == [(1, original), (2, wanted)]
        assert writes == [("POST", scope, None), ("PUT", scope + "/inspection-table", "1"),
                          ("PUT", scope + "/inspection-table", "1")]
