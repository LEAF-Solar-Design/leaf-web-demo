"""Reconcile a lost save response against the original immutable revision."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


ASKS = (
    "Recover the missing receipt for my inspection recipe save.",
    "Did my table edit land after the save response timed out?",
    "Retry my recipe save after the connection dropped without creating another revision.",
)


@pytest.mark.parametrize("ask,mode", zip(ASKS, ("title", "page_size", "sort")))
def test_lost_reply_recovery(save, tmp_path, ask, mode):
    from tools_fallback import author_tool

    original = recipe()
    desired = deepcopy(original)
    if mode == "title":
        desired["title"] = "Roof inspection"
    elif mode == "page_size":
        desired["props"]["row_limit"] = 3
    else:
        desired["query"]["sort"] = [{"field": "amount", "direction": "desc"}]
    ctx = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    args = {"recipe": desired, "expectedRevision": 1}
    with closing(sqlite3.connect(tmp_path / "history.sqlite")) as db:
        db.execute("CREATE TABLE history (revision INTEGER PRIMARY KEY, element TEXT NOT NULL)")
        db.execute("INSERT INTO history VALUES (1,?)", (json.dumps(original),))
        db.commit()
        writes = []

        def peer(request):
            writes.append((request.method, request.url.path, request.headers["if-match"]))
            assert writes == [("PUT", f"/api/v1/projects/{save.auth.project_id}/integrations/elements/" + original["id"], "1")]
            with db:
                db.execute("INSERT INTO history VALUES (2,?)", (request.content.decode(),))
            raise httpx.ReadTimeout("commit succeeded but response lost")

        save.svc.transport = httpx.MockTransport(peer)
        assert save.save(args)["error"]["code"] == "outcome_unknown"
        assert author_tool(ask)[0]["engine_op"] == "recipe_save_recovery"
        expected = [original, desired]
        if mode == "page_size":
            later = {**desired, "title": "Later revision"}
            with db:
                db.execute("INSERT INTO history VALUES (3,?)", (json.dumps(later),))
            expected.append(later)
        reads = []

        def read_revision(authority, element_id, revision):
            reads.append((authority, element_id, revision))
            if mode == "sort":
                raise TimeoutError
            row = db.execute("SELECT element FROM history WHERE revision=?", (revision,)).fetchone()
            return {"revision": revision, "element": json.loads(row[0])}

        for _ in range(2):
            reply = save.svc.recover_recipe_save(ask, args, *ctx, read_revision=read_revision)
            if mode == "sort":
                assert reply["error"]["code"] == "outcome_unknown" and "result" not in reply
            else:
                assert reply == {"result": {"revision": 2, "element": desired}}
        assert reads == [(save.auth, original["id"], 2)] * 2
        assert len(writes) == 1
        assert [(r, json.loads(e)) for r, e in db.execute("SELECT * FROM history ORDER BY revision")] == list(enumerate(expected, start=1))


@pytest.mark.parametrize("kind", ["wrong_revision", "different_content", "wrong_id", "missing"])
def test_inconclusive_history_cannot_confirm_save(save, kind):
    element = recipe()
    ctx = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    args = {"recipe": element, "expectedRevision": 7}
    readback = {"revision": 8, "element": deepcopy(element)}
    if kind == "wrong_revision":
        readback["revision"] = 9
    elif kind == "different_content":
        readback["element"]["title"] = "Other editor"
    elif kind == "wrong_id":
        readback["element"]["id"] = "other-table"
    else:
        readback = None
    retry = save.svc.recover_recipe_save(ASKS[0], args, *ctx, read_revision=lambda *_: readback)
    assert retry["error"]["code"] == "outcome_unknown" and "result" not in retry
    assert not save.requests
