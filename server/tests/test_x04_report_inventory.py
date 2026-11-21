"""Shared synthetic packet CLI inputs for report inventory tests."""
import io
import json
from types import SimpleNamespace
import validate_x04_evidence_packet as checker

REPORT_OPTIONS = [
    ('comparisons', '--expected-comparison-id'),
    ('batches', '--expected-batch-id'),
]


def run_packet(value, stdin, options, tmp_path, monkeypatch):
    raw = json.dumps(value).encode()
    source = tmp_path / 'packet.json'
    source.write_bytes(raw)
    output = tmp_path / 'summary.json'
    if stdin:
        monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
    code = checker.main(['-' if stdin else str(source), '--out', str(output), *options])
    assert source.read_bytes() == raw
    assert not list(tmp_path.glob('.x04-*'))
    return code, output
