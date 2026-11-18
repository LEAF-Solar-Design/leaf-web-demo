"""X04 absent or unusable stdin refuses without traceback and then recovers."""
import io
import json
import sys
from types import SimpleNamespace
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('mode', ['none', 'closed', 'text-only', 'no-buffer'])
def test_unusable_stdin_and_valid_recovery(main, mode, tmp_path, monkeypatch, capsys):
    if mode == 'none':
        stream = None
    elif mode == 'closed':
        buffer = io.BytesIO(b'closed')
        buffer.close()
        stream = SimpleNamespace(buffer=buffer)
    elif mode == 'text-only':
        stream = io.StringIO('{}')
    else:
        stream = SimpleNamespace()
    monkeypatch.setattr(sys, 'stdin', stream)
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    assert main(['-', '--out', str(target)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'open binary input buffer' in streams.err
    assert 'Traceback' not in streams.err and target.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.x04-*'))
    encoded = json.dumps(worked_example()).encode('utf-8')
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(encoded)))
    assert main(['-', '--out', str(target)]) == 0
    streams = capsys.readouterr()
    assert streams.out == streams.err == ''
    result = json.loads(target.read_bytes())
    assert result.get('result', result.get('recomputed_result')) == {
        'count': 7, 'highlight_handles': ['A', 'B'], 'original_bytes_integrity': 'unverified'}
