"""A recipe save dispatches only after its trusted journal adapter commits the intent."""
from hashlib import sha256
import json
import os

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
from tools_fallback import author_tool


CASES = [
    ("Recover my recipe save receipt after the failed journal write.", "short_write"),
    ("Did my inspection table edit save when the response was lost?", "flush"),
    ("Retry the element update after the save timed out.", "rename"),
]


class LocalIntentJournal:
    """A disposable record_attempt adapter. Success means the complete record is published."""
    def __init__(self, root, fault):
        self.staged = root / "intent.tmp"
        self.committed = root / "intent.json"
        self.fault = fault
        self.events = []
        self.pin = None

    def __call__(self, authority, entry):
        raw = json.dumps(entry, ensure_ascii=False).encode("utf-8")
        with self.staged.open("wb") as stream:
            self.events.append("write")
            chunk = raw[:len(raw)//2] if self.fault == "short_write" else raw
            if stream.write(chunk) != len(raw):
                raise OSError("incomplete journal write")
            self.events.append("flush")
            if self.fault == "flush":
                raise OSError("journal flush failed")
            stream.flush()
            os.fsync(stream.fileno())
        self.events.append("rename")
        if self.fault == "rename":
            raise OSError("journal rename failed")
        self.staged.replace(self.committed)
        self.pin = sha256(raw).hexdigest()


@pytest.mark.parametrize("ask,fault", CASES)
def test_journal_fault_then_retry(save, tmp_path, ask, fault):
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    journal = LocalIntentJournal(tmp_path, fault)
    original = {**recipe(), "id": "inspection-east"}
    parent = {"revision": 1, "element": original}
    desired = {**original, "title": "East roof"}
    history = tmp_path / "history"
    history.mkdir()
    (history / "1.json").write_bytes(json.dumps(original).encode())
    writes, reads = [], []

    def commit(request):
        writes.append(request.method)
        assert request.headers["If-Match"] == "1"
        # Check the published bytes from disk before accepting any successor.
        data = journal.committed.read_bytes()
        assert sha256(data).hexdigest() == journal.pin
        assert json.loads(data)["snapshot"] == parent
        with (history / "2.json").open("xb") as stream:
            stream.write(request.content)
        raise httpx.ReadTimeout("reply lost after commit")

    def read_revision(authority, identity, revision):
        reads.append((authority, identity, revision))
        return {"revision": revision, "element": json.loads((history / f"{revision}.json").read_bytes())}

    def recover():
        raw = journal.committed.read_bytes() if journal.committed.exists() else b""
        return save.svc.recover_recipe_save(ask, raw, *scope, journal_sha256=journal.pin,
                                            read_revision=read_revision)

    def submit():
        return save.svc.edit_inspection_recipe("Retitle the inspection table as East roof.", parent,
                                               *scope, record_attempt=journal)

    save.svc.transport = httpx.MockTransport(commit)
    assert author_tool(ask)[0]["engine_op"] == "recipe_save_recovery"
    refused = submit()
    assert refused["error"]["code"] == "unavailable" and "result" not in refused
    steps = ["write", "flush", "rename"]
    assert journal.events == steps[:{"short_write": 1, "flush": 2, "rename": 3}[fault]]
    assert journal.staged.is_file() and not journal.committed.exists()
    assert journal.pin is None and recover()["error"]["code"] == "outcome_unknown"
    assert reads == writes == [] and not (history / "2.json").exists()

    journal.fault = None
    lost = submit()
    assert lost["error"]["code"] == "outcome_unknown"
    assert journal.events[-3:] == steps
    for _ in range(2):
        assert recover() == {"result": {"revision": 2, "element": desired}}
    assert reads == [(save.auth, original["id"], 2)] * 2
    assert writes == ["PUT"] and sorted(p.name for p in history.iterdir()) == ["1.json", "2.json"]
