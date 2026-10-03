"""Style-aware export oracle (studio lane F, P-060 residual).

Oracle: the separate style receipt in engine/export_fidelity.py fails when a
controlled mutation changes one supported style property (colour, true
colour, linetype, lineweight) on a keyed entity, a handle-less entity or a
block child, passes every unchanged tracked drawing, and leaves the geometry
receipt's keys and verdict untouched. Runs without the compiled engine.

The declared-style proof (STU3-STYLE) reads raw records: layer, colour,
linetype, lineweight and text style on every ENTITIES record and block child,
judged with the geometry receipt on one round trip. Fake adapters only.

Run:  cd server && python -m pytest tests/test_export_style_fidelity.py -q
"""
from __future__ import annotations

import json
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


# --- Declared-style proof (STU3-STYLE) -------------------------------------

DECLARED = (
    _chunk((0, "SECTION"), (2, "BLOCKS"),
           (0, "BLOCK"), (5, "B0"), (8, "0"), (2, "*U1"), (70, "1"),
           (10, "0"), (20, "0"), (30, "0"),
           (0, "LINE"), (8, "Frame"), (62, "0"), (6, "ByBlock"), (370, "-2"),
           (10, "0"), (20, "0"), (30, "0"), (11, "1"), (21, "0"), (31, "0"),
           (0, "ATTDEF"), (5, "B2"), (8, "0"), (7, "Notes"), (10, "0"), (20, "0"),
           (30, "0"), (40, "1"), (1, "x"), (3, "Tag?"), (2, "TAG"), (70, "0"),
           (0, "ENDBLK"), (5, "B3"), (8, "0"),
           (0, "ENDSEC"))
    + _chunk((0, "SECTION"), (2, "ENTITIES"),
             (0, "LINE"), (5, "A1"), (8, "Wires"), (62, "1"), (6, "DASHED"), (370, "35"),
             (10, "0"), (20, "0"), (30, "0"), (11, "10"), (21, "0"), (31, "0"),
             (0, "LINE"), (8, "Ground"), (10, "0"), (20, "1"), (30, "0"),
             (11, "10"), (21, "1"), (31, "0"),
             (0, "TEXT"), (5, "A2"), (8, "Notes"), (420, "16711680"), (62, "1"),
             (7, "Romans"), (10, "0"), (20, "0"), (30, "0"), (40, "1"), (1, "hello"),
             (0, "MTEXT"), (8, "Notes"), (10, "0"), (20, "0"), (30, "0"), (40, "1"),
             (1, "note"),
             (0, "INSERT"), (8, "0"), (66, "1"), (2, "*U1"), (10, "5"), (20, "5"), (30, "0"),
             (0, "ATTRIB"), (8, "0"), (7, "Annot"), (10, "5"), (20, "5"), (30, "0"),
             (40, "1"), (1, "v"), (2, "TAG"), (70, "0"),
             (0, "SEQEND"), (8, "0"),
             (0, "CIRCLE"), (5, "A3"), (10, "3"), (20, "3"), (30, "0"), (40, "1"),
             (0, "ENDSEC"), (0, "EOF"))
)


def _declared(after: str, before: str = DECLARED) -> dict:
    return fidelity.compare_declared_styles(before.encode(), after.encode(),
                                            source_name=SOURCE_NAME)


def test_an_unchanged_drawing_passes_every_declared_style_record():
    # Ten records: two anonymous-block children, eight ENTITIES records
    # (handle-less LINE, MTEXT, INSERT, ATTRIB and SEQEND among them). The
    # handle-less LINE passes only because output LINE A1 is excluded from
    # its candidate pool.
    assert _declared(DECLARED) == {
        "ok": True, "style_records": 10, "style_matched": 10, "style_mismatches": 0,
        "style_failures": {}, "diagnostics": [], "error": None,
    }
    assert tuple(_declared(DECLARED)) == fidelity.DECLARED_STYLE_KEYS


