"""Exponent-form material values survive exactly or receive a range error."""
from decimal import Decimal
from hashlib import sha256

import pytest

from authored_material_patch import bind_selection
from authored_material_selection import bind_material_selection
import project_reference_materials as originals
from test_integration_tool_service import rig
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


CASES = [
    ('Edit the selected JSON material: copy "/0/value" to "/0/result".', "1e400", "1e3", True),
    ('Patch my material records: set "/0/result" to 7.', "1e-4000", "125e-2", False),
    ('Update the imported JSON records: test "/0/id" equals "west", then copy "/0/value" to "/0/result".',
     "-1e400", "-25e-1", True),
]


@pytest.mark.parametrize("ask,extreme,control,copy", CASES)
def test_exponent_range(local, ask, extreme, control, copy):
    scope, ledger, materials = local
    tool, source, _ = tools_fallback.author_tool(ask)
    assert tool["engine_op"] == "material_array_patch"
    for version, literal in enumerate([extreme, control], 1):
        raw = ('{"items":[{"id":"west","value":' + literal + ',"result":0}]}').encode()
        (_, mid, context), _ = pin_selection(scope, version, "items", [None], ledger, materials, raw=raw)
        if literal == extreme:
            with pytest.raises(ValueError, match="(?i)range"):
                bind_material_selection({}, *scope, mid, context)
        intake = bind_selection({}, *scope, mid, context)
        status, result = tool_loader._run_source_in_sandbox(source, "edit_values.py", intake, {})
        value = Decimal(literal)
        if literal == "1e-4000":
            assert status == "tool_error" and "range" in str(result).lower()
        else:
            assert status == "ok", result
            report, overlay = result
            row, = report["records"]
            assert overlay is None and Decimal(str(row["value"])) == value
            assert Decimal(str(row["result"])) == (value if copy else Decimal(7))
            assert report["source_sha256"] == sha256(raw).hexdigest()
        assert originals.read_content(*scope[:2], materials[mid]) == raw
