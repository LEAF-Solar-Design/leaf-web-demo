"""An explicit offline routing sequence stops before assembly on wrong report kind."""
import hashlib
import json
import pytest
from test_x04_assembly_source_pins import pinned_files
import assemble_x04_evidence_packet as assembler
import validate_x04_evidence_packet as packet_checker
import validate_x04_review_report as report_checker


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('wrong_section', ['comparisons', 'batches'])
def test_explicit_kind_routing_blocks_downstream_and_recovers(wrong_section, tmp_path, monkeypatch, capsys):
    manifest_path, manifest, expected = pinned_files(tmp_path)
    manifest_path.write_bytes(json.dumps(manifest).encode()); manifest_pin = digest(manifest_path)
    output = tmp_path / 'packet.json'; summary = tmp_path / 'packet-summary.json'
    reports = [(section, tmp_path / row['path']) for section in ('comparisons', 'batches') for row in manifest[section]]
    sources = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    def route(wrong=None):
        for section, path in reports:
            kind = 'comparison' if section == 'comparisons' else 'batch'
            selected = ('batch' if kind == 'comparison' else 'comparison') if section == wrong else kind
            code = report_checker.main([str(path), '--out', str(tmp_path / (path.stem + '-checked.json')),
                '--expected-report-sha256', digest(path), '--expected-report-schema', selected])
            if code: return code
        code = assembler.main([str(manifest_path), '--require-source-pins', '--expected-manifest-sha256', manifest_pin, '--out', str(output)])
        if code: return code
        return packet_checker.main([str(output), '--out', str(summary), '--expected-packet-sha256', digest(output)])
    assert route() == 0 and json.loads(output.read_bytes()) == expected
    snapshot = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}; calls = []
    def forbidden(*args, **kwargs): calls.append(args); raise AssertionError('Wrong report kind must stop downstream')
    with monkeypatch.context() as patch:
        patch.setattr(assembler, 'main', forbidden); patch.setattr(packet_checker, 'main', forbidden)
        assert route(wrong_section) == 2
    assert calls == [] and all(p.read_bytes() == raw for p, raw in snapshot.items())
    assert route() == 0 and all(p.read_bytes() == raw for p, raw in snapshot.items())
    result = json.loads(summary.read_bytes())
    assert [r['recomputed_result']['count'] for r in result['validated_artifacts']] == [6, 1]
    assert [r['recomputed_result']['highlight_handles'] for r in result['validated_artifacts']] == [['A'], ['B']]
    assert result['comparisons'][0]['report']['count'] == {'before': 6, 'after': 1, 'delta': -5}
    assert result['original_bytes_integrity'] == 'unverified' and all(p.read_bytes() == raw for p, raw in sources.items())
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