DECLARED_MUTATIONS = [
    ("layer", "\n8\nWires\n", "\n8\nOther\n", "A1", "layer", "WIRES", "OTHER"),
    ("aci colour", "\n62\n1\n6\nDASHED\n", "\n62\n2\n6\nDASHED\n", "A1", "color",
     "aci:1", "aci:2"),
    ("true colour", "\n420\n16711680\n", "\n420\n65280\n", "A2", "color",
     "rgb:FF0000", "rgb:00FF00"),
    ("true colour dropped", "\n420\n16711680\n", "\n", "A2", "color", "rgb:FF0000", "aci:1"),
    ("linetype", "\n6\nDASHED\n", "\n6\nCENTER\n", "A1", "linetype", "DASHED", "CENTER"),
    ("lineweight", "\n370\n35\n", "\n370\n18\n", "A1", "lineweight", 35, 18),
    ("text style", "\n7\nRomans\n", "\n7\nArial\n", "A2", "text_style", "ROMANS", "ARIAL"),
    ("attdef text style", "\n7\nNotes\n", "\n7\nArial\n", "B2", "text_style", "NOTES", "ARIAL"),
    ("handle-less attrib text style", "\n7\nAnnot\n", "\n7\nOther\n", "ATTRIB[0]",
     "text_style", "ANNOT", "OTHER"),
    ("handle-less mtext text style", "\n0\nMTEXT\n", "\n0\nMTEXT\n7\nArial\n", "MTEXT[0]",
     "text_style", "STANDARD", "ARIAL"),
    ("handle-less insert layer", "\n0\nINSERT\n8\n0\n", "\n0\nINSERT\n8\nRoof\n", "INSERT[0]",
     "layer", "0", "ROOF"),
    ("handle-less line layer", "\n8\nGround\n", "\n8\nSky\n", "LINE[0]", "layer",
     "GROUND", "SKY"),
    ("block child ByBlock colour to ByLayer", "\n62\n0\n", "\n62\n256\n", "*U1/LINE[0]",
     "color", "aci:0", "aci:256"),
    ("block child ByBlock linetype to ByLayer", "\n6\nByBlock\n", "\n6\nByLayer\n",
     "*U1/LINE[0]", "linetype", "BYBLOCK", "BYLAYER"),
    ("block child ByBlock lineweight to ByLayer", "\n370\n-2\n", "\n370\n-1\n",
     "*U1/LINE[0]", "lineweight", -2, -1),
    ("block child layer", "\n8\nFrame\n", "\n8\nFrame2\n", "*U1/LINE[0]", "layer",
     "FRAME", "FRAME2"),
]


@pytest.mark.parametrize("old,new,label,prop,was,now", [m[1:] for m in DECLARED_MUTATIONS],
                         ids=[m[0] for m in DECLARED_MUTATIONS])
def test_each_declared_style_mutation_fails_with_its_exact_message(old, new, label, prop,
                                                                    was, now):
    result = _declared(_mutate(DECLARED, old, new))
    message = f"style mismatch: {prop}"
    assert result["ok"] is False and result["error"] is None
    assert result["diagnostics"] == [
        {"entity": label, "property": prop, "before": was, "after": now, "message": message},
    ]
    assert result["style_failures"] == {message: 1}
    assert result["style_mismatches"] == 1
    assert result["style_matched"] == result["style_records"] - 1


DECLARED_REMOVALS = [
    ("keyed text", "\n0\nTEXT\n5\nA2\n8\nNotes\n420\n16711680\n62\n1\n7\nRomans\n10\n0\n20\n0\n"
     "30\n0\n40\n1\n1\nhello\n", "\n", "A2"),
    ("handle-less mtext", "\n0\nMTEXT\n8\nNotes\n10\n0\n20\n0\n30\n0\n40\n1\n1\nnote\n", "\n",
     "MTEXT[0]"),
    ("anonymous block child", "\n0\nLINE\n8\nFrame\n62\n0\n6\nByBlock\n370\n-2\n10\n0\n20\n0\n"
     "30\n0\n11\n1\n21\n0\n31\n0\n", "\n", "*U1/LINE[0]"),
]


@pytest.mark.parametrize("old,new,label", [m[1:] for m in DECLARED_REMOVALS],
                         ids=[m[0] for m in DECLARED_REMOVALS])
