"""X04 bounded stdin reading tolerates short reads and recovers after IO errors."""
import io
import json
import sys
from types import SimpleNamespace
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


class ShortReader:
    def __init__(self, encoded, pattern, fail_after=None):
        self.encoded = encoded
        self.pattern = pattern
        self.fail_after = fail_after
        self.position = 0
        self.calls = 0

    def read(self, size):
        assert 0 < size <= 65536
        if self.fail_after is not None and self.position >= self.fail_after:
            raise OSError('injected read failure after valid prefix')
        length = min(size, self.pattern[self.calls % len(self.pattern)])
        self.calls += 1
        value = self.encoded[self.position:self.position + length]
        self.position += len(value)
        return value


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('mode', ['one-byte', 'irregular', 'read-error'])
def test_short_reads_and_prefix_failure_recovery(main, mode, tmp_path, monkeypatch, capsys):
    encoded = json.dumps(worked_example()).encode('utf-8')
    pattern = [1] if mode == 'one-byte' else [1, 17, 2, 63, 5]
    reader = ShortReader(encoded, pattern, fail_after=64 if mode == 'read-error' else None)
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=reader))
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    status = main(['-', '--out', str(target)])
    streams = capsys.readouterr()
    assert streams.out == '' and not list(tmp_path.glob('.x04-*'))
    if mode == 'read-error':
        assert status == 2 and 'injected read failure' in streams.err
        assert target.read_bytes() == b'previous'
        monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(encoded)))
        assert main(['-', '--out', str(target)]) == 0
        streams = capsys.readouterr()
    else:
        assert status == 0 and reader.calls > 1
    assert streams.out == streams.err == ''
    output = json.loads(target.read_bytes())
    result = output.get('result', output.get('recomputed_result'))
    assert result == {'count': 7, 'highlight_handles': ['A', 'B'], 'original_bytes_integrity': 'unverified'}
