"""A retry of an authored edit can only observe the original table successor."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


CASES = [
    ("Retitle this inspection recipe as Roof register.", "content"),
    ("Show seven inspection rows per page.", "parent"),
    ("Sort my inspection rows by amount descending.", "table"),
]


@pytest.mark.parametrize("ask,collision", CASES)
def test_retry_key_identifies_one_conditional_save(save, tmp_path, ask, collision):
    original = {**recipe(), "id": "inspection-east", "title": "Inspection register"}
    other = {**original, "id": "inspection-west"}
    snapshot = {"element": original, "revision": 1}
    desired = deepcopy(original)
    if collision == "content":
        desired["title"] = "Roof register"
    elif collision == "parent":
        desired["props"]["row_limit"] = 7
    else:
        desired["query"]["sort"] = [{"field": "amount", "direction": "desc"}]
    store_path = tmp_path / "tables.sqlite"
    with closing(sqlite3.connect(store_path)) as db:
        db.execute("CREATE TABLE versions (id TEXT, rev INTEGER, body TEXT, PRIMARY KEY(id,rev))")
        with db:
            for element in (original, other):
                db.execute("INSERT INTO versions VALUES (?,1,?)", (element["id"], json.dumps(element)))

        writes = []
        def peer(request):
            element_id = request.url.path.rsplit("/", 1)[-1]
            parent = int(request.headers["If-Match"])
            assert request.method == "PUT"
            writes.append((element_id, parent))
            # Each mock HTTP call runs on its own event-loop thread.
            with closing(sqlite3.connect(store_path)) as writer, writer:
                inserted = writer.execute(
                    "INSERT INTO versions SELECT ?, ?, ? WHERE "
                    "(SELECT MAX(rev) FROM versions WHERE id=?) = ?",
                    (element_id, parent + 1, request.content.decode(), element_id, parent))
                if inserted.rowcount == 0:
                    return httpx.Response(409)
            if collision == "table":
                raise httpx.ReadTimeout("response lost after commit")
            return httpx.Response(200, json={"revision": parent + 1, "element": json.loads(request.content)})

        reads = []
        def read_revision(authority, element_id, revision):
            assert authority == save.auth
            reads.append((element_id, revision))
            body, = db.execute("SELECT body FROM versions WHERE id=? AND rev=?",
                               (element_id, revision)).fetchone()
            return {"element": json.loads(body), "revision": revision}

        save.svc.transport = httpx.MockTransport(peer)
        scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
        edit = save.svc.edit_inspection_recipe
        accepted = edit(ask, snapshot, *scope, issue_retry_key=True)
        expected = {"element": desired, "revision": 2}
        if collision == "table":
            assert accepted["error"]["code"] == "outcome_unknown" and "result" not in accepted
        else:
            assert accepted["result"] == expected
        retry = {"retry_key": accepted["retry_key"], "read_revision": read_revision}

        changed_ask, changed_snapshot = ask, snapshot
        if collision == "content":
            changed_ask = "Retitle this inspection recipe as Site register."
        elif collision == "parent":
            changed_snapshot = accepted["result"]
        else:
            changed_snapshot = {"revision": 1, "element": other}
        rejected = edit(changed_ask, changed_snapshot, *scope, **retry)
        assert rejected["error"]["code"] == "conflict" and "result" not in rejected
        assert reads == []
        replay = edit(ask, snapshot, *scope, **retry)
        assert replay == {"result": expected}
        assert reads == [("inspection-east", 2)]
        assert writes == [("inspection-east", 1)]
        history = [(i, r, json.loads(b)) for i, r, b in
                   db.execute("SELECT id, rev, body FROM versions ORDER BY id,rev")]
        assert history == [("inspection-east", 1, original), ("inspection-east", 2, desired),
                           ("inspection-west", 1, other)]