def test_a_missing_counterpart_fails_as_style_entity_missing(old, new, label):
    result = _declared(_mutate(DECLARED, old, new))
    assert result["ok"] is False
    assert result["diagnostics"] == [
        {"entity": label, "property": None, "before": None, "after": None,
         "message": "style entity missing"},
    ]
    assert result["style_failures"] == {"style entity missing": 1}


DECLARED_MALFORMED = [
    ("empty layer", "\n8\nWires\n", "\n8\n\n", "layer"),
    ("non-integer aci", "\n62\n1\n6\nDASHED\n", "\n62\nred\n6\nDASHED\n", "color"),
    ("aci out of range", "\n62\n1\n6\nDASHED\n", "\n62\n300\n6\nDASHED\n", "color"),
    ("non-integer true colour", "\n420\n16711680\n", "\n420\nx\n", "color"),
    ("oversized linetype", "\n6\nDASHED\n", "\n6\n" + "L" * 256 + "\n", "linetype"),
    ("lineweight outside the enumeration", "\n370\n35\n", "\n370\n17\n", "lineweight"),
    ("empty text style", "\n7\nRomans\n", "\n7\n\n", "text_style"),
]


@pytest.mark.parametrize("old,new,prop", [m[1:] for m in DECLARED_MALFORMED],
                         ids=[m[0] for m in DECLARED_MALFORMED])
def test_a_malformed_style_value_refuses_on_either_side(old, new, prop):
    malformed = _mutate(DECLARED, old, new)
    for result in (_declared(malformed), _declared(DECLARED, before=malformed)):
        assert result["ok"] is False
        assert result["error"] == f"malformed style: {prop}"
        assert result["diagnostics"] == [] and result["style_records"] is None


def test_a_duplicate_handle_refuses_on_either_side_and_across_sections():
    within = _mutate(DECLARED, "\n5\nA3\n", "\n5\nA1\n")
    across = _mutate(DECLARED, "\n5\nA3\n", "\n5\nB2\n")
    assert _declared(within)["error"] == "duplicate handle A1"
    assert _declared(DECLARED, before=within)["error"] == "duplicate handle A1"
    assert _declared(across)["error"] == "duplicate handle B2"
    assert _declared(across)["ok"] is False


DECLARED_NORMALIZATIONS = [
    ("absent layer reads 0", "\n0\nCIRCLE\n5\nA3\n", "\n0\nCIRCLE\n5\nA3\n8\n0\n"),
    ("absent text style reads Standard", "\n0\nMTEXT\n", "\n0\nMTEXT\n7\nStandard\n"),
    ("explicit ByLayer defaults", "\n0\nCIRCLE\n5\nA3\n",
     "\n0\nCIRCLE\n5\nA3\n62\n256\n6\nBYLAYER\n370\n-1\n"),
    ("layer name case", "\n8\nWires\n", "\n8\nWIRES\n"),
    ("linetype name case", "\n6\nDASHED\n", "\n6\ndashed\n"),
    ("text style name case", "\n7\nRomans\n", "\n7\nROMANS\n"),
    ("true colour wins over its aci fallback", "\n420\n16711680\n62\n1\n",
     "\n420\n16711680\n62\n2\n"),
    ("engine assigns a handle to a handle-less entity", "\n0\nMTEXT\n", "\n0\nMTEXT\n5\nF0\n"),
    ("engine assigns a handle to a handle-less block child", "\n0\nLINE\n8\nFrame\n",
     "\n0\nLINE\n5\nF1\n8\nFrame\n"),
]


@pytest.mark.parametrize("old,new", [m[1:] for m in DECLARED_NORMALIZATIONS],
                         ids=[m[0] for m in DECLARED_NORMALIZATIONS])
def test_normalized_declared_equivalents_pass(old, new):
    result = _declared(_mutate(DECLARED, old, new))
    assert result["ok"] is True, result
    assert result["style_matched"] == result["style_records"] == 10


