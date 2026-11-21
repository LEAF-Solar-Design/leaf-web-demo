"""Interrupt a conditional recipe save inside a disposable provider transaction."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


@pytest.mark.parametrize("ask,point", [
    ("Retitle this inspection table as Roof audit.", "before_transaction"),
    ("Display nine inspection rows per page.", "after_head_update"),
    ("Sort the inspection amounts lowest first.", "after_successor_insert"),
])
def test_rollback_then_explicit_conditional_retry(save, tmp_path, ask, point):
    original = {**recipe(), "id": "inspection-table", "title": "Inspection table"}
    desired = deepcopy(original)
    if point == "before_transaction":
        desired["title"] = "Roof audit"
    elif point == "after_head_update":
        desired["props"]["row_limit"] = 9
    else:
        desired["query"]["sort"] = [{"field": "amount", "direction": "asc"}]
    path = tmp_path / "recipe.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE head (id TEXT PRIMARY KEY, revision INTEGER)")
        db.execute("CREATE TABLE history (revision INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO head VALUES (?, 1)", (original["id"],))
        db.execute("INSERT INTO history VALUES (1, ?)", (json.dumps(original),))

    def read_store():
        with closing(sqlite3.connect(path)) as reader:
            head, = reader.execute("SELECT revision FROM head").fetchone()
            history = [(r, json.loads(body)) for r, body in
                       reader.execute("SELECT revision, body FROM history ORDER BY revision")]
            return head, history

    writes = []
    def peer(request):
        parent = int(request.headers["If-Match"])
        element = json.loads(request.content)
        writes.append((request.method, request.url.path.split("/")[-1], parent, element))
        first = len(writes) == 1
        if first and point == "before_transaction":
            raise httpx.ConnectError("request did not reach the store")
        try:
            with closing(sqlite3.connect(path)) as writer, writer:
                changed = writer.execute("UPDATE head SET revision=? WHERE id=? AND revision=?",
                                         (parent+1, element["id"], parent))
                if not changed.rowcount:
                    return httpx.Response(409)
                if first and point == "after_head_update":
                    raise sqlite3.OperationalError("injected head failure")
                writer.execute("INSERT INTO history VALUES (?,?)", (parent+1, request.content.decode()))
                if first and point == "after_successor_insert":
                    raise sqlite3.OperationalError("injected precommit failure")
        except sqlite3.OperationalError:
            return httpx.Response(500)
        return httpx.Response(200, json={"element": element, "revision": parent+1})

    save.svc.transport = httpx.MockTransport(peer)
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    parent = {"element": original, "revision": 1}
    rejected = save.svc.edit_inspection_recipe(ask, parent, *scope)
    expected_code = "unavailable" if point == "before_transaction" else "outcome_unknown"
    assert rejected["error"]["code"] == expected_code and "result" not in rejected
    assert read_store() == (1, [(1, original)])
    assert len(writes) == 1
    retried = save.svc.edit_inspection_recipe(ask, parent, *scope)
    assert retried["result"] == {"element": desired, "revision": 2}
    assert read_store() == (2, [(1, original), (2, desired)])
    assert writes == [("PUT", original["id"], 1, desired)] * 2
