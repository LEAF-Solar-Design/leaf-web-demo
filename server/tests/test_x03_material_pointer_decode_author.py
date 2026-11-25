"""Apply plain JSON Pointers to material fields with colliding escaped names."""
from hashlib import sha256

import pytest

from test_integration_tool_service import rig
from authored_material_patch import bind_selection
import project_reference_materials as originals
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


FIELDS = {"~1": "tilde", "/": "slash", "%2F": "percent", "~2": "two", "~": "other"}
CASES = [
    ('Edit the selected JSON material: set "/0/~01" to "beam".', "~1", "beam", "~2"),
    ('Patch my material records: copy "/0/~01" to "/0/~1".', "/", "tilde", "~"),
    ('Update the imported JSON records: set "/0/%2F" to "roof".', "%2F", "roof", "~20"),
]


@pytest.mark.parametrize("ask,key,value,malformed", CASES)
def test_material_pointer_decodes_once(local, ask, key, value, malformed):
    scope, ledger, materials = local
    original = {"id": "west", **FIELDS}
    (_, mid, context), raw = pin_selection(scope, 1, "items", [original], ledger, materials)
    intake = bind_selection({}, *scope, mid, context)
    guarded = ask[:-1] + f', then set "/0/{malformed}" to "invalid".'
    for prompt in (guarded, ask):
        package, source, _ = tools_fallback.author_tool(prompt)
        assert package["engine_op"] == "material_array_patch"
        status, result = tool_loader._run_source_in_sandbox(source, "material_pointer.py", intake, {})
        if prompt == guarded:
            assert status == "tool_error" and "Invalid material JSON pointer" in result
        else:
            assert status == "ok", result
            report, _ = result
            assert report["records"] == [{**original, key: value}]
            assert report["source_sha256"] == sha256(raw).hexdigest()
        assert originals.read_content(*scope[:2], materials[mid]) == raw
        assert intake["checker_material_selection"]["record_values"] == [original]
