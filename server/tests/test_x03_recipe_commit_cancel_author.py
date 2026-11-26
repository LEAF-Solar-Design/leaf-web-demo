"""Recover an authored save when its transport task is cancelled at a commit boundary."""
import asyncio
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from threading import Event

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


CASES = [
    ("Retitle the inspection table as East roof.", "before"),
    ("Show ten inspection rows per page.", "during"),
    ("Sort the inspection amounts descending.", "after"),
]


@pytest.mark.parametrize("ask,phase", CASES)
def test_cancellation_at_commit(save, tmp_path, ask, phase):
    path = tmp_path / "versions.sqlite"
    original = {**recipe(), "id": "inspection-east"}
    parent = {"element": original, "revision": 1}
    desired = deepcopy(original)
    if phase == "before":
        desired["title"] = "East roof"
    elif phase == "during":
        desired["props"]["row_limit"] = 10
    else:
        desired["query"]["sort"] = [{"field": "amount", "direction": "desc"}]
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE versions (revision INTEGER PRIMARY KEY, doc TEXT)")
        db.execute("INSERT INTO versions VALUES (1, ?)", (json.dumps(original),))

    writes, reads, commits = [], [], []
    journal = tmp_path / "attempt.json"
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id

    def record(authority, entry):
        assert authority == save.auth
        journal.write_text(json.dumps(entry), encoding="utf-8")

    async def provider(request):
        assert journal.is_file()
        writes.append((request.method, request.headers["If-Match"]))
        with closing(sqlite3.connect(path, check_same_thread=False)) as db:
            db.execute("INSERT INTO versions VALUES (2, ?)", (request.content.decode(),))
            task = asyncio.current_task()
            if phase == "during":
                loop = asyncio.get_running_loop()
                release = Event()

                def trace(sql):
                    if sql == "COMMIT":
                        loop.call_soon_threadsafe(task.cancel)
                        release.wait(5)

                db.set_trace_callback(trace)
                work = loop.run_in_executor(None, db.commit)
                try:
                    await asyncio.shield(work)
                finally:
                    release.set()
                    await work
                    commits.append(phase)
            else:
                if phase == "after":
                    db.commit()
                    commits.append(phase)
                task.cancel()
                await asyncio.sleep(0)
        pytest.fail("transport should have been cancelled")

    def read(authority, identity, revision):
        reads.append((authority, identity, revision))
        with closing(sqlite3.connect(path)) as db:
            row = db.execute("SELECT doc FROM versions WHERE revision=?", (revision,)).fetchone()
        if row:
            return {"element": json.loads(row[0]), "revision": revision}
        return None

    save.svc.transport = httpx.MockTransport(provider)
    lost = save.svc.edit_inspection_recipe(ask, parent, *scope, record_attempt=record)
    assert lost["error"]["code"] == "outcome_unknown"
    entry = json.loads(journal.read_text(encoding="utf-8"))
    assert lost["retry_key"] == entry["retry_key"]
    for _ in range(2):
        result = save.svc.edit_inspection_recipe(ask, parent, *scope, retry_key=entry["retry_key"],
                                                 read_revision=read)
        if phase == "before":
            assert result["error"]["code"] == "outcome_unknown"
        else:
            assert result == {"result": {"element": desired, "revision": 2}}
    assert writes == [("PUT", "1")]
    assert reads == [(save.auth, original["id"], 2)] * 2
    assert commits == ([] if phase == "before" else [phase])
    with closing(sqlite3.connect(path)) as db:
        history = [(r, json.loads(doc)) for r, doc in db.execute("SELECT * FROM versions ORDER BY revision")]
    expected = [(1, original)]
    if phase != "before":
        expected.append((2, desired))
    assert history == expected
