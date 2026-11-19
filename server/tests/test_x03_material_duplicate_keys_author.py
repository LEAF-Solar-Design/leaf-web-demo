"""Retain raw material evidence when an object contains duplicate members."""
from hashlib import sha256

import pytest

from test_integration_tool_service import rig
from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback
import write_loop


CASES = [
    ('Edit the selected JSON material: set "/0/label" to "revised".',
     b'{"attributes":{"pitch":1,"pitch":2}}', "revised"),
    ('Patch my material records: copy "/0/alias" to "/0/label".',
     br'{"layers":[{"name":"A","\u006eame":"B"}]}', "west"),
    ('Update the imported JSON records: test "/0/id" equals "west", then set "/0/label" to "checked".',
     b'{"members":[{"spec":{"rating":true,"rating":false}}]}', "checked"),
]


@pytest.mark.parametrize("ask,details,expected_label", CASES)
def test_raw_duplicate_blocks_import_and_edit(local, tmp_path, ask, details, expected_label):
    scope, ledger, materials = local
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_array_patch"
    bad = b'{"items":[{"id":"west","label":"old","details":' + details + b'}]}'
    path = tmp_path / "ambiguous.json"
    path.write_bytes(bad)
    with pytest.raises(originals.InvalidReference, match="(?i)duplicate.*key"):
        originals.prepare_and_store(*scope[:2], path.name, "application/json", path.read_bytes())
    assert path.read_bytes() == bad

    # Unique members can use the same name in different objects.
    row = {"id": "west", "alias": "west", "label": "old", "details": [{"rating": True}, {"rating": False}]}
    (_, mid, context), good = pin_selection(scope, 1, "items", [row], ledger, materials)
    intake = bind_selection({}, *scope, mid, context)
    status, result = tool_loader._run_source_in_sandbox(source, "material_duplicate.py", intake, {})
    assert status == "ok", result
    assert result[0]["records"] == [{**row, "label": expected_label}]
    assert originals.read_content(*scope[:2], materials[mid]) == good

    # Exercise the bytes-to-values boundary for an already stored document.
    digest = sha256(bad).hexdigest()
    backend = write_loop.upload_backend_for_tenant(scope[0])
    backend.put_if_absent_or_verify(originals.object_key(*scope[:2], digest), bad)
    materials[mid] = {**materials[mid], "sha256": digest, "byte_size": len(bad)}
    context["source"].update(sha256=digest, byte_length=len(bad))
    with pytest.raises(ValueError, match="(?i)duplicate.*key"):
        bind_selection({}, *scope, mid, context)
    assert originals.read_content(*scope[:2], materials[mid]) == bad
