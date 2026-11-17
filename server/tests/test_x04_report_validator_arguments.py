"""X04 saved-report usage exits occur before any input/output operation."""
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import validate_x04_review_report as validator


@pytest.mark.parametrize('extra,expected', [
    (['--help'], 0), ([], 2), (['--unknown'], 2),
    (['input.json', '--unknown'], 2), (['input.json', '--max', '5'], 2),
    (['input.json', '--max-input', '5'], 2), (['--out', 'out.json'], 2),
    (['input.json', '--out'], 2), (['input.json', '--max-input-bytes'], 2),
    *[(['input.json', option, value], 2)
      for option in ('--max-input-bytes', '--max-output-bytes')
      for value in ('0', '-1', 'bad', '1.5')],
])
def test_usage_has_no_io(extra, expected, tmp_path, monkeypatch, capsys):
    output = tmp_path / 'never-created.json'
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError('Usage must precede IO')
    for name in ('read_json', 'write_json', 'check_output_path'):
        monkeypatch.setattr(validator, name, forbidden)
    with pytest.raises(SystemExit) as exited:
        validator.main(extra)
    assert exited.value.code == expected and calls == []
    captured = capsys.readouterr()
    assert 'usage:' in (captured.out if expected == 0 else captured.err)
    assert not output.exists() and not list(tmp_path.glob('.x04-*'))