def test_declared_style_diagnostics_are_bounded_but_counted():
    lines = "".join(_chunk((0, "LINE"), (8, "A"), (10, "0"), (20, str(i)), (30, "0"),
                           (11, "1"), (21, str(i)), (31, "0")) for i in range(25))
    before = _chunk((0, "SECTION"), (2, "ENTITIES")) + lines + _chunk((0, "ENDSEC"), (0, "EOF"))
    after = before.replace("\n8\nA\n", "\n8\nB\n")
    result = _declared(after, before=before)
    assert result["style_mismatches"] == 25
    assert result["style_failures"] == {"style mismatch: layer": 25}
    assert len(result["diagnostics"]) == fidelity.MAX_LISTED_HANDLES
    assert result["diagnostics"][-1]["entity"] == f"LINE[{fidelity.MAX_LISTED_HANDLES - 1}]"


# --- Sub-entities sit on their owner's layer (STU3-STYLE2) ------------------

def _entities(*records) -> str:
    return (_chunk((0, "SECTION"), (2, "ENTITIES")) + "".join(records)
            + _chunk((0, "ENDSEC"), (0, "EOF")))


def _vertex(x: str, layer: str | None = None) -> str:
    pairs = [(0, "VERTEX")] + ([(8, layer)] if layer is not None else [])
    return _chunk(*pairs, (10, x), (20, "0.0"), (30, "0.0"))


def _seqend(layer: str | None = None) -> str:
    return _chunk((0, "SEQEND"), *([(8, layer)] if layer is not None else []))


# The exact shape of engine/corpus/03_classic_polyline_vertex_seqend.dxf.
CLASSIC_POLYLINE = _chunk((0, "POLYLINE"), (5, "C300"), (8, "Legacy"), (70, "1"))
CLASSIC = _entities(CLASSIC_POLYLINE, _vertex("0.0"), _vertex("40.0"), _vertex("80.0"),
                    _seqend())


def test_classic_polyline_vertices_and_seqend_inherit_the_polyline_layer():
    after = _entities(CLASSIC_POLYLINE, _vertex("0.0", "LEGACY"), _vertex("40.0", "LEGACY"),
                      _vertex("80.0", "LEGACY"), _seqend("LEGACY"))
    result = _declared(after, before=CLASSIC)
    assert result == {
        "ok": True, "style_records": 5, "style_matched": 5, "style_mismatches": 0,
        "style_failures": {}, "diagnostics": [], "error": None,
    }


def test_a_vertex_on_a_different_layer_is_still_reported():
    after = _entities(CLASSIC_POLYLINE, _vertex("0.0", "LEGACY"), _vertex("40.0", "OTHER"),
                      _vertex("80.0", "LEGACY"), _seqend("LEGACY"))
    result = _declared(after, before=CLASSIC)
    assert result["ok"] is False and result["error"] is None
    assert result["diagnostics"] == [
        {"entity": "VERTEX[1]", "property": "layer", "before": "LEGACY", "after": "OTHER",
         "message": "style mismatch: layer"},
    ]
    assert result["style_failures"] == {"style mismatch: layer": 1}
    assert result["style_matched"] == 4


def test_an_insert_seqend_inherits_the_insert_layer():
    insert = _chunk((0, "INSERT"), (8, "Roof"), (66, "1"), (2, "Panel"), (10, "5"), (20, "5"),
                    (30, "0"))
    attrib = _chunk((0, "ATTRIB"), (8, "Roof"), (10, "5"), (20, "5"), (30, "0"), (40, "1"),
                    (1, "v"), (2, "TAG"), (70, "0"))
    before = _entities(insert, attrib, _seqend())
    result = _declared(_entities(insert, attrib, _seqend("ROOF")), before=before)
    assert result["ok"] is True, result
    assert result["style_matched"] == result["style_records"] == 3


