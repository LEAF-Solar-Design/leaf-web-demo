"""Journal permission errors cannot manufacture or conceal a committed successor."""
from hashlib import sha256
import json

import httpx
import pytest

from test_integration_element_save import recipe, rig, save


CASES = [
    ("Recover the receipt for the inspection recipe I saved.", "read"),
    ("Check whether my recipe update saved after the lost response.", "create"),
    ("Retry my table edit after the save response timed out.", "discard"),
]


class Journal:
    def __init__(self, denied, authority):
        self.denied = denied
        self.authority = authority
        self.pin = None
        self.entries = {}
        self.events = []

    def check(self, operation, authority):
        assert authority == self.authority
        self.events.append(operation)
        if self.denied == operation:
            raise PermissionError(operation)

    def create(self, authority, entry):
        self.check("create", authority)
        data = json.dumps(entry).encode()
        self.pin = sha256(data).hexdigest()
        self.entries[self.pin] = data

    def read(self, authority, digest):
        self.check("read", authority)
        return self.entries[digest]

    def discard(self, authority, digest):
        self.check("discard", authority)
        del self.entries[digest]


@pytest.mark.parametrize("ask,denied", CASES)
def test_journal_permissions(save, tmp_path, ask, denied):
    journal = Journal(denied, save.auth)
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    element = {**recipe(), "id": "inspection-east"}
    parent = {"revision": 1, "element": element}
    next_revision = tmp_path / "2.json"
    writes, reads = [], []

    def commit(request):
        writes.append((request.method, request.headers["If-Match"]))
        with next_revision.open("xb") as out:
            out.write(request.content)
        raise httpx.ReadTimeout("response lost")

    def read_revision(authority, identity, revision):
        reads.append((authority, identity, revision))
        return {"revision": revision, "element": json.loads(next_revision.read_bytes())}

    def submit():
        return save.svc.edit_inspection_recipe("Retitle this inspection recipe as East roof.", parent,
                                               *scope, record_attempt=journal.create)

    def recover():
        return save.svc.recover_recipe_save(ask, None, *scope, journal_adapter=journal,
                                            journal_sha256=journal.pin, read_revision=read_revision)

    save.svc.transport = httpx.MockTransport(commit)
    result = submit()
    if denied == "create":
        assert result["error"]["code"] == "unavailable"
        assert not next_revision.exists() and writes == [] and journal.entries == {}
        assert recover()["error"]["code"] == "outcome_unknown"
        assert journal.events == ["create"] and reads == []
        journal.denied = None
        result = submit()

    assert result["error"]["code"] == "outcome_unknown"
    expected = {"result": {"revision": 2, "element": {**element, "title": "East roof"}}}
    if denied == "read":
        assert recover()["error"]["code"] == "outcome_unknown"
        assert reads == [] and journal.events[-1] == "read" and journal.pin in journal.entries
    if denied == "discard":
        assert recover() == expected
        assert journal.pin in journal.entries and journal.events[-2:] == ["read", "discard"]

    journal.denied = None
    assert recover() == expected
    assert not journal.entries and journal.events[-2:] == ["read", "discard"]
    assert reads == [(save.auth, element["id"], 2)] * (2 if denied == "discard" else 1)
    assert writes == [("PUT", "1")] and json.loads(next_revision.read_bytes()) == expected["result"]["element"]
