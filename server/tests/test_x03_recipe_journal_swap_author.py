"""A recovery already in progress uses its pinned bytes when the journal file changes."""
from hashlib import sha256
import json

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
from tools_fallback import author_tool


CASES = [
    ("Recover the receipt for my inspection recipe save.", "other_table"),
    ("Did my table edit save before the response timed out?", "later_revision"),
    ("Retry the element update after the save response was lost.", "reformatted_journal"),
]


@pytest.mark.parametrize("ask,replacement", CASES)
def test_recovery_uses_validated_journal_snapshot(save, tmp_path, ask, replacement):
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    history = tmp_path / "history"
    history.mkdir()
    journal = tmp_path / "current.json"
    incoming = tmp_path / "incoming.json"
    reads, writes = [], []

    def record_path(identity, revision):
        return history / f"{identity}-{revision}.json"

    def commit(request):
        identity = request.url.path.rsplit("/", 1)[-1]
        parent = int(request.headers["If-Match"])
        assert request.method == "PUT" and record_path(identity, parent).exists()
        with record_path(identity, parent+1).open("xb") as stream:
            stream.write(request.content)
        writes.append((identity, parent+1))
        raise httpx.ReadTimeout("committed before reply was delivered")

    def save_attempt(path, parent, title):
        lost = save.svc.edit_inspection_recipe(
            f"Retitle this inspection table as {title}.", parent, *scope,
            record_attempt=lambda _, entry: path.write_bytes(json.dumps(entry).encode()))
        assert lost["error"]["code"] == "outcome_unknown" and path.is_file()

    def seed(identity):
        element = {**recipe(), "id": identity}
        record_path(identity, 1).write_bytes(json.dumps(element).encode())
        return {"revision": 1, "element": element}

    save.svc.transport = httpx.MockTransport(commit)
    parent = seed("inspection-east")
    save_attempt(journal, parent, "East roof")
    expected = {"revision": 2, "element": {**parent["element"], "title": "East roof"}}
    if replacement == "other_table":
        save_attempt(incoming, seed("inspection-west"), "West roof")
    elif replacement == "later_revision":
        save_attempt(incoming, expected, "Second review")
    else:
        incoming.write_bytes(json.dumps(json.loads(journal.read_bytes()), indent=2).encode())

    raw = journal.read_bytes()
    pin = sha256(raw).hexdigest()
    next_raw = incoming.read_bytes()
    assert next_raw != raw
    def history_snapshot():
        return {p.name: p.read_bytes() for p in history.iterdir()}
    before = history_snapshot()
    expected_writes = list(writes)
    assert author_tool(ask)[0]["engine_op"] == "recipe_save_recovery"

    def reader(authority, identity, number):
        assert authority == save.auth
        reads.append((identity, number))
        return {"revision": number, "element": json.loads(record_path(identity, number).read_bytes())}

    def swap_before_read(authority, identity, number):
        # This callback runs after byte validation, before the host reads the successor.
        assert journal.read_bytes() == raw
        incoming.replace(journal)
        return reader(authority, identity, number)

    def recover(candidate, reader_fn):
        return save.svc.recover_recipe_save(ask, candidate, *scope, journal_sha256=pin,
                                            read_revision=reader_fn)

    assert recover(journal.read_bytes(), reader) == {"result": expected}
    assert recover(journal.read_bytes(), swap_before_read) == {"result": expected}
    assert journal.read_bytes() == next_raw
    refused = recover(journal.read_bytes(), reader)
    assert refused["error"]["code"] == "outcome_unknown" and "result" not in refused
    assert reads == [("inspection-east", 2)] * 2
    assert writes == expected_writes and history_snapshot() == before
