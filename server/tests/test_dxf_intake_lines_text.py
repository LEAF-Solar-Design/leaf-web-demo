"""LINE and TEXT/MTEXT in the local DXF intake (guest uploads), additive to the frozen §1 shape.

LINE lands as a 2-point open polyline (renders today, no contract change). TEXT/MTEXT land in
an ADDITIVE `texts` array that only appears when the drawing has any. Mirrors da/intake_parse
for the APS extractor so a DWG and its DXF twin yield the same labels.
"""
from __future__ import annotations

import os
import sys

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
