"""X04 actual bounded reads handle files changing after metadata inspection."""
from contextlib import contextmanager
import json
from pathlib import Path
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('race', ['grow-over-budget', 'grow-valid', 'shrink-valid', 'shrink-truncated'])
def test_stat_read_size_race_and_recovery(main, race, tmp_path, monkeypatch, capsys):
    valid = json.dumps(worked_example()).encode('utf-8')
    if race == 'grow-over-budget':
        initial, actual, limit = valid, valid + b' ' * 1000, len(valid)
    elif race == 'grow-valid':
        initial, actual, limit = b'{}', valid, len(valid)
    elif race == 'shrink-valid':
        initial, actual, limit = valid + b' ' * 1000, valid, len(valid) + 1000
    else:
        initial, actual, limit = valid, valid[:len(valid) // 2], len(valid)
    source = tmp_path / 'source.json'
    source.write_bytes(initial)
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    original_open = Path.open
    changed = []
    @contextmanager
    def race_open(path, *args, **kwargs):
        if path == source and args == ('rb',) and not changed:
            with original_open(source, 'wb') as stream:
                stream.write(actual)
            changed.append(True)
        with original_open(path, *args, **kwargs) as stream:
            yield stream
    monkeypatch.setattr(Path, 'open', race_open)
    args = [str(source), '--max-input-bytes', str(limit), '--out', str(target)]
    status = main(args)
    streams = capsys.readouterr()
    assert changed == [True] and source.read_bytes() == actual
    assert streams.out == '' and not list(tmp_path.glob('.x04-*'))
    if race in ('grow-over-budget', 'shrink-truncated'):
        assert status == 2 and 'refused' in streams.err
        assert target.read_bytes() == b'previous'
        source.write_bytes(valid)
        assert main(args) == 0
        streams = capsys.readouterr()
    else:
        assert status == 0
    assert streams.out == streams.err == ''
    output = json.loads(target.read_bytes())
    result = output.get('result', output.get('recomputed_result'))
    assert result['count'] == 7 and result['highlight_handles'] == ['A', 'B']
