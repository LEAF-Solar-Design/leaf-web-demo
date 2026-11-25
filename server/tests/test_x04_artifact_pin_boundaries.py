"""Optional artifact digest guards raw file/stdin bytes before trusted validation."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_x04_comparison_delta_algebra import artifact
import validate_x04_offline_evidence as checker


@pytest.mark.parametrize('pin', ['', 'g' * 64, 'a' * 63, 'a' * 65, ' ' + 'a' * 64, 'a' * 64 + '\n', '0x' + 'a' * 64])
def test_malformed_artifact_pin_is_usage_before_io(pin, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs): raise AssertionError('Malformed pin must precede reads and validation')
    for name in ('check_output_path', 'read_json', 'validate_evidence', 'write_json'):
        monkeypatch.setattr(checker, name, forbidden)
    with pytest.raises(SystemExit) as caught:
        checker.main(['-', '--expected-artifact-sha256', pin, '--out', str(tmp_path / 'never')])
    assert caught.value.code == 2 and not (tmp_path / 'never').exists()
    assert 'Traceback' not in capsys.readouterr().err


@pytest.mark.parametrize('stdin', [False, True])
def test_artifact_pin_match_unpinned_tamper_and_refresh(stdin, tmp_path, monkeypatch, capsys):
    value = artifact(0); raw = b'\xef\xbb\xbf' + json.dumps(value).encode()
    source = tmp_path / 'artifact.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; expected = checker.validate_evidence(value)
    def run(data, pin=None):
        if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(data)))
        else: source.write_bytes(data)
        args = ['-' if stdin else str(source), '--out', str(output)]
        if pin is not None: args += ['--expected-artifact-sha256', pin]
        return checker.main(args)
    digest = hashlib.sha256(raw).hexdigest()
    assert run(raw, digest.upper()) == 0 and json.loads(output.read_bytes()) == expected
    previous = output.read_bytes()
    with monkeypatch.context() as patch:
        original_read = checker.read_json; arities = []
        def compatible(*args):
            arities.append(len(args)); return original_read(*args)
        patch.setattr(checker, 'read_json', compatible)
        assert run(raw) == 0 and output.read_bytes() == previous
    assert arities == [2]
    changed = raw + b' '
    with monkeypatch.context() as patch:
        def forbidden(*args): raise AssertionError('Stale bytes must precede artifact checks')
        patch.setattr(checker, 'export_evidence', forbidden)
        assert run(changed, digest) == 2 and output.read_bytes() == previous
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
    assert run(changed, hashlib.sha256(changed).hexdigest()) == 0 and output.read_bytes() == previous
