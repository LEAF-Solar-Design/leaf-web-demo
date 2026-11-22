"""Input budget, artifact pin, decode, drawing label and semantics have fixed order."""
from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_comparison_delta_algebra import artifact
import validate_x04_offline_evidence as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('kind', ['budget', 'stale-utf8', 'stale-json', 'matching-json', 'wrong-id', 'forged-count'])
def test_artifact_pin_order_and_corrected_control(stdin, kind, tmp_path, monkeypatch, capsys):
    value = artifact(0); identity = value['drawing']['id']; forged = deepcopy(value); forged['result']['count'] = 999
    raw = json.dumps(forged if kind == 'forged-count' else value).encode()
    if kind == 'stale-utf8': raw += b'\xff'
    if kind in ('stale-json', 'matching-json'): raw = b'{broken'
    source = tmp_path / 'artifact.json'; source.write_bytes(raw); output = tmp_path / 'checked.json'; output.write_bytes(b'previous')
    pin = '0' * 64 if kind in ('budget', 'stale-utf8', 'stale-json') else hashlib.sha256(raw).hexdigest()
    args = ['-' if stdin else str(source), '--out', str(output), '--expected-artifact-sha256', pin,
            '--expected-drawing-id', identity if kind == 'forged-count' else identity + '-wrong']
    if kind == 'budget': args += ['--max-input-bytes', str(len(raw) - 1)]
    export = checker.export_evidence; calls = []
    def counted(item): calls.append(item); return export(item)
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'export_evidence', counted)
        if stdin: patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        assert checker.main(args) == 2
    expected = {'budget': 'byte', 'stale-utf8': 'expected_sha256', 'stale-json': 'expected_sha256',
                'matching-json': 'Expecting property', 'wrong-id': 'Expected drawing ID', 'forged-count': 'Derived count claim'}[kind]
    captured = capsys.readouterr()
    assert expected in captured.err and captured.out == '' and 'Traceback' not in captured.err
    assert len(captured.err.splitlines()) == 1 and len(calls) == int(kind == 'forged-count')
    assert source.read_bytes() == raw and output.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))
    valid = json.dumps(value).encode(); source.write_bytes(valid)
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(valid)))
    assert checker.main(['-' if stdin else str(source), '--out', str(output), '--expected-drawing-id', identity,
                         '--expected-artifact-sha256', hashlib.sha256(valid).hexdigest()]) == 0
    assert json.loads(output.read_bytes()) == checker.validate_evidence(value)
