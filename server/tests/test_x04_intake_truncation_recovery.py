"""Truncated synthetic intake never establishes complete original-DWG evidence."""
import hashlib
from dxf_intake import parse_dxf_bytes, DxfParseError
from test_x04_original_bytes_replay import RAW, guard_source
from test_x04_minsert_arrays import TOOL, selected
import tool_loader


def test_group_line_truncation_has_bounded_evidence_and_no_state_leak(tmp_path, monkeypatch):
    tool_loader.run_tool_dynamic(TOOL, {}, {}, aps_live=False)
    opened = guard_source(monkeypatch, tmp_path / 'original.dxf')
    offsets = [index + 1 for index, value in enumerate(RAW) if value == 10][:-1]
    digest = hashlib.sha256(RAW).hexdigest()
    for offset in offsets:
        prefix = RAW[:offset]
        try:
            partial = parse_dxf_bytes(prefix)
        except DxfParseError:
            pass
        else:
            report = partial['dependency_report']
            assert report['scope'] == 'local_dxf_records'
            assert 'independent inventory' in report['coverage_note']
            assert report.get('complete') is not True and report.get('raw_complete') is not True
            outcome = selected(partial)
            if outcome['ok']:
                assert outcome['result']['original_bytes_integrity'] == 'unverified'
        recovered = selected(parse_dxf_bytes(RAW))
        assert recovered['ok'] and recovered['result']['count'] == 7
        assert recovered['overlay']['highlight_handles'] == ['A', 'B']
        assert hashlib.sha256(RAW).hexdigest() == digest
    assert offsets and opened == []
