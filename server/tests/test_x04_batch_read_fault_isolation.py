"""Permission and mid-read errors isolate one occurrence without stale results."""
import json
from pathlib import Path
import pytest
from test_x04_untrusted_derived_counts import worked_example
import batch_validate_x04_evidence as batch


@pytest.mark.parametrize('fault', ['permission', 'mid-read'])
@pytest.mark.parametrize('refuse_output', [False, True])
def test_middle_read_fault_and_valid_retry(fault, refuse_output, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source'; source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes(); output = tmp_path / 'output'; output.write_bytes(b'previous')
    original_read, original_open, original_write = batch.read_json, Path.open, batch.write_json
    calls, reports = [], []
    class Faulty:
        def __init__(self): self.stream = original_open(source, 'rb'); self.reads = 0
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def read(self, size):
            self.reads += 1
            if self.reads == 2: raise OSError('injected read after valid prefix')
            return self.stream.read(size)
    def open_fault(path, *args, **kwargs):
        if path == source:
            if fault == 'permission': raise PermissionError('injected permission refusal')
            return Faulty()
        return original_open(path, *args, **kwargs)
    def read(path, limit):
        calls.append(path)
        if len(calls) == 2:
            with monkeypatch.context() as patch:
                patch.setattr(Path, 'open', open_fault)
                return original_read(path, limit)
        return original_read(path, limit)
    def write(value, *args, **kwargs):
        reports.append(value); return original_write(value, *args, **kwargs)
    args = [str(source)] * 3 + ['--out', str(output)]
    if refuse_output: args += ['--max-output-bytes', '1']
    with monkeypatch.context() as patch:
        patch.setattr(batch, 'read_json', read); patch.setattr(batch, 'write_json', write)
        assert batch.main(args) == (2 if refuse_output else 1)
    report = reports[0]
    assert (report['total'], report['passed'], report['failed']) == (3, 2, 1)
    assert [row['valid'] for row in report['results']] == [True, False, True]
    assert [row['index'] for row in report['results']] == [0, 1, 2]
    streams = capsys.readouterr(); assert streams.out == '' and 'Traceback' not in streams.err
    if refuse_output: assert output.read_bytes() == b'previous' and 'refused' in streams.err
    else: assert json.loads(output.read_bytes()) == report and streams.err == ''
    assert source.read_bytes() == before and not list(tmp_path.glob('.x04-*'))
    assert batch.main([str(source)] * 3 + ['--out', str(output)]) == 0
    assert json.loads(output.read_bytes())['passed'] == 3
