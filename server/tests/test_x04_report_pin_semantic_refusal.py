"""Byte-matched reports still face internal arithmetic and total checks."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
import validate_x04_review_report as checker


@pytest.mark.parametrize('factory', [comparison, batch])
@pytest.mark.parametrize('stdin', [False, True])
def test_correctly_pinned_report_contradictions_refuse_and_recover(factory, stdin, tmp_path, monkeypatch, capsys):
    valid = factory(); forged = deepcopy(valid)
    if factory is comparison: forged['count']['delta'] += 1
    else: forged['total'] += 1
    before = deepcopy(forged); raw = json.dumps(forged).encode()
    source = tmp_path / 'report.json'; source.write_bytes(raw)
    output = tmp_path / 'checked.json'; output.write_bytes(b'previous')
    original_open = Path.open; opened = []
    def guarded(path, mode='r', *args, **kwargs):
        if mode == 'rb':
            opened.append(path); assert path == source
        return original_open(path, mode, *args, **kwargs)
    def run(data):
        if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(data)))
        else: source.write_bytes(data)
        return checker.main(['-' if stdin else str(source), '--out', str(output),
                             '--expected-report-sha256', hashlib.sha256(data).hexdigest()])
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', guarded)
        assert run(raw) == 2
    assert opened == ([] if stdin else [source])
    assert source.read_bytes() == raw and output.read_bytes() == b'previous' and forged == before
    captured = capsys.readouterr()
    assert captured.out == '' and 'Traceback' not in captured.err and 'refused' in captured.err
    assert not list(tmp_path.glob('.x04-*'))
    corrected = json.dumps(valid).encode()
    assert run(corrected) == 0 and json.loads(output.read_bytes()) == checker.validate_report(valid)
    assert json.loads(output.read_bytes())['original_bytes_integrity'] == 'unverified'
