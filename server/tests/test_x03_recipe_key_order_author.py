"""Independently canonicalize JSON object members while retaining array order."""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
import sqlite3

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


def canonical(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key) + ":" + canonical(value[key]) for key in sorted(value)) + "}"
    if isinstance(value, list):
        return "[" + ",".join(map(canonical, value)) + "]"
    return json.dumps(value)


@pytest.mark.parametrize("ask,edit", [
    ("Retitle the inspection recipe as Array audit.", "title"),
    ("Show twelve inspection rows per page.", "rows"),
    ("Sort the inspection amounts ascending.", "sort"),
])
def test_reordered_objects_recover_but_changed_arrays_conflict(save, tmp_path, ask, edit):
    original = {**recipe(), "id": "inspection-register"}
    original["props"]["columns"] = ["date", "amount"]
    original["query"]["fields"] = ["date", "amount"]
    desired = deepcopy(original)
    if edit == "title":
        desired["title"] = "Array audit"
    elif edit == "rows":
        desired["props"]["row_limit"] = 12
    else:
        desired["query"]["sort"] = [{"field": "amount", "direction": "asc"}]
    path = tmp_path / "version.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE history(rev INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO history VALUES(1,?)", (json.dumps(original),))
    writes = []
    def peer(request):
        assert request.method == "PUT" and request.headers["If-Match"] == "1"
        element = json.loads(request.content)
        writes.append(element)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO history VALUES(2,?)", (request.content.decode(),))
        return httpx.Response(200, json={"revision": 2, "element": element})

    save.svc.transport = httpx.MockTransport(peer)
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    snapshot = {"element": original, "revision": 1}
    first = save.svc.edit_inspection_recipe(ask, snapshot, *scope, issue_retry_key=True)
    assert first["result"] == {"revision": 2, "element": desired}
    encoded = canonical({"operation": "inspection-recipe-save-v1", "tenant": save.auth.tenant_id,
                         "project": save.auth.project_id, "origin": save.auth.origin, "subject": save.auth.subject,
                         "request": {"expectedRevision": 1, "recipe": desired}})
    key = sha256(encoded.encode()).hexdigest()
    assert key == first["retry_key"]
    reordered = json.loads(json.dumps(snapshot), object_pairs_hook=lambda pairs: dict(reversed(pairs)))
    assert json.dumps(reordered) != json.dumps(snapshot) and canonical(reordered) == canonical(snapshot)
    reads = []
    def reader(authority, identity, revision):
        assert authority == save.auth and identity == original["id"]
        reads.append(revision)
        with closing(sqlite3.connect(path)) as db:
            body, = db.execute("SELECT body FROM history WHERE rev=?", (revision,)).fetchone()
        return {"element": json.loads(body, object_pairs_hook=lambda pairs: dict(reversed(pairs))), "revision": revision}
    retry = {"retry_key": key, "read_revision": reader}
    assert save.svc.edit_inspection_recipe(ask, reordered, *scope, **retry) == {"result": first["result"]}
    changed = deepcopy(reordered)
    changed["element"]["props"]["columns"].reverse()
    assert canonical(changed) != canonical(snapshot)
    rejected = save.svc.edit_inspection_recipe(ask, changed, *scope, **retry)
    assert rejected["error"]["code"] == "conflict" and "result" not in rejected
    assert reads == [2] and writes == [desired]
    with closing(sqlite3.connect(path)) as db:
        assert [(r, json.loads(b)) for r, b in db.execute("SELECT * FROM history ORDER BY rev")] == [
            (1, original), (2, desired)]
