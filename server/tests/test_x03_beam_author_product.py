"""Per-layer beam takeoff through the ordinary offline author and script executor."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dxf_intake
import tool_loader
import tools_fallback


CASES = [
    ("Sum beam lengths by layer in metres.", 6, 1, 1),
    ("Give me the total length of beams on each layer in meters.", 4, 10, 0.001),
    ("Report the combined length per layer for the beam centerlines, in metres.", 2, 2, 0.3048),
]


def dxf_pairs(*fields):
    return "".join(f"{code}\n{value}\n" for code, value in fields)


def beam_drawing(unit, scale):
    def line(layer, end, paper=False):
        return dxf_pairs((0, "LINE"), (8, layer), (67, int(paper)),
                         (10, 0), (20, 0), (30, 0),
                         (11, end[0]*scale), (21, end[1]*scale), (31, end[2]*scale))

    def path(layer, closed, vertices):
        return (dxf_pairs((0, "LWPOLYLINE"), (8, layer), (90, len(vertices)), (70, int(closed)))
                + "".join(dxf_pairs((10, x*scale), (20, y*scale)) for x, y in vertices))

    # First beam layer has a sloped 13-unit beam and a 7-unit bent beam.
    # Second layer has an 8-unit beam. Other lines, paper annotations, and
    # closed outlines on the beam layer must contribute nothing.
    return (dxf_pairs((0, "SECTION"), (2, "HEADER"), (9, "$INSUNITS"), (70, unit),
                      (0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES"))
            + line("S-BEAM-L1", (3, 4, 12))
            + path("S-BEAM-L1", False, [(0, 0), (3, 0), (3, 4)])
            + line("Roof_Beams", (0, 0, 8))
            + line("WALL", (99, 0, 0)) + line("Sunbeams", (30, 0, 0))
            + line("Roof_Beams", (88, 0, 0), paper=True)
            + path("S-BEAM-L1", True, [(0, 0), (20, 0), (20, 20)])
            + dxf_pairs((0, "ENDSEC"), (0, "EOF"))).encode("ascii")


def invoke(ask, unit, scale):
    _, source, _ = tools_fallback.author_tool(ask)
    intake = dxf_intake.parse_dxf_bytes(beam_drawing(unit, scale))
    return tool_loader._run_source_in_sandbox(source, "beam_takeoff.py", intake, {})


@pytest.mark.parametrize("ask,unit,scale,to_metres", CASES)
def test_beam_lengths_by_layer(ask, unit, scale, to_metres):
    status, payload = invoke(ask, unit, scale)
    assert status == "ok", payload
    result, _ = payload
    assert result.get("lengths_by_layer") == pytest.approx(
        {"S-BEAM-L1": 20*scale*to_metres, "Roof_Beams": 8*scale*to_metres}
    ), result
    assert result["units"] == "m"


def test_unitless_beams_request_units_before_reporting_metres():
    status, payload = invoke(CASES[0][0], 0, 1)
    assert status == "tool_error", payload
    assert "unit" in str(payload).lower()


@pytest.mark.parametrize("ask, layer, units, length, member_count", [
    ('Sum beam lengths on layer "roof_beams" only.', "Roof_Beams", "drawing_units", 8, 0),
    ('List beam lengths by layer, restricted to "S-BEAM-L1", in feet.',
     "S-BEAM-L1", "ft", 20 / 0.3048, 2),
    ('List each beam and sum beam lengths on "WALL" only in millimeters.',
     "WALL", "mm", 99000, 1),
    ('Sum beam lengths on layer "ft spline limits" only.', None, "drawing_units", 0, 0),
])
def test_beam_lengths_on_explicit_layer(ask, layer, units, length, member_count):
    status, payload = invoke(ask, 6, 1)
    assert status == "ok", payload
    result, _ = payload
    totals = {layer: length} if layer else {}
    assert result["lengths_by_layer"] == pytest.approx(totals)
    assert result["units"] == units
    members = result.get("lengths_by_handle", {})
    assert len(members) == member_count
    assert all(row["layer"] == layer for row in members.values())


def test_original_beam_list_in_drawing_units():
    import intake_dxf

    paths = [
        dict(handle="A1", layer="Beams East", pts=[[0, 0, 0], [3, 4, 0]], closed=False),
        dict(handle="A2", layer="Beams West", pts=[[0, 0, 0], [0, 12, 0]], closed=False),
    ]
    raw = intake_dxf.intake_to_dxf(dict(layers=["Beams East", "Beams West"], polylines=paths))
    _, source, _ = tools_fallback.author_tool("Make a tool that lists beam lengths by layer.")
    intake = dxf_intake.parse_dxf_bytes(raw)
    status, payload = tool_loader._run_source_in_sandbox(source, "beam_list.py", intake, {})
    assert status == "ok", payload
    result, _ = payload
    assert result.get("lengths_by_handle") == {
        "A1": {"layer": "Beams East", "drawing_units": 5},
        "A2": {"layer": "Beams West", "drawing_units": 12},
    }, result
    assert result["units"] == "drawing_units"


def test_feet_requested_from_a_metric_drawing():
    status, payload = invoke("Add up the beam lengths by layer in feet.", 6, 1)
    assert status == "ok", payload
    result, _ = payload
    assert result["units"] == "ft"
    assert result["lengths_by_layer"]["Roof_Beams"] == pytest.approx(8 / 0.3048)


@pytest.mark.parametrize("ask", [
    "Check beam totals where an annotation block mixes text and independently identified framing.",
    "Show the combined length of the beams in the detail block.",
    "Measure the total length of each beam layer in this mixed annotation and framing block.",
])
def test_takeoff_in_mixed_block(ask):
    raw = dxf_pairs(
        (0, "SECTION"), (2, "BLOCKS"), (0, "BLOCK"), (2, "Detail"),
        (0, "TEXT"), (8, "Notes"), (10, 0), (20, 0), (40, 1), (1, "BEAM 999"),
        (0, "LINE"), (5, "D1"), (8, "S-Beams"), (10, 0), (20, 0), (11, 3), (21, 4),
        (0, "LINE"), (5, "D2"), (8, "Notes"), (10, 0), (20, 0), (11, 99), (21, 0),
        (0, "ENDBLK"), (0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES"),
        (0, "INSERT"), (5, "B1"), (2, "Detail"), (8, "Notes"), (10, 10), (20, 20),
        (41, 2), (42, 2), (50, 90), (0, "ENDSEC"), (0, "EOF"),
    ).encode("ascii")
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = tool_loader._run_source_in_sandbox(
        source, "framing.py", dxf_intake.parse_dxf_bytes(raw), {})
    assert status == "ok", payload
    assert payload[0]["lengths_by_layer"] == pytest.approx({"S-Beams": 10})


@pytest.mark.parametrize("ask", [
    "Check beam totals by count for the framing.",
    "Show beam totals by area for the framing.",
])
def test_non_length_totals_keep_their_quantity(ask):
    from authored_beam_lengths import matches

    assert not matches(ask)
