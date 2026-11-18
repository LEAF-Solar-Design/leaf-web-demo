"""Report, manifest-source and packet pins guard separate synthetic byte buffers."""
import hashlib
import json
from test_x04_assembly_source_pins import pinned_files
import assemble_x04_evidence_packet as assembler
import validate_x04_evidence_packet as packet_checker
import validate_x04_review_report as report_checker


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def test_separately_pinned_reports_through_strict_assembly_and_packet(tmp_path, capsys):
    source, manifest, expected = pinned_files(tmp_path)
    source.write_bytes(json.dumps(manifest).encode())
    output = tmp_path / 'packet.json'; summary = tmp_path / 'summary.json'
    reports = [(row, tmp_path / row['path']) for section in ('comparisons', 'batches') for row in manifest[section]]
    pins = {path: digest(path) for _, path in reports}
    checks = {path: tmp_path / (path.stem + '-checked.json') for _, path in reports}
    def check_report(path, pin):
        return report_checker.main([str(path), '--expected-report-sha256', pin, '--out', str(checks[path])])
    def assemble(pin):
        return assembler.main([str(source), '--require-source-pins', '--expected-manifest-sha256', pin, '--out', str(output)])
    def check_packet(pin):
        return packet_checker.main([str(output), '--expected-packet-sha256', pin, '--out', str(summary)])
    for _, path in reports: assert check_report(path, pins[path]) == 0
    manifest_pin = digest(source)
    assert assemble(manifest_pin) == 0 and json.loads(output.read_bytes()) == expected
    packet_bytes = output.read_bytes(); packet_pin = digest(output)
    assert check_packet(packet_pin) == 0
    summary_bytes = summary.read_bytes()
    for row, target in reports:
        untouched = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file() and path not in (target, source)}
        prior_report_summary = checks[target].read_bytes()
        old_raw = target.read_bytes(); target.write_bytes(old_raw + b' ')
        changed = target.read_bytes()
        assert json.loads(changed) == json.loads(old_raw)
        assert check_report(target, pins[target]) == 2
        assert checks[target].read_bytes() == prior_report_summary
        assert all(path.read_bytes() == raw for path, raw in untouched.items())
        assert assemble(manifest_pin) == 2 and output.read_bytes() == packet_bytes
        # Refreshing the standalone report digest does not refresh the manifest row.
        pins[target] = digest(target)
        assert check_report(target, pins[target]) == 0
        assert checks[target].read_bytes() == prior_report_summary
        assert assemble(manifest_pin) == 2 and output.read_bytes() == packet_bytes
        row['expected_sha256'] = pins[target]
        source.write_bytes(json.dumps(manifest).encode())
        assert assemble(manifest_pin) == 2 and output.read_bytes() == packet_bytes
        manifest_pin = digest(source)
        assert assemble(manifest_pin) == 0 and output.read_bytes() == packet_bytes
        # Whitespace is absent from the decoded packet, so its pin remains valid.
        assert digest(output) == packet_pin and check_packet(packet_pin) == 0
        assert summary.read_bytes() == summary_bytes and target.read_bytes() == changed
    result = json.loads(summary.read_bytes())
    assert [r['recomputed_result']['count'] for r in result['validated_artifacts']] == [6, 1]
    assert [r['recomputed_result']['highlight_handles'] for r in result['validated_artifacts']] == [['A'], ['B']]
    assert result['comparisons'][0]['report']['count'] == {'before': 6, 'after': 1, 'delta': -5}
    assert result['original_bytes_integrity'] == 'unverified'
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
