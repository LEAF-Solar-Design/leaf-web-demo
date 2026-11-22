"""X04 diagnostics escape terminal controls and bound untrusted message text."""
import json
from pathlib import Path
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('category', ['duplicate-key', 'path-error'])
@pytest.mark.parametrize('name', ['carriage\rreturn', 'line\nfeed', '\x1b[31mred', 'Z' * 10000],
                         ids=['cr', 'lf', 'ansi', 'long'])
def test_untrusted_diagnostic_is_bounded_single_ascii_line(main, category, name, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source.json'
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    if category == 'duplicate-key':
        key = json.dumps(name)
        source.write_text('{' + key + ':0,' + key + ':1}', encoding='utf-8')
    else:
        source.write_text(json.dumps(worked_example()), encoding='utf-8')
        original = Path.stat
        def failed_stat(path, *args, **kwargs):
            if path == source:
                raise OSError('injected path error ' + name)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'stat', failed_stat)
    before = source.read_bytes()
    assert main([str(source), '--out', str(target)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'refused' in streams.err
    assert len(streams.err.encode('ascii')) <= 2048
    assert streams.err.count('\n') == 1 and streams.err.endswith('\n')
    assert '\r' not in streams.err and '\x1b' not in streams.err
    assert all(32 <= ord(character) <= 126 for character in streams.err[:-1])
    assert target.read_bytes() == b'previous' and source.read_bytes() == before
    assert not list(tmp_path.glob('.x04-*'))
