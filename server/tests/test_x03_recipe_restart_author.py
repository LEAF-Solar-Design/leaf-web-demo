"""Lose the entire adapter after a recipe commit and reopen the recorded intent."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
from integration_tool_service import IntegrationToolService


class AdapterStopped(BaseException):
    """Simulate process exit before an adapter can handle or return the reply."""


@pytest.mark.parametrize("ask,field", [
    ("Retitle the inspection recipe as Restart audit.", "title"),
    ("Show ten inspection rows per page.", "row_limit"),
    ("Sort the inspection amounts descending.", "sort"),
])
def test_reopen_after_commit_without_response(save, tmp_path, ask, field):
    path = tmp_path / "restart.sqlite"
    original = {**recipe(), "id": "inspection-west", "title": "Inspection"}
    desired = deepcopy(original)
    if field == "title":
        desired["title"] = "Restart audit"
    elif field == "row_limit":
        desired["props"]["row_limit"] = 10
    else:
        desired["query"]["sort"] = [{"field": "amount", "direction": "desc"}]
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE versions (rev INTEGER PRIMARY KEY, body TEXT)")
        db.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, intent TEXT)")
        db.execute("CREATE TABLE puts (parent INTEGER)")
        db.execute("INSERT INTO versions VALUES (1,?)", (json.dumps(original),))

    def journal(authority, attempt):
        assert authority == save.auth
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO requests VALUES (1,?)", (json.dumps(attempt),))

    def peer(request):
        assert request.method == "PUT" and request.url.path.endswith("/inspection-west")
        parent = int(request.headers["If-Match"])
        with closing(sqlite3.connect(path)) as db, db:
            assert db.execute("SELECT COUNT(*) FROM requests").fetchone() == (1,)
            db.execute("INSERT INTO puts VALUES (?)", (parent,))
            inserted = db.execute("INSERT INTO versions SELECT ?, ? WHERE (SELECT MAX(rev) FROM versions)=?",
                                  (parent+1, request.content.decode(), parent))
            assert inserted.rowcount == 1
        raise AdapterStopped()

    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    save.svc.transport = httpx.MockTransport(peer)
    with pytest.raises(AdapterStopped):
        save.svc.edit_inspection_recipe(ask, {"element": original, "revision": 1}, *scope,
                                        record_attempt=journal)
    del save.svc
    current = save.current
    reads = []
    def read_revision(authority, element_id, revision):
        assert authority == current.authority and element_id == original["id"]
        reads.append(revision)
        with closing(sqlite3.connect(path)) as db:
            body, = db.execute("SELECT body FROM versions WHERE rev=?", (revision,)).fetchone()
        return {"element": json.loads(body), "revision": revision}

    # Only current host authority is supplied anew. No prior service, transport,
    # store connection, response, or in-memory attempt is handed to recovery.
    fresh = IntegrationToolService(resolve_write=lambda *_: current,
                                  transport=httpx.MockTransport(lambda _: pytest.fail("replayed write")))
    with closing(sqlite3.connect(path)) as db:
        encoded, = db.execute("SELECT intent FROM requests WHERE id=1").fetchone()
        attempt = json.loads(encoded)
        assert len(attempt["retry_key"]) == 64
        for _ in range(2):
            result = fresh.edit_inspection_recipe(attempt["description"], attempt["snapshot"], *scope,
                                                  retry_key=attempt["retry_key"], read_revision=read_revision)
            assert result == {"result": {"element": desired, "revision": 2}}
        assert reads == [2, 2]
        assert list(db.execute("SELECT parent FROM puts")) == [(1,)]
        assert [(r, json.loads(body)) for r, body in db.execute("SELECT * FROM versions ORDER BY rev")] == [
            (1, original), (2, desired)]
