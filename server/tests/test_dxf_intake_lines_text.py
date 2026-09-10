"""LINE and TEXT/MTEXT in the local DXF intake (guest uploads), additive to the frozen §1 shape.

LINE lands as a 2-point open polyline (renders today, no contract change). TEXT/MTEXT land in
an ADDITIVE `texts` array that only appears when the drawing has any. Mirrors da/intake_parse
for the APS extractor so a DWG and its DXF twin yield the same labels.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "da"))

import dxf_intake  # noqa: E402
import intake_dxf  # noqa: E402
import intake_parse  # noqa: E402


def _dxf(entities: str) -> bytes:
    return ("0\nSECTION\n2\nTABLES\n0\nTABLE\n2\nLAYER\n0\nLAYER\n2\nRAFTER_45X145\n0\nENDTAB\n0\nENDSEC\n"
            "0\nSECTION\n2\nENTITIES\n" + entities + "0\nENDSEC\n0\nEOF\n").encode()


def test_line_becomes_two_point_open_polyline():
    raw = _dxf("0\nLINE\n5\nA1\n8\nRAFTER_45X145\n10\n0\n20\n0\n30\n0\n11\n3000\n21\n0\n31\n0\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    assert intake["polylines"] == [{"layer": "RAFTER_45X145", "closed": False,
                                    "pts": [[0.0, 0.0, 0.0], [3000.0, 0.0, 0.0]],
                                    "xdata": None, "handle": "A1"}]
    assert "texts" not in intake


def test_text_and_mtext_land_in_additive_texts_field():
    raw = _dxf(
        "0\nTEXT\n5\nB1\n8\n0\n10\n100\n20\n200\n1\nNORTH ELEVATION\n"
        # MTEXT continuation (code 3 then code 1); the parser strips each value, so a real
        # writer's 250-char chunk boundary inside a word joins cleanly, as here.
        "0\nMTEXT\n5\nB2\n8\n0\n10\n5\n20\n6\n3\n{\\fArial|b0;GROUND \\PFLO\n1\nOR PLAN}\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    assert intake["polylines"] == []
    assert intake["texts"] == [
        {"kind": "TEXT", "layer": "0", "pt": [100.0, 200.0], "text": "NORTH ELEVATION", "handle": "B1"},
        {"kind": "MTEXT", "layer": "0", "pt": [5.0, 6.0], "text": "GROUND FLOOR PLAN", "handle": "B2"},
    ]


@pytest.mark.parametrize("value,expected", [
    (r"\LEXTRA\l", "EXTRA"),
    (r"\OEXTRA\o", "EXTRA"),
    (r"\KEXTRA\k", "EXTRA"),
    (r"Hello\~World", "Hello World"),
    (r"{\fArial|b0;Hello} World", "Hello World"),
])
def test_mtext_formatting_keeps_visible_text(value, expected):
    raw = _dxf("0\nMTEXT\n5\nB2\n8\n0\n10\n5\n20\n6\n1\n" + value + "\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    assert intake["texts"] == [
        {"kind": "MTEXT", "layer": "0", "pt": [5.0, 6.0], "text": expected, "handle": "B2"},
    ]


def test_text_value_is_capped():
    raw = _dxf("0\nTEXT\n5\nC1\n8\n0\n10\n0\n20\n0\n1\n" + ("X" * 5000) + "\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    assert len(intake["texts"][0]["text"]) == 512


def test_aps_families_text_parses_ln_and_tx_identically():
    families = (
        "LAYER|RAFTER_45X145\n"
        "LN|RAFTER_45X145|0.000,0.000,0.000|3000.000,0.000,0.000|A1\n"
        "TX|TEXT|0|100.000,200.000|B1|NORTH ELEVATION\n"
        "TX|MTEXT|0|5.000,6.000|B2|{\\fArial|b0;GROUND \\PFLOOR} PLAN\n"
        "GEO|none\n")
    intake = intake_parse.parse_text(families, "t.dwg")
    assert intake["polylines"] == [{"layer": "RAFTER_45X145", "closed": False,
                                    "pts": [[0.0, 0.0, 0.0], [3000.0, 0.0, 0.0]],
                                    "xdata": None, "handle": "A1"}]
    assert [t["text"] for t in intake["texts"]] == ["NORTH ELEVATION", "GROUND FLOOR PLAN"]
    assert intake["texts"][1]["pt"] == [5.0, 6.0]


# w4g-lwpolyline-wcs: an LWPOLYLINE's 10/20/38 are OCS relative to its own
# extrusion normal (210/220/230); a top-level row now stores WCS `pts` with
# an informational `normal` (server/intake_dxf.py applies the exact inverse).
# A +Z polyline (no 210 groups at all) is untouched, byte-identical.
def test_lwpolyline_with_no_extrusion_normal_stores_pts_as_is():
    raw = _dxf("0\nLWPOLYLINE\n5\nP1\n8\nRAFTER_45X145\n90\n2\n70\n0\n38\n3\n"
                "10\n0\n20\n0\n10\n2\n20\n0\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    assert intake["polylines"] == [{"layer": "RAFTER_45X145", "closed": False,
                                    "pts": [[0.0, 0.0, 3.0], [2.0, 0.0, 3.0]],
                                    "xdata": None, "handle": "P1"}]


def test_lwpolyline_reflected_normal_lifts_ocs_points_to_wcs_and_flips_bulge():
    raw = _dxf("0\nLWPOLYLINE\n5\nP2\n8\nRAFTER_45X145\n90\n2\n70\n0\n38\n3\n"
                "210\n0\n220\n0\n230\n-1\n"
                "10\n0\n20\n0\n42\n1\n10\n2\n20\n0\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    poly = intake["polylines"][0]
    assert poly["pts"] == [[0.0, 0.0, -3.0], [-2.0, 0.0, -3.0]]
    assert poly["normal"] == [0.0, 0.0, -1.0]
    assert poly["bulges"] == [-1.0, 0.0]


def test_lwpolyline_tilted_normal_lifts_ocs_points_to_wcs():
    raw = _dxf("0\nLWPOLYLINE\n5\nP3\n8\nRAFTER_45X145\n90\n3\n70\n0\n38\n3\n"
                "210\n0\n220\n0.6\n230\n0.8\n"
                "10\n0\n20\n0\n42\n1\n10\n2\n20\n0\n10\n2\n20\n2\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    poly = intake["polylines"][0]
    expected = [[0.0, 1.8, 2.4], [-2.0, 1.8, 2.4], [-2.0, 0.2, 3.6]]
    for got, want in zip(poly["pts"], expected):
        assert got == pytest.approx(want, abs=1e-9)
    assert poly["normal"] == [0.0, 0.6, 0.8]
    # A tilted (positive z) normal does not flip bulge sign.
    assert poly["bulges"] == [1.0, 0.0, 0.0]


# w4g-lwpolyline-wcs-c finding 2: a normal within the writer's own 1e-6
# tolerance of +Z must be the identity on the reader too, or the reader
# transforms points the writer will never invert back (no `normal` stored),
# and the resulting per-vertex noise can make an arc's elevation disagree
# across vertices, dropping its bulge on the next write.
def test_lwpolyline_near_plus_z_normal_is_identity_and_keeps_bulge():
    raw = _dxf("0\nLWPOLYLINE\n5\nP9\n8\nRAFTER_45X145\n90\n2\n70\n0\n38\n3\n"
                "210\n0.000001\n220\n0\n230\n0.9999999999995\n"
                "10\n0\n20\n0\n42\n1\n10\n2\n20\n0\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    poly = intake["polylines"][0]
    assert poly["pts"] == [[0.0, 0.0, 3.0], [2.0, 0.0, 3.0]]
    assert "normal" not in poly
    assert poly["bulges"] == [1.0, 0.0]
    data = intake_dxf.intake_to_dxf(intake)
    back = dxf_intake.parse_dxf_bytes(data, source_name="t.dxf")
    assert back["polylines"][0]["bulges"] == [1.0, 0.0]


# w4g-classic-polyline-ocs row d: flag 70 bit 8 (3D polyline) means the
# vertices are already WCS. 210 is read only to decide the bit: never
# transformed, never emitted as `normal`, whatever it says. Getting this
# wrong would corrupt every 3D polyline in every drawing.
def test_classic_3d_polyline_ignores_extrusion_and_carries_no_normal():
    raw = _dxf("0\nPOLYLINE\n5\nP4\n8\nRAFTER_45X145\n70\n8\n66\n1\n"
                "210\n0\n220\n0\n230\n-1\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n1\n20\n2\n30\n3\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n4\n20\n5\n30\n6\n"
                "0\nSEQEND\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    poly = intake["polylines"][0]
    assert poly["pts"] == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    assert "normal" not in poly


# w4g-classic-polyline-ocs row e: a 3D polyline with no 210 group at all
# behaves exactly as today (default normal is +z, so nothing changes).
def test_classic_3d_polyline_with_no_extrusion_normal_keeps_vertex_z():
    raw = _dxf("0\nPOLYLINE\n5\nP11\n8\nRAFTER_45X145\n70\n8\n66\n1\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n1\n20\n2\n30\n3\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n4\n20\n5\n30\n6\n"
                "0\nSEQEND\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    poly = intake["polylines"][0]
    assert poly["pts"] == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    assert "normal" not in poly


# w4g-classic-polyline-ocs row a: a 2D classic polyline with no extrusion
# normal at all is byte-identical to today: no `normal` on the row, vertices
# unchanged.
def test_classic_2d_polyline_with_no_extrusion_normal_stores_pts_as_is():
    raw = _dxf("0\nPOLYLINE\n5\nP12\n8\nRAFTER_45X145\n70\n0\n66\n1\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n0\n20\n0\n30\n3\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n2\n20\n0\n30\n3\n"
                "0\nSEQEND\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    poly = intake["polylines"][0]
    assert poly["pts"] == [[0.0, 0.0, 3.0], [2.0, 0.0, 3.0]]
    assert "normal" not in poly


# w4g-classic-polyline-ocs row b: a tilted 2D classic polyline transforms its
# OCS vertices to WCS through the same `_ocs_to_wcs` LWPOLYLINE uses, and
# carries the raw normal informationally.
def test_classic_2d_polyline_tilted_normal_lifts_ocs_points_to_wcs():
    raw = _dxf("0\nPOLYLINE\n5\nP13\n8\nRAFTER_45X145\n70\n0\n66\n1\n"
                "210\n0\n220\n0.6\n230\n0.8\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n0\n20\n0\n30\n3\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n2\n20\n0\n30\n3\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n2\n20\n2\n30\n3\n"
                "0\nSEQEND\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    poly = intake["polylines"][0]
    expected = [[0.0, 1.8, 2.4], [-2.0, 1.8, 2.4], [-2.0, 0.2, 3.6]]
    for got, want in zip(poly["pts"], expected):
        assert got == pytest.approx(want, abs=1e-9)
    assert poly["normal"] == [0.0, 0.6, 0.8]
    data = intake_dxf.intake_to_dxf(intake)
    back = dxf_intake.parse_dxf_bytes(data, source_name="t.dxf")
    for got, want in zip(back["polylines"][0]["pts"], expected):
        assert got == pytest.approx(want, abs=1e-9)


# w4g-classic-polyline-ocs row c: a reflected 2D classic polyline mirrors x
# and negates elevation, and its stored bulges flip sign, matching A's
# LWPOLYLINE convention.
def test_classic_2d_polyline_reflected_normal_lifts_ocs_points_to_wcs_and_flips_bulge():
    raw = _dxf("0\nPOLYLINE\n5\nP14\n8\nRAFTER_45X145\n70\n1\n66\n1\n"
                "210\n0\n220\n0\n230\n-1\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n0\n20\n0\n30\n3\n42\n1\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n2\n20\n0\n30\n3\n42\n0\n"
                "0\nSEQEND\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    poly = intake["polylines"][0]
    assert poly["pts"] == [[0.0, 0.0, -3.0], [-2.0, 0.0, -3.0]]
    assert poly["normal"] == [0.0, 0.0, -1.0]
    assert poly["bulges"] == [-1.0, 0.0]
    assert poly["closed"] is True
    data = intake_dxf.intake_to_dxf(intake)
    back = dxf_intake.parse_dxf_bytes(data, source_name="t.dxf")
    assert back["polylines"][0]["pts"] == poly["pts"]
    assert back["polylines"][0]["bulges"] == poly["bulges"]
    assert back["polylines"][0]["closed"] is True


# w4g-classic-polyline-ocs row f: refusals match A. A zero or non-finite 210
# on a 2D polyline is refused naming the handle; a 3D polyline is never
# refused for its 210, because it never reads it.
def test_classic_2d_polyline_zero_normal_is_refused():
    raw = _dxf("0\nPOLYLINE\n5\nP15\n8\nRAFTER_45X145\n70\n0\n66\n1\n"
                "210\n0\n220\n0\n230\n0\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n0\n20\n0\n30\n3\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n2\n20\n0\n30\n3\n"
                "0\nSEQEND\n")
    with pytest.raises(dxf_intake.DxfParseError, match="zero vector"):
        dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")


def test_classic_2d_polyline_non_finite_normal_is_refused():
    raw = _dxf("0\nPOLYLINE\n5\nP16\n8\nRAFTER_45X145\n70\n0\n66\n1\n"
                "210\n0\n220\n0\n230\nnan\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n0\n20\n0\n30\n3\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n2\n20\n0\n30\n3\n"
                "0\nSEQEND\n")
    with pytest.raises(dxf_intake.DxfParseError, match="finite"):
        dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")


def test_classic_3d_polyline_zero_normal_is_never_refused():
    raw = _dxf("0\nPOLYLINE\n5\nP17\n8\nRAFTER_45X145\n70\n8\n66\n1\n"
                "210\n0\n220\n0\n230\n0\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n1\n20\n2\n30\n3\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n4\n20\n5\n30\n6\n"
                "0\nSEQEND\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    poly = intake["polylines"][0]
    assert poly["pts"] == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    assert "normal" not in poly


# w4g-lwpolyline-wcs-c finding 1: Astra's own counterexample. A classic 3D
# POLYLINE's vertices are genuinely WCS, not OCS relative to any normal, so
# a naive inverse that shares one elevation across every vertex discarded
# 3 units of the second vertex's z. w4g-classic-polyline-ocs: a 3D polyline
# (flag 70 bit 8) never reads 210 at all on parse, so it carries no `normal`
# to invert in the first place; the round trip must return both vertices
# unchanged regardless.
def test_classic_3d_polyline_round_trips_without_losing_z():
    raw = _dxf("0\nPOLYLINE\n5\nP7\n8\nRAFTER_45X145\n70\n8\n66\n1\n"
                "210\n0\n220\n0\n230\n-1\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n1\n20\n2\n30\n3\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n4\n20\n5\n30\n6\n"
                "0\nSEQEND\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    data = intake_dxf.intake_to_dxf(intake)
    back = dxf_intake.parse_dxf_bytes(data, source_name="t.dxf")
    assert back["polylines"][0]["pts"] == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]


# w4g-lwpolyline-wcs-c finding 1: Astra's LINE counterexample. Autodesk
# explicitly permits a LINE extrusion direction that differs from world Z
# while its endpoints stay in WCS, so the same disagreement rule applies.
def test_line_round_trips_without_losing_wcs_endpoints():
    raw = _dxf("0\nLINE\n5\nP8\n8\nRAFTER_45X145\n10\n0\n20\n0\n30\n0\n"
                "11\n1\n21\n2\n31\n3\n210\n0\n220\n1\n230\n0\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    data = intake_dxf.intake_to_dxf(intake)
    back = dxf_intake.parse_dxf_bytes(data, source_name="t.dxf")
    assert back["polylines"][0]["pts"] == [[0.0, 0.0, 0.0], [1.0, 2.0, 3.0]]


def test_lwpolyline_zero_normal_is_refused():
    raw = _dxf("0\nLWPOLYLINE\n5\nP5\n8\nRAFTER_45X145\n90\n2\n70\n0\n38\n3\n"
                "210\n0\n220\n0\n230\n0\n10\n0\n20\n0\n10\n2\n20\n0\n")
    with pytest.raises(dxf_intake.DxfParseError, match="zero vector"):
        dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")


def test_lwpolyline_non_finite_normal_is_refused():
    raw = _dxf("0\nLWPOLYLINE\n5\nP6\n8\nRAFTER_45X145\n90\n2\n70\n0\n38\n3\n"
                "210\n0\n220\n0\n230\nnan\n10\n0\n20\n0\n10\n2\n20\n0\n")
    with pytest.raises(dxf_intake.DxfParseError, match="finite"):
        dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
