"""Check numeric JSON edits against rational arithmetic on the original lexemes."""
from fractions import Fraction
import json

import pytest

from authored_material_revision import bind_material_revisions
from test_integration_tool_service import rig
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


@pytest.mark.parametrize("ask,old,new,equivalent", [
    ("Compare the numeric fields in my imported JSON revisions.",
     "9007199254740992", "9007199254740993", "9007199254740993.0"),
    ("Diff the precision of the measurement values between material versions.",
     "0.1234567890123456789012345678901", "0.1234567890123456789012345678902",
     "12345678901234567890123456789020e-32"),
    ("Which numeric properties changed across the JSON revisions?",
     "9007199254740993.00000000000000001", "9007199254740993.00000000000000002",
     "900719925474099300000000000000002e-17"),
])
def test_numeric_changes_survive_author_executor(local, ask, old, new, equivalent):
    scope, ledger, materials = local
    assert Fraction(old) != Fraction(new) == Fraction(equivalent)
    assert float(old) == float(new)
    pins = []
    for revision, changed, stable in ((1, old, new), (2, new, equivalent)):
        raw = ('{"rows":[{"id":"changed","amount":' + changed + '},{"id":"stable","amount":' + stable + '}]}').encode()
        selected_rows = json.loads(raw, parse_float=str)["rows"]
        pin, _ = pin_selection(scope, revision, "rows", selected_rows, ledger, materials, raw=raw)
        pins.append(pin)
    intake = bind_material_revisions({}, ledger, *scope, "inspection", *pins, description=ask)
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_revision_diff"
    status, payload = tool_loader._run_source_in_sandbox(source, "numeric_diff.py", intake, {})
    assert status == "ok", payload
    row, = payload[0]["changes"]
    assert row["id"] == "changed" and row["edited"]
    assert row["before"]["sha256"] != row["after"]["sha256"]
    field, = row["fields"]
    for side, token in (("before", old), ("after", new)):
        state = field[side]
        assert state["present"] and state["pointer"] == "/rows/0/amount"
        assert Fraction(str(state["value"])) == Fraction(token)
        if isinstance(state["value"], str):
            assert state["value_type"] == "decimal"
