"""Style-aware export oracle (studio lane F, P-060 residual).

Oracle: the separate style receipt in engine/export_fidelity.py fails when a
controlled mutation changes one supported style property (colour, true
colour, linetype, lineweight) on a keyed entity, a handle-less entity or a
block child, passes every unchanged tracked drawing, and leaves the geometry
receipt's keys and verdict untouched. Runs without the compiled engine.

Run:  cd server && python -m pytest tests/test_export_style_fidelity.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = PROJECT_ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import corpus_harness as harness  # noqa: E402
import export_fidelity as fidelity  # noqa: E402

SOURCE_NAME = "styled.dxf"


def _chunk(*pairs) -> str:
    return "".join(f"{code}\n{value}\n" for code, value in pairs)


BLOCK_HEAD = _chunk((0, "SECTION"), (2, "BLOCKS"), (0, "BLOCK"), (5, "40"), (8, "0"),
                    (2, "Panel"), (70, "0"), (10, "0"), (20, "0"), (30, "0"))
BLOCK_LINE = _chunk((0, "LINE"), (5, "41"), (8, "0"), (62, "0"), (6, "ByBlock"), (370, "25"),
                    (10, "0"), (20, "0"), (30, "0"), (11, "1"), (21, "0"), (31, "0"))
BLOCK_CIRCLE = _chunk((0, "CIRCLE"), (8, "0"), (62, "5"), (10, "0"), (20, "0"), (30, "0"),
                      (40, "0.5"))
BLOCK_TAIL = _chunk((0, "ENDBLK"), (5, "42"), (8, "0"), (0, "ENDSEC"))
ENTITIES_HEAD = _chunk((0, "SECTION"), (2, "ENTITIES"))
LINE = _chunk((0, "LINE"), (5, "A1"), (8, "Wires"), (62, "1"), (6, "DASHED"), (370, "35"),
              (10, "0"), (20, "0"), (30, "0"), (11, "10"), (21, "0"), (31, "0"))
LWPOLYLINE = _chunk((0, "LWPOLYLINE"), (5, "A2"), (8, "Wires"), (420, "16711680"), (62, "1"),
                    (90, "2"), (70, "0"), (10, "0"), (20, "5"), (10, "5"), (20, "5"))
CIRCLE = _chunk((0, "CIRCLE"), (8, "Wires"), (6, "HIDDEN"), (10, "3"), (20, "3"), (30, "0"),
                (40, "1"))
ARC = _chunk((0, "ARC"), (5, "A3"), (8, "Wires"), (370, "50"), (10, "0"), (20, "0"), (30, "0"),
             (40, "2"), (50, "0"), (51, "90"))
INSERT = _chunk((0, "INSERT"), (5, "A4"), (8, "0"), (2, "Panel"), (62, "3"), (10, "20"),
                (20, "20"), (30, "0"))
TAIL = _chunk((0, "ENDSEC"), (0, "EOF"))


def _drawing(*, block_children=(BLOCK_LINE, BLOCK_CIRCLE),
             entities=(LINE, LWPOLYLINE, CIRCLE, ARC, INSERT)) -> str:
    return BLOCK_HEAD + "".join(block_children) + BLOCK_TAIL + ENTITIES_HEAD + "".join(entities) + TAIL


FIXTURE = _drawing()


def _mutate(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"mutation anchor {old!r} must occur exactly once"
    return text.replace(old, new)


def _styles(after: str, before: str = FIXTURE) -> dict:
    return fidelity.compare_styles(before.encode(), after.encode(), source_name=SOURCE_NAME)


def _style_ok(result: dict) -> bool:
    return (result["style_preserved"] == result["style_entities"]
            and result["block_children_preserved"] == result["block_children"])


def _geometry_ok(after: str, before: str = FIXTURE) -> bool:
    geometry = fidelity.compare(before.encode(), after.encode(), source_name=SOURCE_NAME)
    return (
        geometry["entities_before"] == geometry["entities_after"]
        and geometry["handles_preserved"] == geometry["source_handles"]
        and geometry["unkeyed_matched"] == geometry["unkeyed_before"]
        and geometry["block_records_preserved"] == geometry["block_records"]
        and geometry["_block_unkeyed_match"]
        and geometry["blocks_match"] and geometry["layers_match"]
    )


class _ReplaceAdapter(harness.EngineAdapter):
    name = "replace"

    def __init__(self, old: bytes, new: bytes):
        self.old, self.new = old, new

    def round_trip(self, dxf_bytes: bytes) -> bytes:
        assert dxf_bytes.count(self.old) == 1
        return dxf_bytes.replace(self.old, self.new)


class _RaisingAdapter(harness.EngineAdapter):
    name = "raising"

    def round_trip(self, dxf_bytes: bytes) -> bytes:
        raise RuntimeError("boom")


def test_the_geometry_receipt_keys_are_unchanged_and_the_style_keys_are_separate():
    assert fidelity.TRACKED_RECEIPT_KEYS == (
        "drawing", "adapter", "ok", "timing_ms", "bytes_before", "bytes_after",
        "byte_identical", "entities_before", "entities_after", "source_handles",
        "handles_preserved", "handles_missing", "handles_changed", "unkeyed_before",
        "unkeyed_matched", "blocks_match", "block_records", "block_records_preserved",
        "block_records_missing", "block_records_changed", "layers_match",
        "source_untouched", "error",
    )
    assert fidelity.STYLE_RECEIPT_KEYS == (
        "drawing", "adapter", "ok", "timing_ms", "styled_before", "style_entities",
        "style_preserved", "style_missing", "style_changed", "block_children",
        "block_children_preserved", "block_children_missing", "block_children_changed",
        "source_untouched", "error",
    )
    geometry = fidelity.run_drawing(harness.IdentityAdapter(), "web/e2e/fixtures/block-fixture.dxf")
    assert tuple(geometry) == fidelity.TRACKED_RECEIPT_KEYS
    assert geometry["ok"] is True


def test_an_unchanged_styled_drawing_passes_every_entity_and_block_child():
    result = _styles(FIXTURE)
    assert result == {
        "styled_before": 5, "style_entities": 5, "style_preserved": 5,
        "style_missing": [], "style_changed": [],
        "block_children": 2, "block_children_preserved": 2,
        "block_children_missing": [], "block_children_changed": [],
    }
    assert _geometry_ok(FIXTURE)


ENTITY_MUTATIONS = [
    ("aci colour", "\n62\n1\n6\nDASHED\n", "\n62\n2\n6\nDASHED\n", "A1"),
    ("linetype", "\n6\nDASHED\n", "\n6\nCENTER\n", "A1"),
    ("lineweight", "\n370\n35\n", "\n370\n18\n", "A1"),
    ("true colour", "\n420\n16711680\n", "\n420\n65280\n", "A2"),
    ("true colour dropped", "\n420\n16711680\n", "\n", "A2"),
    ("lineweight dropped", "\n370\n50\n", "\n", "A3"),
    ("insert colour", "\n62\n3\n", "\n62\n4\n", "A4"),
    ("handle-less linetype", "\n6\nHIDDEN\n", "\n6\nPHANTOM\n", "circles[0]"),
]


@pytest.mark.parametrize("old,new,label", [m[1:] for m in ENTITY_MUTATIONS],
                         ids=[m[0] for m in ENTITY_MUTATIONS])
def test_each_entity_style_mutation_fails_style_and_keeps_geometry(old, new, label):
    mutated = _mutate(FIXTURE, old, new)
    result = _styles(mutated)
    assert result["style_changed"] == [label]
    assert result["style_missing"] == []
    assert result["style_preserved"] == result["style_entities"] - 1
    assert not _style_ok(result)
    assert result["block_children_changed"] == []
    assert _geometry_ok(mutated)


BLOCK_CHILD_MUTATIONS = [
    ("child ByBlock colour to ByLayer", "\n62\n0\n", "\n62\n256\n", "Panel#0"),
    ("child ByBlock linetype to ByLayer", "\n6\nByBlock\n", "\n6\nByLayer\n", "Panel#0"),
    ("child lineweight", "\n370\n25\n", "\n370\n13\n", "Panel#0"),
    ("child aci colour", "\n62\n5\n", "\n62\n7\n", "Panel#1"),
]


@pytest.mark.parametrize("old,new,label", [m[1:] for m in BLOCK_CHILD_MUTATIONS],
                         ids=[m[0] for m in BLOCK_CHILD_MUTATIONS])
def test_each_block_child_style_mutation_fails_style_and_keeps_geometry(old, new, label):
    mutated = _mutate(FIXTURE, old, new)
    result = _styles(mutated)
    assert result["block_children_changed"] == [label]
    assert result["block_children_missing"] == []
    assert result["block_children_preserved"] == result["block_children"] - 1
    assert result["style_changed"] == []
    assert not _style_ok(result)
    assert _geometry_ok(mutated)


NORMALIZATIONS = [
    ("explicit ByLayer defaults", "\n5\nA3\n", "\n5\nA3\n62\n256\n6\nBYLAYER\n"),
    ("linetype name case", "\n6\nDASHED\n", "\n6\ndashed\n"),
    ("true colour wins over its aci fallback", "\n420\n16711680\n62\n1\n",
     "\n420\n16711680\n62\n2\n"),
    ("engine assigns a handle to a handle-less entity", "\n0\nCIRCLE\n8\nWires\n",
     "\n0\nCIRCLE\n5\nB0\n8\nWires\n"),
]


@pytest.mark.parametrize("old,new", [m[1:] for m in NORMALIZATIONS],
                         ids=[m[0] for m in NORMALIZATIONS])
def test_normalized_equivalents_pass_style(old, new):
    mutated = _mutate(FIXTURE, old, new)
    assert _style_ok(_styles(mutated))
    assert _geometry_ok(mutated)


def test_a_missing_entity_and_a_missing_block_child_fail_style():
    no_line = _styles(_drawing(entities=(LWPOLYLINE, CIRCLE, ARC, INSERT)))
    assert no_line["style_missing"] == ["A1"]
    assert not _style_ok(no_line)
    no_circle = _styles(_drawing(entities=(LINE, LWPOLYLINE, ARC, INSERT)))
    assert no_circle["style_missing"] == ["circles[0]"]
    assert not _style_ok(no_circle)
    no_child = _styles(_drawing(block_children=(BLOCK_LINE,)))
    assert no_child["block_children_missing"] == ["Panel#1"]
    assert not _style_ok(no_child)


def test_style_key_rules():
    default = fidelity.style_key(None)
    assert default == (("aci", 256), "BYLAYER", -1)
    assert fidelity.style_key({"aci": 256, "rgb": None, "linetype": "ByLayer",
                               "lineweight": -1}) == default
    assert fidelity.style_key({"aci": 0, "rgb": None, "linetype": "ByBlock",
                               "lineweight": -2}) != default
    assert fidelity.style_key({"aci": 1, "rgb": [255, 0, 0], "linetype": "ByLayer",
                               "lineweight": -1}) == fidelity.style_key(
        {"aci": 2, "rgb": [255, 0, 0], "linetype": "ByLayer", "lineweight": -1})
    assert fidelity.style_key({"aci": 1, "rgb": [255, 0, 0], "linetype": "ByLayer",
                               "lineweight": -1}) != fidelity.style_key(
        {"aci": 1, "rgb": None, "linetype": "ByLayer", "lineweight": -1})


def test_every_tracked_drawing_passes_the_style_receipt_unchanged():
    receipts = fidelity.run_tracked_styles(harness.IdentityAdapter())
    assert [r["drawing"] for r in receipts] == list(fidelity.TRACKED_DRAWINGS)
    for receipt in receipts:
        assert tuple(receipt) == fidelity.STYLE_RECEIPT_KEYS
        assert receipt["ok"] is True, receipt
        assert receipt["error"] is None and receipt["source_untouched"] is True
    block = receipts[fidelity.TRACKED_DRAWINGS.index("web/e2e/fixtures/block-fixture.dxf")]
    assert block["block_children"] == 1 and block["block_children_preserved"] == 1


def test_a_tracked_block_child_linetype_mutation_fails_style_but_not_geometry():
    rel = "web/e2e/fixtures/block-fixture.dxf"
    # The fixture's only "ByLayer" is its block child's linetype (group 6).
    adapter = _ReplaceAdapter(b"ByLayer", b"DASHED")
    style = fidelity.run_drawing_styles(adapter, rel)
    assert style["ok"] is False
    assert style["block_children_changed"] == ["Fixture#0"]
    assert style["error"] is None and style["source_untouched"] is True
    geometry = fidelity.run_drawing(adapter, rel)
    assert geometry["ok"] is True


def test_an_adapter_error_fails_the_style_receipt_closed():
    receipt = fidelity.run_drawing_styles(_RaisingAdapter(), "web/e2e/fixtures/block-fixture.dxf")
    assert receipt["ok"] is False
    assert receipt["error"] == "RuntimeError: boom"
    assert receipt["source_untouched"] is True
    assert all(receipt[key] is None for key in fidelity.STYLE_RECEIPT_KEYS[4:-2])


def test_style_cli_exits_zero_with_identity_adapter(capsys):
    assert fidelity.main(["--adapter=identity", "--styles"]) == 0
    assert "# STYLE RESULT: ALL DRAWINGS OK (8 drawings, adapter='identity')" in capsys.readouterr().out
