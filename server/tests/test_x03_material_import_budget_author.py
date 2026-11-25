"""Apply the material byte and nesting budgets before JSON decoding or storage."""
from hashlib import sha256
import json
from unittest.mock import Mock

import pytest

from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_integration_tool_service import rig
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback
import write_loop


CASES = [
    ('Edit the selected JSON material: set "/0/label" to "bounded".', "bytes", "bounded"),
    ('Patch my material records: copy "/0/name" to "/0/label".', "arrays", "mill"),
    ('Update the imported JSON records: test "/0/id" equals "b", then set "/0/label" to "verified".',
     "objects", "verified"),
]


@pytest.mark.parametrize("ask,boundary,expected", CASES)
def test_preparse_material_budget(local, monkeypatch, ask, boundary, expected):
    scope, ledger, materials = local
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_array_patch"
    nested = [1] if boundary == "arrays" else {"v": 1}
    row = {"id": "b", "name": "mill", "label": '[[["\\', "detail": nested}
    def encode(record):
        return json.dumps({"items": [record]}, separators=(",", ":")).encode("utf-8")
    raw = encode(row)
    too_deep = [nested] if boundary == "arrays" else {"x": nested}
    rejected = raw + b" " if boundary == "bytes" else encode({**row, "detail": too_deep})
    digest = sha256(raw).hexdigest()
    rejected_digest = sha256(rejected).hexdigest()
    monkeypatch.setattr(originals, "TEXT_MAX_BYTES", len(raw) if boundary == "bytes" else len(rejected))
    monkeypatch.setattr(originals, "MATERIAL_JSON_MAX_DEPTH", 4, raising=False)
    backend = write_loop.upload_backend_for_tenant(scope[0])
    with monkeypatch.context() as checks:
        parser = Mock(side_effect=AssertionError("parser called before budget check"))
        writer = Mock(side_effect=AssertionError("writer called before budget check"))
        checks.setattr(originals.json, "loads", parser)
        checks.setattr(backend, "put_if_absent_or_verify", writer)
        with pytest.raises(originals.InvalidReference, match="limit"):
            originals.prepare_and_store(*scope[:2], "material.json", "application/json", rejected)
        parser.assert_not_called()
        writer.assert_not_called()
    assert sha256(rejected).hexdigest() == rejected_digest
    (_, mid, context), _ = pin_selection(scope, 1, "items", [row], ledger, materials, raw=raw)
    intake = bind_selection({}, *scope, mid, context)
    status, result = tool_loader._run_source_in_sandbox(source, "bounded_import.py", intake, {})
    assert status == "ok", result
    assert result[0]["records"] == [{**row, "label": expected}]
    assert result[0]["source_sha256"] == digest
    assert originals.read_content(*scope[:2], materials[mid]) == raw