def test_an_orphan_vertex_keeps_layer_zero():
    before = _entities(_chunk((0, "LINE"), (8, "Wires"), (10, "0"), (20, "0"), (30, "0"),
                              (11, "1"), (21, "0"), (31, "0")), _vertex("0.0"))
    passing = _declared(before.replace("\n0\nVERTEX\n", "\n0\nVERTEX\n8\n0\n"), before=before)
    assert passing["ok"] is True, passing
    failing = _declared(before.replace("\n0\nVERTEX\n", "\n0\nVERTEX\n8\nX\n"), before=before)
    assert failing["diagnostics"] == [
        {"entity": "VERTEX[0]", "property": "layer", "before": "0", "after": "X",
         "message": "style mismatch: layer"},
    ]


class _CountingAdapter(harness.EngineAdapter):
    """Records every round trip; optionally replaces one byte run when present."""

    def __init__(self, name: str = "counting", old: bytes = b"", new: bytes = b""):
        self.name = name
        self.old, self.new = old, new
        self.calls = []

    def round_trip(self, dxf_bytes: bytes) -> bytes:
        self.calls.append(dxf_bytes)
        if self.old and dxf_bytes.count(self.old) == 1:
            return dxf_bytes.replace(self.old, self.new)
        return dxf_bytes


def test_every_tracked_drawing_passes_the_style_proof_with_one_call_each():
    adapter = _CountingAdapter()
    receipts = fidelity.run_tracked_style_proofs(adapter)
    assert [r["drawing"] for r in receipts] == list(fidelity.TRACKED_DRAWINGS)
    assert adapter.calls == [path.read_bytes() for path in fidelity.tracked_paths()]
    for receipt in receipts:
        assert tuple(receipt) == fidelity.STYLE_PROOF_RECEIPT_KEYS
        assert receipt["ok"] is True, receipt
        assert receipt["failures"] == [] and receipt["source_untouched"] is True
        assert receipt["geometry"]["ok"] is True and receipt["styles"]["ok"] is True
        assert tuple(receipt["geometry"]) == ("ok", *fidelity.TRACKED_RECEIPT_KEYS[5:-2])
    block = receipts[fidelity.TRACKED_DRAWINGS.index("web/e2e/fixtures/block-fixture.dxf")]
    assert block["styles"]["style_records"] == 2


def test_a_style_only_mutation_fails_the_proof_and_keeps_geometry():
    rel = "web/e2e/fixtures/block-fixture.dxf"
    adapter = _CountingAdapter(old=b"ByLayer", new=b"DASHED")
    receipt = fidelity.run_drawing_style_proof(adapter, rel)
    assert len(adapter.calls) == 1
    assert receipt["ok"] is False
    assert receipt["failures"] == ["style mismatch: linetype"]
    assert receipt["geometry"]["ok"] is True
    assert receipt["styles"]["diagnostics"] == [
        {"entity": "100", "property": "linetype", "before": "BYLAYER", "after": "DASHED",
         "message": "style mismatch: linetype"},
    ]


def test_a_geometry_only_mutation_fails_the_proof_and_keeps_styles():
    rel = "vendor/acadrust-worker/fixtures/one_line.dxf"
    adapter = _CountingAdapter(old=b"100.0", new=b"100.5")
    receipt = fidelity.run_drawing_style_proof(adapter, rel)
    assert len(adapter.calls) == 1
    assert receipt["ok"] is False
    assert receipt["failures"] == ["geometry mismatch"]
    assert receipt["geometry"]["ok"] is False and receipt["styles"]["ok"] is True


def test_an_adapter_error_fails_the_proof_closed():
    receipt = fidelity.run_drawing_style_proof(_RaisingAdapter(),
                                               "web/e2e/fixtures/block-fixture.dxf")
    assert receipt["ok"] is False
    assert receipt["failures"] == ["RuntimeError: boom"]
    assert receipt["source_untouched"] is True
    assert receipt["geometry"]["ok"] is False and receipt["styles"]["ok"] is False


