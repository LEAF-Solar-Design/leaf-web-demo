"""Replay the same key against two independently persisted project histories."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import sqlite3
from uuid import uuid4

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


@pytest.mark.parametrize("ask,kind", [
    ("Retitle the inspection table as West roof.", "direct"),
    ("Display eleven inspection rows per page.", "journal"),
    ("Sort the inspection amounts lowest first.", "both"),
])
def test_recovery_scope_is_bound_before_store_access(save, tmp_path, ask, kind):
    local_project = save.auth.project_id
    other_project = str(uuid4())
    stores = {local_project: tmp_path / "local.sqlite", other_project: tmp_path / "other.sqlite"}
    original = {**recipe(), "id": "inspection-register"}
    desired = deepcopy(original)
    if kind == "direct":
        desired["title"] = "West roof"
    elif kind == "journal":
        desired["props"]["row_limit"] = 11
    else:
        desired["query"]["sort"] = [{"field": "amount", "direction": "asc"}]
    for project, path in stores.items():
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE versions(rev INTEGER PRIMARY KEY, body TEXT)")
            db.execute("INSERT INTO versions VALUES(1,?)", (json.dumps(original),))
            if project == other_project:
                # Matching bytes would give a false positive if key scope were ignored.
                db.execute("INSERT INTO versions VALUES(2,?)", (json.dumps(desired),))
    puts = []
    def peer(request):
        project = request.url.path.split("/")[4]
        assert request.method == "PUT" and request.headers["If-Match"] == "1"
        puts.append(project)
        with closing(sqlite3.connect(stores[project])) as db, db:
            db.execute("INSERT INTO versions VALUES(2,?)", (request.content.decode(),))
        return httpx.Response(200, json={"element": json.loads(request.content), "revision": 2})

    save.svc.transport = httpx.MockTransport(peer)
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    snapshot = {"element": original, "revision": 1}
    journal = tmp_path / "saved-intent"
    first = save.svc.edit_inspection_recipe(ask, snapshot, *scope,
        record_attempt=lambda _, intent: journal.write_text(json.dumps(intent), encoding="utf-8"))
    assert first["result"] == {"element": desired, "revision": 2}
    key = first["retry_key"]
    raw = journal.read_bytes()
    assert json.loads(raw)["retry_key"] == key
    reads = []
    def reader(authority, element_id, revision):
        assert element_id == original["id"]
        reads.append(authority.project_id)
        with closing(sqlite3.connect(stores[authority.project_id])) as db:
            body, = db.execute("SELECT body FROM versions WHERE rev=?", (revision,)).fetchone()
        return {"element": json.loads(body), "revision": revision}

    for project in (other_project, local_project):
        save.current = replace(save.current, authority=replace(save.auth, project_id=project))
        if kind in ("direct", "both"):
            replay = save.svc.edit_inspection_recipe(ask, snapshot, *scope, retry_key=key, read_revision=reader)
            if project == other_project:
                assert replay["error"]["code"] == "conflict" and "result" not in replay
            else:
                assert replay == {"result": first["result"]}
        if kind in ("journal", "both"):
            replay = save.svc.recover_recipe_save("Recover my inspection recipe save receipt.", raw, *scope,
                journal_sha256=sha256(raw).hexdigest(), read_revision=reader)
            if project == other_project:
                assert replay["error"]["code"] == "outcome_unknown" and "result" not in replay
            else:
                assert replay == {"result": first["result"]}
        if project == other_project:
            assert reads == []
    assert reads == [local_project] * (2 if kind == "both" else 1)
    assert puts == [local_project]
    for path in stores.values():
        with closing(sqlite3.connect(path)) as db:
            assert [(rev, json.loads(body)) for rev, body in db.execute("SELECT * FROM versions ORDER BY rev")] == [
                (1, original), (2, desired)]
