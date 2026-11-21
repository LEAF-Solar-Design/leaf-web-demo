"""Execute Studio saves against a disposable HTTP peer with SQLite revision history."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


@pytest.mark.parametrize("ask,edit", [
    ("Rename my inspection table to Roof punch list without overwriting another editor.", "title"),
    ("Show two inspection rows per page and warn me if the table changed.", "row_limit"),
    ("Sort the inspection amounts highest first unless another editor saved first.", "sort"),
])
def test_save_one_successor_and_reject_stale_edit(save, tmp_path, ask, edit):
    original = {**recipe(), "id": "inspection-table", "title": "Inspection table"}
    changed = deepcopy(original)
    if edit == "title":
        changed["title"] = "Roof punch list"
    elif edit == "row_limit":
        changed["props"]["row_limit"] = 2
    else:
        changed["query"]["sort"] = [{"field": "amount", "direction": "desc"}]
    path = f"/api/v1/projects/{save.auth.project_id}/integrations/elements"
    with closing(sqlite3.connect(tmp_path / "elements.sqlite")) as db:
        db.execute("CREATE TABLE history(revision INTEGER PRIMARY KEY, content TEXT NOT NULL)")
        writes = []

        def transport(request):
            expected = request.headers.get("if-match")
            writes.append((request.method, request.url.path, expected))
            row = json.loads(request.content)
            current, = db.execute("SELECT coalesce(max(revision), 0) FROM history").fetchone()
            if request.method == "POST":
                assert request.url.path == path and expected is None
                accepted = current == 0
            else:
                assert request.method == "PUT" and request.url.path == f"{path}/{original['id']}"
                accepted = expected == str(current)
            if not accepted:
                return httpx.Response(409)
            with db:
                db.execute("INSERT INTO history VALUES (?, ?)", (current + 1, json.dumps(row)))
            return httpx.Response(200, json={"element": row, "revision": current + 1})

        save.svc.transport = httpx.MockTransport(transport)
        first = save.save({"recipe": original})["result"]
        assert first == {"element": original, "revision": 1}
        successor = save.save({"recipe": changed, "expectedRevision": first["revision"]})
        assert successor["result"] == {"element": changed, "revision": 2}, ask
        losing_edit = {**original, "title": "Stale editor copy"}
        conflict = save.save({"recipe": losing_edit, "expectedRevision": first["revision"]})
        assert conflict["error"]["code"] == "conflict" and "result" not in conflict
        history = [(rev, json.loads(content)) for rev, content in db.execute(
            "SELECT revision, content FROM history ORDER BY revision")]
        assert history == [(1, original), (2, changed)]
        assert writes == [("POST", path, None), ("PUT", f"{path}/inspection-table", "1"),
                          ("PUT", f"{path}/inspection-table", "1")]
