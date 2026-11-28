"""Recovery matches the original scoped digest exactly, preserving Unicode distinctions."""
from hashlib import sha256
import json
import re
import unicodedata

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
from tools_fallback import author_tool


CASES = [
    ("Recover the missing receipt for my inspection recipe save.", "Caf\u00e9", "composed"),
    ("Did my table edit land after the save response timed out?", "Cafe\u0301", "decomposed"),
    ("Retry my recipe save after the connection dropped.", "C\u0430fe", "confusable"),
]


@pytest.mark.parametrize("ask,title,form", CASES)
def test_unicode_retry_keys_require_exact_identity(save, tmp_path, ask, title, form):
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    original = {**recipe(), "id": "inspection-east"}
    parent = {"revision": 1, "element": original}
    journal = tmp_path / "intent.json"
    successor = tmp_path / "successor.json"
    writes, reads = [], []

    def record(authority, entry):
        assert authority == save.auth
        journal.write_bytes(json.dumps(entry, ensure_ascii=False).encode())

    def peer(request):
        writes.append((request.method, request.headers["If-Match"]))
        with successor.open("xb") as out:
            out.write(request.content)
        raise httpx.ReadTimeout("response lost")

    def read(authority, identity, revision):
        reads.append((authority, identity, revision))
        return {"element": json.loads(successor.read_bytes()), "revision": revision}

    def recover(entry):
        raw = json.dumps(entry, ensure_ascii=False).encode()
        return save.svc.recover_recipe_save(ask, raw, *scope, read_revision=read,
                                            journal_sha256=sha256(raw).hexdigest())

    save.svc.transport = httpx.MockTransport(peer)
    edit = f"Retitle the inspection table as {title}."
    issued = save.svc.edit_inspection_recipe(edit, parent, *scope, record_attempt=record)
    assert issued["error"]["code"] == "outcome_unknown"
    entry = json.loads(journal.read_bytes())
    key = entry["retry_key"]
    assert re.fullmatch("[a-f0-9]{64}", key) and issued["retry_key"] == key
    assert author_tool(ask)[0]["engine_op"] == "recipe_save_recovery"

    if form == "composed":
        variant = "\u00e9" + key[1:]
        other_title = unicodedata.normalize("NFD", title)
    elif form == "decomposed":
        variant = "e\u0301" + key[1:]
        other_title = unicodedata.normalize("NFC", title)
    else:
        variant = chr(ord(key[0]) + 0xFEE0) + key[1:]
        assert unicodedata.normalize("NFKC", variant) == key
        other_title = title.replace("\u0430", "a")
    assert title.encode() != other_title.encode()
    for wrong in ({**entry, "retry_key": variant},
                  {**entry, "description": edit.replace(title, other_title)}):
        result = recover(wrong)
        assert result["error"]["code"] == "outcome_unknown" and "result" not in result
    assert reads == []
    expected = {"element": {**original, "title": title}, "revision": 2}
    assert recover(entry) == {"result": expected}
    assert reads == [(save.auth, original["id"], 2)]
    assert writes == [("PUT", "1")]
    assert json.loads(successor.read_bytes()) == expected["element"]
