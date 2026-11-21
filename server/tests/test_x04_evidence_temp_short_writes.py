"""X04 temporary output completes partial writes and rejects invalid counts."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main
import export_x04_offline_evidence as exporter


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('mode', ['one-byte', 'irregular', 'zero-first', 'zero-after',
    'none', 'negative', 'oversized', 'boolean', 'fractional'])
def test_temp_write_counts_and_valid_retry(main, mode, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes()
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    original = exporter.tempfile.NamedTemporaryFile
    class CountedTemporary:
        def __init__(self, **kwargs):
            self.stream = original(**kwargs)
            self.name = self.stream.name
            self.calls = 0
        def __enter__(self):
            return self
        def write(self, value):
            self.calls += 1
            invalid = {'none': None, 'negative': -1, 'oversized': len(value) + 1,
                       'boolean': True, 'fractional': 0.5}
            if mode in invalid:
                return invalid[mode]
            if mode == 'zero-first' or (mode == 'zero-after' and self.calls > 1):
                return 0
            count = 1 if mode == 'one-byte' else [3, 17, 1, 63][(self.calls - 1) % 4]
            return self.stream.write(value[:count])
        def flush(self):
            self.stream.flush()
        def __exit__(self, *args):
            self.stream.close()
    with monkeypatch.context() as patch:
        patch.setattr(exporter.tempfile, 'NamedTemporaryFile', CountedTemporary)
        status = main([str(source), '--out', str(target)])
    streams = capsys.readouterr()
    assert streams.out == '' and source.read_bytes() == before
    assert not list(tmp_path.glob('.x04-*'))
    if mode in ('one-byte', 'irregular'):
        assert status == 0 and streams.err == ''
        result = json.loads(target.read_bytes())
        assert result.get('result', result.get('recomputed_result'))['count'] == 7
    else:
        assert status == 2 and 'forward progress' in streams.err and 'Traceback' not in streams.err
        assert target.read_bytes() == b'previous'
    assert main([str(source), '--out', str(target)]) == 0
    streams = capsys.readouterr()
    assert streams.out == streams.err == ''
    result = json.loads(target.read_bytes())
    assert result.get('result', result.get('recomputed_result')) == {
        'count': 7, 'highlight_handles': ['A', 'B'], 'original_bytes_integrity': 'unverified'}
