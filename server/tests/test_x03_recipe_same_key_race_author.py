"""Concurrent recoveries of one durable attempt must agree on its exact successor."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import json
from threading import Barrier

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


CASES = [
    ("Recover the receipt for my inspection recipe save.", "Retitle the inspection recipe as East roof.", "title"),
    ("Did my table edit save after the response timed out?", "Show ten inspection rows per page.", "rows"),
    ("Retry my recipe save after the connection dropped.", "Sort the inspection amounts descending.", "sort"),
]


@pytest.mark.parametrize("ask,edit,kind", CASES)
def test_overlapping_same_key_recoveries(save, tmp_path, ask, edit, kind):
    original = {**recipe(), "id": "inspection-west"}
    desired = deepcopy(original)
    if kind == "title":
        desired["title"] = "East roof"
    elif kind == "rows":
        desired["props"]["row_limit"] = 10
    else:
        desired["query"]["sort"] = [{"field": "amount", "direction": "desc"}]
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    history = tmp_path / "history"
    history.mkdir()
    (history / "1").write_text(json.dumps(original))
    journal = tmp_path / "intent"
    writes, reads = [], []
    meet = Barrier(2, timeout=20)

    def record(authority, entry):
        assert authority == save.auth
        journal.write_bytes(json.dumps(entry).encode())

    def commit(request):
        writes.append((request.method, request.headers["If-Match"]))
        with (history / "2").open("xb") as out:
            out.write(request.content)
        raise httpx.ReadTimeout("lost acknowledgement")

    def read(authority, identity, revision):
        reads.append((authority, identity, revision))
        # Both live recovery calls reached revision I/O before either can return.
        meet.wait()
        return {"revision": revision, "element": json.loads((history / str(revision)).read_bytes())}

    save.svc.transport = httpx.MockTransport(commit)
    result = save.svc.edit_inspection_recipe(edit, {"revision": 1, "element": original},
                                            *scope, record_attempt=record)
    assert result["error"]["code"] == "outcome_unknown"
    raw = journal.read_bytes()
    assert result["retry_key"] == json.loads(raw)["retry_key"]
    pin = sha256(raw).hexdigest()

    def retry():
        return save.svc.recover_recipe_save(ask, journal.read_bytes(), *scope, journal_sha256=pin,
                                            read_revision=read)

    with ThreadPoolExecutor(max_workers=2) as workers:
        jobs = [workers.submit(retry) for _ in range(2)]
        outcomes = [job.result(timeout=35) for job in jobs]
    assert outcomes == [{"result": {"revision": 2, "element": desired}}] * 2
    assert reads == [(save.auth, original["id"], 2)] * 2 and writes == [("PUT", "1")]
    assert [(p.name, json.loads(p.read_bytes())) for p in sorted(history.iterdir())] == [
        ("1", original), ("2", desired)]
    assert journal.read_bytes() == raw
