"""X04 help and usage terminate before touching input or output files."""
from pathlib import Path
import pytest
from test_x04_untrusted_derived_counts import export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('case,expected', [('help', 0), ('unknown', 2), ('abbreviation', 2),
    ('missing-output', 2), ('missing-input', 2), ('invalid-budget', 2), ('missing-budget', 2)])
def test_help_and_usage_have_no_input_output_io(main, case, expected, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source.json'
    target = tmp_path / 'output.json'
    arguments = {
        'help': [str(source), '--help'],
        'unknown': [str(source), '--unknown', '--out', str(target)],
        'abbreviation': [str(source), '--ou', str(target)],
        'missing-output': [str(source), '--out'],
        'missing-input': [],
        'invalid-budget': [str(source), '--max-input-bytes', '0', '--out', str(target)],
        'missing-budget': [str(source), '--max-input-bytes'],
    }
    def forbidden(*args, **kwargs):
        raise AssertionError('Help and usage must not touch input or output')
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(Path, 'stat', forbidden)
    with pytest.raises(SystemExit) as stopped:
        main(arguments[case])
    assert stopped.value.code == expected
    streams = capsys.readouterr()
    if expected == 0:
        assert 'usage:' in streams.out and '--max-input-bytes' in streams.out
        assert streams.err == ''
    else:
        assert streams.out == '' and 'usage:' in streams.err and 'error:' in streams.err
        assert 'Traceback' not in streams.err