def test_a_mutated_source_fails_as_source_changed(tmp_path, monkeypatch):
    rel = "vendor/acadrust-worker/fixtures/one_line.dxf"
    copy = tmp_path / rel
    copy.parent.mkdir(parents=True)
    copy.write_bytes((PROJECT_ROOT / rel).read_bytes())
    monkeypatch.setattr(fidelity, "PROJECT_ROOT", tmp_path)

    class _Tamper(harness.EngineAdapter):
        name = "tamper"

        def round_trip(self, dxf_bytes: bytes) -> bytes:
            copy.write_bytes(dxf_bytes + b"999\nx\n")
            return dxf_bytes

    receipt = fidelity.run_drawing_style_proof(_Tamper(), rel)
    assert receipt["ok"] is False
    assert receipt["failures"] == ["source changed"]
    assert receipt["source_untouched"] is False


def test_a_slow_drawing_fails_as_drawing_timeout(monkeypatch):
    monkeypatch.setattr(fidelity, "TRACKED_DRAWING_TIMEOUT_MS", -1.0)
    receipt = fidelity.run_drawing_style_proof(_CountingAdapter(),
                                               "vendor/acadrust-worker/fixtures/one_line.dxf")
    assert receipt["ok"] is False
    assert receipt["failures"] == ["drawing timeout"]


FAKE_METADATA = {
    "source_commit": "0" * 40, "build": "fake build", "tools": {"node": "v0"},
    "wasm_sha256": ["f" * 64 + " engine_bg.wasm"],
}


def test_style_proof_cli_exits_zero_with_identity_adapter(capsys):
    assert fidelity.main(["--adapter=identity", "--style-proof"]) == 0
    out = capsys.readouterr().out
    assert "# STYLE PROOF RESULT: ALL DRAWINGS OK (8 drawings, adapter='identity')" in out


def test_a_failed_proof_exits_one_and_keeps_its_failure_evidence(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fidelity, "evidence_metadata", lambda: dict(FAKE_METADATA))
    evidence = tmp_path / "evidence" / "style.json"
    adapter = _CountingAdapter(name="acadrust", old=b"ByLayer", new=b"DASHED")
    args = ["--style-proof", f"--evidence={evidence}"]
    assert fidelity.main(args, adapter=adapter) == 1
    assert len(adapter.calls) == len(fidelity.TRACKED_DRAWINGS)
    assert "FAILURES ABOVE" in capsys.readouterr().out
    document = json.loads(evidence.read_bytes().decode("utf-8"))
    assert list(document) == ["source_commit", "adapter", "build", "tools", "wasm_sha256",
                              "command", "ok", "receipts"]
    assert document["ok"] is False and document["adapter"] == "acadrust"
    assert document["source_commit"] == "0" * 40
    assert [r["drawing"] for r in document["receipts"]] == list(fidelity.TRACKED_DRAWINGS)
    failed = [r for r in document["receipts"] if not r["ok"]]
    assert [r["drawing"] for r in failed] == ["web/e2e/fixtures/block-fixture.dxf"]
    assert failed[0]["failures"] == ["style mismatch: linetype"]


def test_a_passing_acadrust_proof_writes_passing_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(fidelity, "evidence_metadata", lambda: dict(FAKE_METADATA))
    evidence = tmp_path / "style.json"
    adapter = _CountingAdapter(name="acadrust")
    assert fidelity.main(["--style-proof", f"--evidence={evidence}"], adapter=adapter) == 0
    document = json.loads(evidence.read_text(encoding="utf-8"))
    assert document["ok"] is True
    assert all(r["ok"] for r in document["receipts"])


def test_evidence_is_refused_for_any_adapter_but_acadrust(tmp_path, capsys):
    evidence = tmp_path / "style.json"
    adapter = _CountingAdapter(name="identity")
    assert fidelity.main(["--style-proof", f"--evidence={evidence}"], adapter=adapter) == 2
    assert "evidence requires acadrust" in capsys.readouterr().err
    assert adapter.calls == [] and not evidence.exists()
    assert fidelity.main(["--adapter=identity", "--style-proof", f"--evidence={evidence}"]) == 2
    assert not evidence.exists()


def test_evidence_without_style_proof_is_refused(tmp_path, capsys):
    evidence = tmp_path / "style.json"
    assert fidelity.main(["--adapter=identity", f"--evidence={evidence}"]) == 2
    assert "--evidence requires --style-proof" in capsys.readouterr().err
    assert not evidence.exists()
