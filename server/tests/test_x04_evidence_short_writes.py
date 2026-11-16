"""X04 stdout writing completes partial progress or reports zero progress."""
import io
import json
import sys
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


class PartialWriter:
    def __init__(self, mode):
        self.mode = mode
        self.parts = []
        self.calls = 0
        self.flushed = False

    def write(self, value):
        self.calls += 1
        if self.mode == 'zero-first' or (self.mode == 'zero-after' and self.calls > 2):
            return 0
        count = 1 if self.mode == 'one-byte' else [3, 17, 1, 63][(self.calls - 1) % 4]
        count = min(count, len(value))
        self.parts.append(value[:count])
        return count

    def flush(self):
        self.flushed = True


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('mode', ['one-byte', 'irregular', 'zero-first', 'zero-after'])
def test_partial_stdout_completion_or_diagnostic_error(main, mode, tmp_path, monkeypatch):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes()
    preserved = tmp_path / 'existing.json'
    preserved.write_bytes(b'previous')
    writer = PartialWriter(mode)
    diagnostics = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', writer)
    monkeypatch.setattr(sys, 'stderr', diagnostics)
    status = main([str(source)])
    assert source.read_bytes() == before and preserved.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.x04-*'))
    if mode.startswith('zero'):
        assert status == 2 and not writer.flushed and sys.stdout is None
        assert 'forward progress' in diagnostics.getvalue()
        assert 'Traceback' not in diagnostics.getvalue()
    else:
        assert status == 0 and writer.flushed and diagnostics.getvalue() == ''
        assert writer.calls > 1
        output = json.loads(''.join(writer.parts))
        result = output.get('result', output.get('recomputed_result'))
        assert result == {'count': 7, 'highlight_handles': ['A', 'B'], 'original_bytes_integrity': 'unverified'}
