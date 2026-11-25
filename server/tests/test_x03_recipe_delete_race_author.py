"""Interleave an immutable deletion with an authored recipe's conditional save."""
from contextlib import closing
import json
import sqlite3

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


@pytest.mark.parametrize("ask,when", [
    ("Retitle the inspection table as Archived defects.", "after_selection"),
    ("Show eight inspection rows per page.", "during_approval"),
    ("Sort my inspection rows by amount descending.", "before_commit"),
])
def test_stale_save_preserves_tombstone(save, tmp_path, ask, when):
    path = tmp_path / "revisions.sqlite"
    element = {**recipe(), "id": "inspection-log", "title": "Inspection log"}
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE history (revision INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO history VALUES (1, ?)", (json.dumps(element),))
        version, body = db.execute("SELECT * FROM history").fetchone()
        selected = {"element": json.loads(body), "revision": version}

    def delete():
        with closing(sqlite3.connect(path)) as writer, writer:
            writer.execute("INSERT INTO history VALUES (2, NULL)")

    if when == "after_selection":
        delete()
    elif when == "during_approval":
        gate = save.svc.gate
        def delete_at_gate(*args, **kwargs):
            verdict = gate(*args, **kwargs)
            delete()
            return verdict
        save.svc.gate = delete_at_gate

    attempts = []
    def peer(request):
        attempts.append((request.method, request.url.path.split("/")[-1], request.headers["If-Match"]))
        if when == "before_commit":
            delete()
        with closing(sqlite3.connect(path)) as writer, writer:
            parent = int(request.headers["If-Match"])
            changed = writer.execute(
                "INSERT INTO history SELECT revision+1, ? FROM history WHERE revision=? AND "
                "revision=(SELECT MAX(revision) FROM history) AND body IS NOT NULL",
                (request.content.decode(), parent))
            if changed.rowcount:
                return httpx.Response(200, json={"revision": parent+1, "element": json.loads(request.content)})
            latest, = writer.execute("SELECT body FROM history ORDER BY revision DESC LIMIT 1").fetchone()
            return httpx.Response(410 if latest is None else 409)

    save.svc.transport = httpx.MockTransport(peer)
    result = save.svc.edit_inspection_recipe(ask, selected, save.auth.tenant_id, save.auth.session_id,
                                             save.auth.turn_id)
    assert result["error"]["code"] == "deleted_conflict" and "result" not in result
    assert attempts == [("PUT", "inspection-log", "1")]
    with closing(sqlite3.connect(path)) as db:
        assert list(db.execute("SELECT * FROM history ORDER BY revision")) == [(1, body), (2, None)]
