"""Duplicate nested keys leave the save outcome unresolved before revision I/O."""
from hashlib import sha256
import json

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
from tools_fallback import author_tool


CASES = [
    ("Recover the receipt for my inspection recipe save.", "id", "inspection-east", "inspection-west"),
    ("Did my inspection table update save before the response timed out?", "revision", 4, 3),
    ("Retry the saved element edit after the lost reply.", "row_limit", 5, 9),
]


@pytest.mark.parametrize("ask,key,value,other", CASES)
def test_duplicate_journal_members_precede_revision_access(save, tmp_path, ask, key, value, other):
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    journal = tmp_path / "intent.json"
    revision = tmp_path / "successor.json"
    original = {**recipe(), "id": "inspection-east"}
    desired = {**original, "title": "Roof observations"}
    writes, reads = [], []

    def commit(request):
        assert request.method == "PUT" and request.headers["If-Match"] == "4"
        writes.append(json.loads(request.content))
        revision.write_bytes(request.content)
        raise httpx.ReadTimeout("reply lost after commit")

    def persist(_, entry):
        journal.write_bytes(json.dumps(entry, separators=(",", ":")).encode())

    def reader(authority, identity, number):
        reads.append((authority, identity, number))
        return {"element": json.loads(revision.read_bytes()), "revision": number}

    def recover(raw):
        journal.write_bytes(raw)
        return save.svc.recover_recipe_save(ask, journal.read_bytes(), *scope,
                                            journal_sha256=sha256(raw).hexdigest(), read_revision=reader)

    save.svc.transport = httpx.MockTransport(commit)
    outcome = save.svc.edit_inspection_recipe(
        "Retitle this inspection table as Roof observations.", {"element": original, "revision": 4},
        *scope, record_attempt=persist)
    assert outcome["error"]["code"] == "outcome_unknown" and writes == [desired]
    assert author_tool(ask)[0]["engine_op"] == "recipe_save_recovery"
    unique = journal.read_bytes()
    committed = revision.read_bytes()

    member = (json.dumps(key) + ":" + json.dumps(value)).encode()
    # The second spelling has the same decoded key, including through a JSON escape.
    escaped_key = '"\\u%04x%s"' % (ord(key[0]), key[1:])
    alternate = (escaped_key + ":" + json.dumps(other)).encode()
    assert unique.count(member) == 1
    for pair in ((member, alternate), (alternate, member)):
        ambiguous = unique.replace(member, b",".join(pair))
        rejected = recover(ambiguous)
        assert rejected["error"]["code"] == "outcome_unknown" and "result" not in rejected
        assert reads == [] and writes == [desired] and revision.read_bytes() == committed
    assert recover(unique) == {"result": {"element": desired, "revision": 5}}
    assert reads == [(save.auth, original["id"], 5)] and writes == [desired]
