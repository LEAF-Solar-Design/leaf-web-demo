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


def test_classic_3d_polyline_ignores_extrusion_and_keeps_vertex_z():
    raw = _dxf("0\nPOLYLINE\n5\nP4\n8\nRAFTER_45X145\n70\n8\n66\n1\n"
                "210\n0\n220\n0\n230\n-1\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n1\n20\n2\n30\n3\n"
                "0\nVERTEX\n8\nRAFTER_45X145\n10\n4\n20\n5\n30\n6\n"
                "0\nSEQEND\n")
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="t.dxf")
    poly = intake["polylines"][0]
    assert poly["pts"] == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    # The pre-existing member-evidence pass (W4g-7c-2s) still records the raw
    # extrusion informationally on every polyline kind; VERTEX coordinates
    # above are untouched, which is the contract this test pins.
    assert poly["normal"] == [0.0, 0.0, -1.0]


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
