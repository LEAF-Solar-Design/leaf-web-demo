"""Keep the exact source bytes and run edits only on strictly decoded JSON."""
from hashlib import sha256

import pytest

from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_integration_tool_service import rig
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback
import write_loop


CASES = [
    ('Edit the selected JSON material: set "/0/mark" to 1.', b"caf\xc3\xa9", "caf\u00e9", b"\xff", 1),
    ('Patch my material records: set "/0/mark" to 2.', b"\xe6\xa2\x81", "\u6881", b"\xe6\xa2", 2),
    ('Update the imported JSON records: set "/0/mark" to 3.', b"\xf0\x9f\x8c\x9e", "\U0001f31e", b"\xc0\xaf", 3),
]


@pytest.mark.parametrize("ask,utf8,label,invalid,mark", CASES)
def test_import_encoding(local, monkeypatch, ask, utf8, label, invalid, mark):
    scope, ledger, materials = local
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_array_patch"
    prefix = b'{"items":[{"id":"east","mark":0,"label":"'
    suffix = b'"}]}'
    good = prefix + utf8 + suffix
    bad = prefix + invalid + suffix
    backend = write_loop.upload_backend_for_tenant(scope[0])
    with monkeypatch.context() as checks:
        checks.setattr(backend, "put_if_absent_or_verify", lambda *_: pytest.fail("invalid original reached storage"))
        with pytest.raises(originals.InvalidReference, match="UTF-8"):
            originals.prepare_and_store(*scope[:2], "material.json", "application/json", bad)
    row = {"id": "east", "mark": 0, "label": label}
    (_, mid, context), _ = pin_selection(scope, 1, "items", [row], ledger, materials, raw=good)
    intake = bind_selection({}, *scope, mid, context)
    status, result = tool_loader._run_source_in_sandbox(source, "encoded_material.py", intake, {})
    assert status == "ok", result
    assert result[0]["records"] == [{**row, "mark": mark}]
    assert result[0]["source_sha256"] == sha256(good).hexdigest()
    assert originals.read_content(*scope[:2], materials[mid]) == good
