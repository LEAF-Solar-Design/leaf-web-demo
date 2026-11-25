"""Compare exact UTF-8 identities through the material author and sandbox."""
import pytest

from test_integration_tool_service import rig
from authored_material_revision import bind_material_revisions
from test_x03_material_revision_author import local, pin_selection
import tool_loader
import tools_fallback


# Pointer suffixes are independent byte literals. No normalization or product escape
# helper constructs these expectations. The pair occurs in the same JSON object.
CASES = [
    ("Diff the accented field keys between my imported JSON revisions.",
     "caf\u00e9", "cafe\u0301", b"caf\xc3\xa9", b"cafe\xcc\x81"),
    ("Compare the Latin and Cyrillic properties across these material versions.",
     "a", "\u0430", b"a", b"\xd0\xb0"),
    ("Show which slash and tilde keys changed in the JSON revisions.",
     "~/\u00c5", "~/A\u030a", b"~0~1\xc3\x85", b"~0~1A\xcc\x8a"),
]


@pytest.mark.parametrize("ask,first,second,first_bytes,second_bytes", CASES)
def test_distinct_unicode_field_paths(local, ask, first, second, first_bytes, second_bytes):
    scope, ledger, materials = local
    key = "r\u00e9s/~"
    fixed = {"id": "fixed"}
    old = {"id": "target", first: 17, second: 29}
    new = {"id": "target", second: 41}
    previous, _ = pin_selection(scope, 1, key, [fixed, old], ledger, materials)
    current, _ = pin_selection(scope, 2, key, [new, fixed], ledger, materials)
    intake = bind_material_revisions({}, ledger, *scope, "inspection", previous, current,
                                     description=ask)
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["engine_op"] == "material_revision_diff"
    status, payload = tool_loader._run_source_in_sandbox(source, "unicode_diff.py", intake, {})
    assert status == "ok", payload
    report, _ = payload
    target, = [row for row in report["changes"] if row["id"] == "target"]
    assert target["moved"] and target["edited"]
    assert len(target["fields"]) == 2
    base = b"/r\xc3\xa9s~1~0/"
    for side, prefix, expected in (
        ("before", base + b"1/", {first_bytes: (True, 17), second_bytes: (True, 29)}),
        ("after", base + b"0/", {first_bytes: (False, None), second_bytes: (True, 41)}),
    ):
        states = [field[side] for field in target["fields"]]
        assert {s["pointer"].encode("utf-8"): (s["present"], s.get("value")) for s in states} == {
            prefix + suffix: value for suffix, value in expected.items()}
        assert all(("value" in s) == s["present"] for s in states)
