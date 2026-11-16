"""Unrelated decoded geometry and tables cannot change X04 candidate meaning."""
from dxf_intake import parse_dxf_bytes
from test_x04_original_bytes_replay import RAW, guard_source
from test_x04_minsert_arrays import TOOL, selected
import tool_loader


def test_comments_unused_tables_and_lines_leave_count_and_report_unchanged(tmp_path, monkeypatch):
    tool_loader.run_tool_dynamic(TOOL, {}, {}, aps_live=False)
    attempts = guard_source(monkeypatch, tmp_path / "original.dxf")
    extra = ("0\nSECTION\n2\nTABLES\n0\nLAYER\n2\nUnused\n70\n0\n0\nENDSEC\n"
             "0\nSECTION\n2\nENTITIES\n999\nSynthetic comment\n0\nLINE\n5\nF\n8\nNoise\n"
             "10\n1\n20\n1\n11\n3\n21\n3\n0\nENDSEC\n0\nEOF\n").encode()
    decorated = RAW.replace(b"0\nEOF\n", extra)
    baseline = parse_dxf_bytes(RAW)
    changed = parse_dxf_bytes(decorated)
    assert changed["dependency_report"] == baseline["dependency_report"]
    before, after = selected(baseline), selected(changed)
    assert before["ok"] and after["ok"]
    assert before["result"] == after["result"] and before["overlay"] == after["overlay"]
    assert attempts == []
