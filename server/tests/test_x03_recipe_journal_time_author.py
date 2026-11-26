"""Journal recovery uses host time and an inclusive, host-declared age window."""
from hashlib import sha256
import json

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
from tools_fallback import author_tool


CASES = [
    ("Recover my inspection recipe save receipt after the lost reply.", -0.25, False),
    ("Retry the element update after the response timed out.", 30.25, False),
    ("Did my table edit save before the connection dropped?", 30, True),
]


@pytest.mark.parametrize("ask,age,eligible", CASES)
def test_recovery_age_policy(save, tmp_path, ask, age, eligible):
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    recorded_at = 1001.5
    now = recorded_at
    save.svc.clock = lambda: now
    journal = tmp_path / "intent.json"
    original = {**recipe(), "id": "inspection-east"}
    wanted = {"revision": 2, "element": {**original, "title": "East roof"}}
    successor = tmp_path / "revision-2.json"
    writes, reads = [], []

    def put(request):
        assert request.method == "PUT" and request.headers["If-Match"] == "1"
        writes.append(json.loads(request.content))
        with successor.open("xb") as stream:
            stream.write(request.content)
        raise httpx.ReadTimeout("commit completed before reply was delivered")

    save.svc.transport = httpx.MockTransport(put)
    lost = save.svc.edit_inspection_recipe(
        "Retitle the inspection table as East roof.", {"revision": 1, "element": original}, *scope,
        record_attempt=lambda _, record: journal.write_bytes(json.dumps(record).encode()))
    assert lost["error"]["code"] == "outcome_unknown"
    raw = journal.read_bytes()
    assert json.loads(raw)["recorded_at"] == recorded_at
    committed = successor.read_bytes()

    def read_revision(authority, identity, number):
        assert authority == save.auth and identity == original["id"]
        reads.append(number)
        return {"revision": number, "element": json.loads(successor.read_bytes())}

    def recover(data, **policy):
        return save.svc.recover_recipe_save(ask, data, *scope, journal_sha256=sha256(data).hexdigest(),
                                            read_revision=read_revision, **policy)

    assert author_tool(ask)[0]["engine_op"] == "recipe_save_recovery"
    now += age
    out = recover(raw, journal_max_age_seconds=30)
    if eligible:
        assert out == {"result": wanted} and reads == [2]
    else:
        assert out["error"]["code"] == "outcome_unknown" and "result" not in out and not reads
    now = recorded_at
    assert recover(raw, journal_max_age_seconds=0) == {"result": wanted}
    expected_reads = [2] * (int(eligible) + 1)
    legacy = json.loads(raw)
    del legacy["recorded_at"]
    undated = json.dumps(legacy).encode()
    assert recover(undated, journal_max_age_seconds=30)["error"]["code"] == "outcome_unknown"
    assert reads == expected_reads
    assert recover(undated) == {"result": wanted}
    assert reads == expected_reads + [2] and writes == [wanted["element"]]
    assert successor.read_bytes() == committed and journal.read_bytes() == raw
