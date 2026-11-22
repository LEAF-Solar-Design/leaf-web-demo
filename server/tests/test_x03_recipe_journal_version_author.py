"""Refuse unknown pinned intent formats before consulting recipe revisions."""
from hashlib import sha256
import json

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
from tools_fallback import author_tool


CASES = [
    ("Recover my recipe save receipt after the inspection window closed.", 2),
    ("Did the table edit save before the response timed out?", "1"),
    ("Retry the element update after the connection dropped.", True),
]


@pytest.mark.parametrize("ask,unknown_version", CASES)
def test_unknown_save_journal_version(save, tmp_path, ask, unknown_version):
    intent = tmp_path / "intent.json"
    revision = tmp_path / "revision.json"
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    original = {**recipe(), "id": "inspection-roof"}
    before = {"element": original, "revision": 4}
    expected = {"element": {**original, "title": "West roof"}, "revision": 5}
    writes = []

    def commit_without_receipt(request):
        assert request.headers["If-Match"] == "4" and request.method == "PUT"
        writes.append(request.content)
        revision.write_bytes(request.content)
        raise httpx.ReadTimeout("lost after commit")

    save.svc.transport = httpx.MockTransport(commit_without_receipt)
    lost = save.svc.edit_inspection_recipe(
        "Retitle this inspection table as West roof.", before, *scope,
        record_attempt=lambda _, entry: intent.write_text(json.dumps(entry), encoding="utf-8"))
    assert lost["error"]["code"] == "outcome_unknown"
    emitted = json.loads(intent.read_bytes())
    committed = revision.read_bytes()
    assert json.loads(committed) == expected["element"]
    reads = []

    def read_revision(authority, identity, number):
        assert authority == save.auth and identity == original["id"]
        reads.append(number)
        return {"element": json.loads(revision.read_bytes()), "revision": number}

    def recover(entry):
        intent.write_text(json.dumps(entry), encoding="utf-8")
        raw = intent.read_bytes()
        return save.svc.recover_recipe_save(ask, raw, *scope, read_revision=read_revision,
                                             journal_sha256=sha256(raw).hexdigest())

    assert author_tool(ask)[0]["engine_op"] == "recipe_save_recovery"
    rejected = recover({**emitted, "format_version": unknown_version})
    assert rejected["error"]["code"] == "unsupported_journal_version" and "result" not in rejected
    assert reads == [] and writes == [committed]
    assert type(emitted["format_version"]) is int and emitted["format_version"] == 1
    assert recover(emitted) == {"result": expected}
    # Existing unversioned records survive an adapter upgrade.
    assert recover({key: val for key, val in emitted.items() if key != "format_version"}) == {"result": expected}
    assert reads == [5, 5] and writes == [committed] and revision.read_bytes() == committed
