"""Bounded decoding precedes expected identity, then existing recomputation semantics."""
from copy import deepcopy
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_comparison_delta_algebra import artifact
import validate_x04_offline_evidence as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('kind', ['byte-ceiling', 'malformed-json', 'wrong-id-forged', 'matching-id-forged'])
def test_expected_identity_refusal_order_and_valid_recovery(stdin, kind, tmp_path, monkeypatch, capsys):
    value = artifact(0); identity = value['drawing']['id']; forged = deepcopy(value); forged['result']['count'] = 999
    raw = b'{broken' if kind == 'malformed-json' else json.dumps(forged if 'forged' in kind else value).encode()
    source = tmp_path / 'artifact.json'; source.write_bytes(raw); output = tmp_path / 'checked.json'; output.write_bytes(b'previous')
    export = checker.export_evidence; calls = []
    def counted(item): calls.append(item); return export(item)
    args = ['-' if stdin else str(source), '--out', str(output), '--expected-drawing-id',
            identity if kind == 'matching-id-forged' else identity + '-wrong']
    if kind == 'byte-ceiling': args += ['--max-input-bytes', str(len(raw) - 1)]
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'export_evidence', counted)
        if stdin: patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        assert checker.main(args) == 2
    expected = {'byte-ceiling': 'byte', 'malformed-json': 'Expecting property',
                'wrong-id-forged': 'Expected drawing ID', 'matching-id-forged': 'Derived count claim'}[kind]
    captured = capsys.readouterr()
    assert expected in captured.err and captured.out == '' and 'Traceback' not in captured.err
    assert len(captured.err.splitlines()) == 1 and len(calls) == int(kind == 'matching-id-forged')
    assert source.read_bytes() == raw and output.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))
    valid = json.dumps(value).encode(); source.write_bytes(valid)
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(valid)))
    assert checker.main(['-' if stdin else str(source), '--out', str(output), '--expected-drawing-id', identity]) == 0
    assert json.loads(output.read_bytes()) == checker.validate_evidence(value)
