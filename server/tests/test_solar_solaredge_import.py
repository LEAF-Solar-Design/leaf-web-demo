"""S3 and S4 parity: the ImportSolarEdgePDF command port on the fixture and on hand-built grids.

The fixture run (data/solaredge_1to1_demo.pdf against the committed drawing intake) pins what the
plugin's licensed capture of the same import recorded: 25 of 25 panel groups matched, 24 bridge
strings, 116 strings with the capture's length distribution, no panel left unstrung. The drawn
membership is pinned by digest. Small hand-built matrices pin the helpers the plugin's quirks live
in (the 180-degree cell mapping, bridge renumbering, the Sequences cut and the trailing partial
string) and the fail-closed refusals (ambiguous and missing matches, a missing bridge handle, a
panel lattice with no single orientation).
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import pytest

import solar_solaredge_import as si
from solar_solaredge_import import SolarEdgeImportError
from solar_solaredge_parse import parse_primitives
from solar_solaredge_pdf import extract_primitives_from_file


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "data" / "solaredge_1to1_demo.pdf"
INTAKE = REPO_ROOT / "docs" / "parity" / "evidence" / "solaredge" / "se-pg-intake.json"
# The capture's sorted string lengths (the plugin's strings document, length_distribution).
LENGTHS = ([22, 24, 25] + [26] * 3 + [27] + [28] * 23 + [29] * 19 + [30] * 6 + [31] * 20
           + [32] * 25 + [33] * 4 + [34] * 6 + [35] + [36] * 3 + [37, 39])
# sha256 of the drawn strings' memberships, in draw order, as compact JSON.
MEMBERSHIP_DIGEST = "98db8fa1aa12741779e5367264db4575e1070b4b048bd85e7e23eba9c51dc309"


@pytest.fixture(scope="module")
def fixture_run():
    intake = json.loads(INTAKE.read_text(encoding="utf-8"))
    _, matrices = parse_primitives(extract_primitives_from_file(FIXTURE), FIXTURE.stem)
    angle = si.lattice_row_angle([(p["x"], p["y"]) for p in intake["panels"]])
    result = si.run_import(matrices, intake["groups"], intake["panels"], row_angle=angle,
                           alignment_tolerance=intake["settings"]["AlignmentTolerance"],
                           selection_order="recorded")
    return intake, matrices, angle, result


def test_fixture_matches_every_group_and_draws_the_captured_strings(fixture_run):
    intake, matrices, _, result = fixture_run
    assert len(intake["groups"]) == 25
    assert len(result["matches"]) == 25
    assert result["matchable_count"] == 25
    assert all(len(m["candidates"]) == 1 for m in result["matches"])
    assert len(result["bridge_strings"]) == 24
    drawn = result["strings"] + result["bridge_strings"]
    assert len(drawn) == 116
    assert sorted(len(s["panels"]) for s in drawn) == LENGTHS
    assert result["unassigned"] == []
    assert not any(s["partial"] for s in drawn)
    members = [h for s in drawn for h in s["panels"]]
    assert len(members) == len(set(members)) == len(intake["panels"]) == 3526
    digest = hashlib.sha256(json.dumps([s["panels"] for s in drawn], separators=(",", ":")).encode())
    assert digest.hexdigest() == MEMBERSHIP_DIGEST
    # Every string carries its PDF inverter and string input, each pair drawn once.
    pairs = [(s["inverter_id"], s["string_input_number"]) for s in drawn]
    assert len(set(pairs)) == 116 and min(p[0] for p in pairs) == 1
    assert len(matrices) == 14


def test_fixture_lattice_angle_and_why_the_block_rotation_alone_fails(fixture_run):
    intake, matrices, angle, _ = fixture_run
    assert math.isclose(angle, 0.0323548553, abs_tol=1e-9)
    # The intake records block rotation 0; without the definition's rectangle rotation the
    # plugin's own matrices would not match the PDF's, and the port refuses rather than guess.
    with pytest.raises(SolarEdgeImportError) as err:
        si.run_import(matrices, intake["groups"], intake["panels"], row_angle=0.0,
                      alignment_tolerance=intake["settings"]["AlignmentTolerance"],
                      selection_order="recorded")
    assert err.value.code == "no-structural-match"


def test_run_import_does_not_mutate_its_inputs(fixture_run):
    intake, matrices, angle, _ = fixture_run
    before = copy.deepcopy(matrices), copy.deepcopy(intake)
    si.run_import(matrices, intake["groups"], intake["panels"], row_angle=angle,
                  alignment_tolerance=intake["settings"]["AlignmentTolerance"],
                  selection_order="recorded")
    assert (matrices, intake) == before


# ---------------------------------------------------------------- hand-built grids

def cell(code, seq=0, inv=-1, sin=0, hex_id=""):
    return {"Code": code, "Id": hex_id, "Seq": seq, "InverterId": inv, "StringInputNumber": sin}


def plain_grid(seqs, inv=1):
    """One PDF grid, one row; seqs in JSON column order (0 = empty cell)."""
    cells = [cell(1, s, inv, 1, f"{i:04x}") if s else cell(0) for i, s in enumerate(seqs)]
    return {"Dwgname": "g", "Sequences": [sum(1 for s in seqs if s)], "Rows": [{"Panels": cells}],
            "SubGrids": [{"Id": 0, "Cells": []}], "BridgeConnections": []}


def two_block_grid():
    """A merged grid of two one-row blocks joined by one bridge string.

    Pre-rotation cells: block A (sub-grid 0) at (0, 0..2), block B (sub-grid 1) at (0, 4..6),
    column 3 empty. JSON columns are reversed (the 180-degree turn), so JSON col = 6 - col.
    Strings: #1 Seq 1-2 in A, #2 Seq 3-5 bridging A to B, #3 Seq 6 in B.
    """
    seq_at = {0: 1, 1: 2, 2: 3, 4: 4, 5: 5, 6: 6}  # pre-rotation col -> Seq
    info = {1: (1, 1), 2: (1, 1), 3: (2, 4), 4: (2, 4), 5: (2, 4), 6: (3, 2)}
    panels = []
    for json_col in range(7):
        col = 6 - json_col
        if col in seq_at:
            s = seq_at[col]
            panels.append(cell(1, s, *info[s], hex_id=f"{col:04x}"))
        else:
            panels.append(cell(0))
    return {
        "Dwgname": "merged", "Sequences": [2, 3, 1], "Rows": [{"Panels": panels}],
        "SubGrids": [{"Id": 0, "Cells": [[0, 0], [0, 1], [0, 2]]},
                     {"Id": 1, "Cells": [[0, 4], [0, 5], [0, 6]]}],
        "BridgeConnections": [{"FromSubgrid": 0, "FromRow": 0, "FromCol": 2,
                               "ToSubgrid": 1, "ToRow": 0, "ToCol": 4}],
    }


def row_panels(prefix, n, x0=0.0, y=0.0, pitch=10.0):
    return [{"handle": f"{prefix}{i:02X}", "x": x0 + i * pitch, "y": y} for i in range(n)]


def test_bridge_seqs_split_and_renumber_follow_the_plugin():
    merged = two_block_grid()
    assert si.identify_bridge_string_seqs(merged) == {3, 4, 5}
    grids, bridges = si.matchable_grids([merged])
    assert [g["sub_grid_id"] for g in grids] == [0, 1]
    # Block B occupies JSON cols 0..2 (reversed), block A JSON cols 4..6.
    b, a = grids[1]["json"], grids[0]["json"]
    assert [c["Seq"] for c in a["Rows"][0]["Panels"]] == [0, 2, 1]  # Seq 3 removed
    assert a["Sequences"] == [2]
    assert [c["Seq"] for c in b["Rows"][0]["Panels"]] == [1, 0, 0]  # Seq 6 -> 1, 4 and 5 removed
    assert b["Sequences"] == [1]
    assert bridges[0]["bridge_seqs"] == {3, 4, 5}
    assert sorted(p[0] for p in bridges[0]["bridge_panels"]) == [3, 4, 5]
    assert merged == two_block_grid()  # input untouched


def test_bridge_string_handles_come_from_the_matched_sub_grids():
    merged = two_block_grid()
    # Drawing: two groups of three panels each, one row, pitch 10, far apart.
    group_a = row_panels("A", 3)
    group_b = row_panels("B", 3, x0=1000.0)
    groups = [{"block": "GA", "panels": [p["handle"] for p in group_a]},
              {"block": "GB", "panels": [p["handle"] for p in group_b]}]
    result = si.run_import([merged], groups, group_a + group_b, alignment_tolerance=1.0,
                           selection_order="recorded")
    assert [m["sub_grid_id"] for m in result["matches"]] == [0, 1]
    # The group matrix runs rightmost first ([A02, A01, A00]) against the sub-grid's PDF Seqs
    # [0, 2, 1], so Seq 1 is A00; block B's [B02, B01, B00] against [1, 0, 0].
    assert [s["panels"] for s in result["strings"]] == [["A00", "A01"], ["B02"]]
    assert result["strings"][1]["partial"] is False
    # Seq 3, 4, 5 sit at merged JSON cols 4, 2, 1: A's first matrix cell, then B's last two.
    assert [s["panels"] for s in result["bridge_strings"]] == [["A02", "B00", "B01"]]
    bridge = result["bridge_strings"][0]
    assert (bridge["inverter_id"], bridge["string_input_number"]) == (2, 4)
    assert result["unassigned"] == []


def test_draw_cuts_by_sequences_skips_missing_entities_and_keeps_a_partial():
    merged = {"Sequences": [2, 3], "Rows": [{"Panels": [
        {"Code": 1, "Id": "a1", "Seq": 1, "InverterId": 5, "StringInputNumber": 2},
        {"Code": 1, "Id": "A2", "Seq": 2, "InverterId": 5, "StringInputNumber": 2},
        {"Code": 1, "Id": "GONE", "Seq": 3, "InverterId": 6, "StringInputNumber": 1},
        {"Code": 1, "Id": "A4", "Seq": 4, "InverterId": 7, "StringInputNumber": 3},
        {"Code": 1, "Id": "A5", "Seq": 5, "InverterId": 7, "StringInputNumber": 3},
        {"Code": 1, "Id": "A9", "Seq": 5, "InverterId": 8, "StringInputNumber": 8},  # duplicate Seq
        {"Code": 1, "Id": "A6", "Seq": 7, "InverterId": 7, "StringInputNumber": 3},
    ]}]}
    drawn = si.draw_sequences(merged, {"A1", "A2", "A4", "A5", "A6"})
    assert [(s["panels"], s["partial"]) for s in drawn] == [
        (["A1", "A2"], False), (["A4", "A5", "A6"], False)]
    # The second string's inverter comes from its first drawn panel (Seq 4), not the lost Seq 3.
    assert (drawn[1]["inverter_id"], drawn[1]["string_input_number"]) == (7, 3)
    merged["Sequences"] = [2]
    drawn = si.draw_sequences(merged, {"A1", "A2", "A4", "A5", "A6"})
    # Sequences exhausted: the last count (2) stays, and the leftover is a trailing partial.
    assert [(s["panels"], s["partial"]) for s in drawn] == [
        (["A1", "A2"], False), (["A4", "A5"], False), (["A6"], True)]


def test_ambiguous_equal_shape_match_refuses_without_the_recorded_order():
    grids = [plain_grid([1, 2, 3]), plain_grid([3, 2, 1], inv=2)]
    panels = row_panels("P", 3) + row_panels("Q", 3, x0=500.0)
    groups = [{"block": "G1", "panels": ["P00", "P01", "P02"]},
              {"block": "G2", "panels": ["Q00", "Q01", "Q02"]}]
    with pytest.raises(SolarEdgeImportError) as err:
        si.run_import(grids, groups, panels, alignment_tolerance=1.0)
    assert err.value.code == "ambiguous-structural-match"
    result = si.run_import(grids, groups, panels, alignment_tolerance=1.0,
                           selection_order="recorded")
    assert [m["grid_index"] for m in result["matches"]] == [0, 1]
    assert [m["candidates"] for m in result["matches"]] == [[0, 1], [1]]


def test_missing_match_and_missing_bridge_handle_refuse():
    panels = row_panels("P", 4)
    with pytest.raises(SolarEdgeImportError) as err:
        si.run_import([plain_grid([1, 2, 3])], [{"block": "G", "panels": [p["handle"] for p in panels]}],
                      panels, alignment_tolerance=1.0)
    assert err.value.code == "no-structural-match"
    # Only block A of the bridged grid is in the drawing: the bridge string cannot be drawn.
    group_a = row_panels("A", 3)
    with pytest.raises(SolarEdgeImportError) as err:
        si.run_import([two_block_grid()], [{"block": "GA", "panels": [p["handle"] for p in group_a]}],
                      group_a, alignment_tolerance=1.0, selection_order="recorded")
    assert err.value.code == "bridge-handle-missing"


@pytest.mark.parametrize("bad, code", [
    ([], "malformed-groups"),
    ([{"block": "G", "panels": ["NOPE"]}], "unknown-group-panel"),
])
def test_malformed_groups_refuse(bad, code):
    with pytest.raises(SolarEdgeImportError) as err:
        si.run_import([plain_grid([1])], bad, row_panels("P", 1), alignment_tolerance=1.0)
    assert err.value.code == code


def test_lattice_angle_recovers_a_rotation_and_refuses_mixed_orientations():
    theta = 0.2
    pts = []
    for r in range(4):
        for c in range(5):
            x, y = c * 30.0, r * 20.0
            pts.append((x * math.cos(theta) - y * math.sin(theta), x * math.sin(theta) + y * math.cos(theta)))
    assert math.isclose(si.lattice_row_angle(pts), theta, abs_tol=1e-12)
    mixed = pts + [(1000.0 + 30.0 * i, 0.0) for i in range(3)]
    with pytest.raises(SolarEdgeImportError) as err:
        si.lattice_row_angle(mixed)
    assert err.value.code == "lattice-angle-ambiguous"
    with pytest.raises(SolarEdgeImportError):
        si.lattice_row_angle([(0.0, 0.0)])
