"""Read trusted journal digests separately from damaged bytes and immutable successors."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
from tools_fallback import author_tool


@pytest.mark.parametrize("ask,damage", [
    ("Recover the receipt for my inspection recipe save.", "truncated"),
    ("Did my table edit land after the save response timed out?", "changed_payload"),
    ("Retry my recipe save after the connection dropped.", "wrong_key"),
])
def test_corrupt_journal_cannot_confirm_committed_successor(save, tmp_path, ask, damage):
    path = tmp_path / "recipe.sqlite"
    journal = tmp_path / "intent.json"
    original = {**recipe(), "id": "inspection-table"}
    desired = {**original, "title": "Verified roof"}
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE history(rev INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO history VALUES(1,?)", (json.dumps(original),))
    writes = []
    def peer(request):
        writes.append(request.method)
        assert request.headers["If-Match"] == "1"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO history VALUES(2,?)", (request.content.decode(),))
        raise httpx.ReadTimeout("reply lost after commit")

    save.svc.transport = httpx.MockTransport(peer)
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    lost = save.svc.edit_inspection_recipe("Retitle this inspection table as Verified roof.",
        {"element": original, "revision": 1}, *scope,
        record_attempt=lambda auth, record: journal.write_bytes(json.dumps(record).encode()))
    assert lost["error"]["code"] == "outcome_unknown"
    good = journal.read_bytes()
    digest = sha256(good).hexdigest()
    if damage == "truncated":
        damaged = good[:-6]
    elif damage == "changed_payload":
        damaged = good.replace(b"Verified roof", b"Different roof")
    else:
        damaged = json.dumps({**json.loads(good), "retry_key": "0"*64}).encode()
    assert damaged != good
    journal.write_bytes(damaged)
    reads = []
    def history(authority, identity, revision):
        assert authority == save.auth and identity == original["id"]
        reads.append(revision)
        with closing(sqlite3.connect(path)) as db:
            body, = db.execute("SELECT body FROM history WHERE rev=?", (revision,)).fetchone()
        return {"element": json.loads(body), "revision": revision}

    assert author_tool(ask)[0]["engine_op"] == "recipe_save_recovery"
    # A checksum can match malformed JSON or an internally inconsistent record.
    expected = digest if damage == "changed_payload" else sha256(damaged).hexdigest()
    rejected = save.svc.recover_recipe_save(ask, journal.read_bytes(), *scope, journal_sha256=expected,
                                             read_revision=history)
    assert rejected["error"]["code"] == "outcome_unknown" and "result" not in rejected
    assert reads == []
    journal.write_bytes(good)
    confirmed = save.svc.recover_recipe_save(ask, journal.read_bytes(), *scope, journal_sha256=digest,
                                              read_revision=history)
    assert confirmed == {"result": {"element": desired, "revision": 2}}
    assert reads == [2] and writes == ["PUT"]
    with closing(sqlite3.connect(path)) as db:
        assert [(r, json.loads(b)) for r, b in db.execute("SELECT * FROM history ORDER BY rev")] == [
            (1, original), (2, desired)]
