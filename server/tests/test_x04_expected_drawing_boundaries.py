"""Expected artifact identity guards parse before I/O and mismatch before recompute."""
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_comparison_delta_algebra import artifact
import validate_x04_offline_evidence as checker


@pytest.mark.parametrize('extra', [
    ['--expected-drawing-id', ''], ['--expected-drawing-id', '  '],
    ['--expected-drawing-id', '\t\n'], ['--expected-drawing-id'],
    ['--expected-drawing', 'x'], ['--expected-drawing-i', 'x'],
])
def test_expected_identity_usage_before_io(extra, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs): raise AssertionError('Usage must precede I/O and recomputation')
    for name in ('check_output_path', 'read_json', 'export_evidence', 'write_json'):
        monkeypatch.setattr(checker, name, forbidden)
    output = tmp_path / 'never.json'
    with pytest.raises(SystemExit) as caught: checker.main(['-', '--out', str(output), *extra])
    assert caught.value.code == 2 and not output.exists()
    captured = capsys.readouterr(); assert captured.out == '' and 'usage:' in captured.err and 'Traceback' not in captured.err


@pytest.mark.parametrize('stdin', [False, True])
def test_wrong_identity_zero_recompute_default_and_retry(stdin, tmp_path, monkeypatch, capsys):
    value = artifact(0); raw = json.dumps(value).encode(); identity = value['drawing']['id']
    source = tmp_path / 'artifact.json'; source.write_bytes(raw); output = tmp_path / 'checked.json'
    expected = checker.validate_evidence(value)
    def run(selected=None):
        if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        args = ['-' if stdin else str(source), '--out', str(output)]
        if selected is not None: args += ['--expected-drawing-id', selected]
        return checker.main(args)
    assert run() == 0 and json.loads(output.read_bytes()) == expected
    previous = output.read_bytes(); calls = []
    def forbidden(item): calls.append(item); raise AssertionError('Mismatched identity must not recompute')
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'export_evidence', forbidden)
        assert run(identity + '-other') == 2
    assert calls == [] and output.read_bytes() == previous and source.read_bytes() == raw
    assert run(identity) == 0 and output.read_bytes() == previous
    assert expected['recomputed_result']['original_bytes_integrity'] == 'unverified'
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
