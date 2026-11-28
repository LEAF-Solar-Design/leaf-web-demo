"""Bound journal recovery before consulting the immutable recipe revisions."""
from hashlib import sha256
import json

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
import authored_recipe_retry as retry
from integration_tool_service import REQUEST_CAP
from tools_fallback import author_tool


CASES = [
    ("Recover my inspection recipe save receipt.", "bytes"),
    ("Did my table edit save before the response timed out?", "depth"),
    ("Retry the element update after the reply was lost.", "bytes_and_depth"),
]


def container_depth(value):
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return 0
    return 1 + max((container_depth(child) for child in children), default=0)


@pytest.mark.parametrize("ask,limit", CASES)
def test_recovery_journal_limits(save, tmp_path, monkeypatch, ask, limit):
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    journal = tmp_path / "journal.json"
    revision = tmp_path / "revision.json"
    writes = []
    reads = []
    original = {**recipe(), "id": "inspection-roof"}
    # Structural characters, escaped quotes, and multibyte text are ordinary title data.
    title = 'Roof "A" [{{{{{{{{{{ café'
    expected = {"revision": 2, "element": {**original, "title": title}}

    def commit(request):
        assert request.method == "PUT" and request.headers["If-Match"] == "1"
        writes.append(request.content)
        revision.write_bytes(request.content)
        raise httpx.ReadTimeout("commit succeeded but response lost")

    def read_successor(authority, identity, number):
        reads.append((authority, identity, number))
        return {"revision": number, "element": json.loads(revision.read_bytes())}

    def recover(raw):
        journal.write_bytes(raw)
        return save.svc.recover_recipe_save(ask, journal.read_bytes(), *scope,
                                            journal_sha256=sha256(raw).hexdigest(),
                                            read_revision=read_successor)

    save.svc.transport = httpx.MockTransport(commit)
    lost = save.svc.edit_inspection_recipe(
        f"Retitle this inspection table as {title}.", {"revision": 1, "element": original}, *scope,
        record_attempt=lambda _, entry: journal.write_bytes(json.dumps(entry, ensure_ascii=False).encode()))
    assert lost["error"]["code"] == "outcome_unknown"
    committed = revision.read_bytes()
    assert json.loads(committed) == expected["element"]
    assert author_tool(ask)[0]["engine_op"] == "recipe_save_recovery"
    good = journal.read_bytes()
    assert len(good) > len(good.decode())
    byte_limit = 2*REQUEST_CAP + 8192
    pinned_depth = container_depth(json.loads(good))
    if "bytes" in limit:
        # Legal trailing whitespace changes only byte count, preserving retry identity.
        good = good.ljust(byte_limit, b" ")
        rejected = recover(good + b" ")
        assert rejected["error"]["code"] == "journal_byte_limit"
    if "depth" in limit:
        monkeypatch.setattr(retry, "JOURNAL_MAX_DEPTH", pinned_depth)
        # One more container than the boundary; use both arrays and objects.
        nested = None
        for i in range(pinned_depth):
            nested = [nested] if i % 2 == 0 else {"next": nested}
        deeper = {**json.loads(good), "metadata": nested}
        assert container_depth(deeper) == pinned_depth+1
        rejected = recover(json.dumps(deeper).encode())
        assert rejected["error"]["code"] == "journal_depth_limit"
    assert reads == [] and writes == [committed] and revision.read_bytes() == committed
    assert recover(good) == {"result": expected}
    assert reads == [(save.auth, original["id"], 2)] and writes == [committed]
