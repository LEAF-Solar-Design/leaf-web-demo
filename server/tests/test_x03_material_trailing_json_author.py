"""Only JSON whitespace may follow the imported material's single root value."""
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
    ('Edit the selected JSON material: set "/0/tag" to "roof".',
     b'{"second":true}', b"\r\n", "roof"),
    ('Patch my material records: copy "/0/source" to "/0/tag".',
     b"\n false", b" \r\n\t ", "east"),
    ('Update the imported JSON records: test "/0/tag" equals "draft", then set "/0/tag" to "ready".',
     b" !unfinished", b"\t", "ready"),
]


def assert_import_and_patch(local, tmp_path, monkeypatch, ask, key, before, after, accepted, rejected):
    scope, ledger, materials = local
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_array_patch"
    source_file = tmp_path / "input.json"
    source_file.write_bytes(rejected)
    rejected_digest = sha256(rejected).hexdigest()
    backend = write_loop.upload_backend_for_tenant(scope[0])
    with monkeypatch.context() as checks:
        checks.setattr(backend, "put_if_absent_or_verify", lambda *_: pytest.fail("invalid original reached storage"))
        with pytest.raises(originals.InvalidReference):
            originals.prepare_and_store(*scope[:2], source_file.name, "application/json", source_file.read_bytes())
    assert source_file.read_bytes() == rejected
    assert sha256(source_file.read_bytes()).hexdigest() == rejected_digest
    (_, mid, context), _ = pin_selection(scope, 1, key, [before], ledger, materials, raw=accepted)
    intake = bind_selection({}, *scope, mid, context)
    status, result = tool_loader._run_source_in_sandbox(source, "material_boundary.py", intake, {})
    assert status == "ok", result
    report, overlay = result
    assert overlay is None and report["records"] == [after]
    assert report["source_sha256"] == sha256(accepted).hexdigest()
    assert intake["checker_material_selection"]["record_values"] == [before]
    assert originals.read_content(*scope[:2], materials[mid]) == accepted


@pytest.mark.parametrize("ask,suffix,white,tag", CASES)
def test_single_json_root(local, tmp_path, monkeypatch, ask, suffix, white, tag):
    raw = b'{"items":[{"id":"east","tag":"draft","source":"east"}]}'
    before = {"id": "east", "tag": "draft", "source": "east"}
    assert not suffix.isspace() and all(byte in b" \t\r\n" for byte in white)
    assert_import_and_patch(local, tmp_path, monkeypatch, ask, "items", before,
                            {**before, "tag": tag}, raw + white, raw + suffix)
