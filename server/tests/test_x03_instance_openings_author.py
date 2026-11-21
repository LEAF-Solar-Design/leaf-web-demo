"""Opening marks on individual model-space instances of a repeated DXF frame."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dxf_intake
import tool_loader
import tools_fallback


ASKS = [
    "Apply the opening marks to beams inside repeated framing blocks and tell me which floor has the greatest deduction.",
    "Rank the levels by the steel length removed by cutouts in each framing-block instance.",
    "Which storey loses the most beam length after subtracting its marked openings from the repeated frames?",
]
FIRST = "B1[0,0]/A2/E1"
SECOND = "B1[0,1]/A2/E1"
ROOF = "B2/A2/E1"


def drawing(marks):
    def block(name, children):
        return [(0, "BLOCK"), (2, name), (10, 0), (20, 0), *children, (0, "ENDBLK")]

    def insert(handle, name, layer, extra):
        return [(0, "INSERT"), (5, handle), (2, name), (8, layer), (10, 2), (20, 10), *extra]

    line = [(0, "LINE"), (5, "E1"), (8, "0"), (10, 0), (20, 0), (11, 10), (21, 0)]
    definitions = block("Member", line) + block("Frame", insert("A2", "Member", "0", []))
    # First-floor members are doubled in X, rotated in world XY and repeated twice.
    # A second insertion of the same block on the roof retains its original size.
    refs = insert("B1", "Frame", "BEAM-L1", [(41, 2), (50, 90), (70, 2), (44, 12)])
    refs += insert("B2", "Frame", "BEAM-Roof", [])
    for owner, start, end in marks:
        refs += [(0, "TEXT"), (8, "Openings"), (10, 0), (20, 0), (1, f"OPENING {owner} {start} {end}")]
    records = [(0, "SECTION"), (2, "BLOCKS"), *definitions, (0, "ENDSEC"),
               (0, "SECTION"), (2, "ENTITIES"), *refs, (0, "ENDSEC"), (0, "EOF")]
    return "".join(f"{c}\n{v}\n" for c, v in records).encode()


def run(ask, marks):
    _, source, _ = tools_fallback.author_tool(ask)
    intake = dxf_intake.parse_dxf_bytes(drawing(marks))
    return tool_loader._run_source_in_sandbox(source, "instance_openings.py", intake, {})


@pytest.mark.parametrize("ask,marks", [
    (ASKS[0], [(FIRST, 2, 8), (FIRST, 5, 12), (SECOND, 1, 4), (ROOF, 0, 9)]),
    (ASKS[1], [(SECOND, 3, 6), (SECOND, 3, 6), (FIRST, 3, 6), (ROOF, 1, 9)]),
    (ASKS[2], [(FIRST, 12, 17), (ROOF, 0, 3), (ROOF, 3, 5)]),
])
def test_deduction_ranking_for_individual_frames(ask, marks):
    status, payload = run(ask, marks)
    assert status == "ok", payload
    result, _ = payload
    gross = {FIRST: 20, SECOND: 20, ROOF: 10}
    levels = {FIRST: "L1", SECOND: "L1", ROOF: "Roof"}
    deductions = {"L1": 0, "Roof": 0}
    for owner, length in gross.items():
        covered = sum(any(key == owner and lo <= i < hi for key, lo, hi in marks) for i in range(length))
        level = levels[owner]
        assert result["beams"][owner]["gross_length"] == pytest.approx(length)
        assert result["beams"][owner]["installed_length"] == pytest.approx(length - covered)
        assert result["beams"][owner]["opening_length"] == covered
        deductions[level] += covered
    assert result["deduction_by_level"] == deductions
    expected_order = sorted(deductions, key=lambda level: (-deductions[level], level))
    assert result["floor_ranking"] == expected_order
    assert result["greatest_deduction_levels"] == [level for level in expected_order if
                                                   deductions[level] == max(deductions.values())]


def test_bare_definition_handle_cannot_select_a_repeated_beam():
    status, payload = run(ASKS[0], [("E1", 2, 5)])
    assert status == "tool_error" and "ambiguous" in str(payload).lower(), payload


def test_world_stations_must_fit_the_selected_instance():
    status, payload = run(ASKS[0], [(ROOF, 2, 15)])
    assert status == "tool_error" and "within" in str(payload).lower(), payload
