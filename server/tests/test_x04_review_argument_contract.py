"""Review argument parsing exits before artifact or publication operations."""
import pytest
from test_x04_untrusted_derived_counts import worked_example
import compare_x04_offline_evidence as comparison
import batch_validate_x04_evidence as batch


@pytest.mark.parametrize('module', [comparison, batch])
@pytest.mark.parametrize('case', ['help', 'unknown', 'abbreviation', 'missing',
    'zero-input', 'negative-input', 'invalid-input', 'zero-output', 'negative-output', 'invalid-output'])
def test_usage_does_no_artifact_io(module, case, tmp_path, monkeypatch, capsys):
    target = tmp_path / 'output'; target.write_bytes(b'previous')
    operands = ['never-read.json'] * (2 if module is comparison else 1)
    args = operands + ['--out', str(target)]
    if case == 'help': args += ['--help']
    elif case == 'unknown': args += ['--unknown-flag']
    elif case == 'abbreviation': args += ['--max', '5']
    elif case == 'missing': args = ['--out', str(target)]
    else:
        kind, direction = case.split('-')
        args += ['--max-' + direction + '-bytes', {'zero': '0', 'negative': '-1', 'invalid': 'bad'}[kind]]
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(args); raise AssertionError('Usage must not touch artifacts or output')
    for name in ('read_json', 'write_json', 'check_output_path'):
        monkeypatch.setattr(module, name, forbidden)
    with pytest.raises(SystemExit) as exited: module.main(args)
    assert exited.value.code == (0 if case == 'help' else 2)
    streams = capsys.readouterr()
    assert 'usage:' in (streams.out if case == 'help' else streams.err)
    assert not calls and target.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))
