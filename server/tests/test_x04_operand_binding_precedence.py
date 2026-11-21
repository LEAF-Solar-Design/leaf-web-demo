"""Packet refusal order retains semantic checks after operand matching."""
from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_operand_binding_arguments import operand_packet, invoke
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('kind', ['budget', 'stale-pin', 'decode', 'inventory', 'drawing',
                                 'unknown-operands', 'wrong-operands', 'forged', 'duplicate'])
def test_operand_refusal_precedence_and_retry(stdin, kind, tmp_path, monkeypatch, capsys):
    value = operand_packet()
    if kind == 'forged': value['artifacts'][0]['artifact']['result']['count'] = 999
    if kind == 'duplicate': value['comparisons'].append(deepcopy(value['comparisons'][0]))
    raw = b'{broken' if kind in ('stale-pin', 'decode') else json.dumps(value).encode()
    source = tmp_path / 'packet.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'; output.write_bytes(b'previous')
    pin = '0' * 64 if kind in ('budget', 'stale-pin') else hashlib.sha256(raw).hexdigest()
    mapping = 'c=a,b' if kind in ('forged', 'duplicate') else 'unknown=a,b' if kind == 'unknown-operands' else 'c=b,a'
    options = ['-' if stdin else str(source), '--out', str(output), '--expected-packet-sha256', pin,
               '--expected-artifact-id', 'a', '--expected-artifact-drawing',
               'a=wrong' if kind == 'drawing' else 'a=' + value['artifacts'][0]['artifact']['drawing']['id'],
               '--expected-comparison-operands', mapping]
    if kind != 'inventory': options += ['--expected-artifact-id', 'b']
    if kind == 'budget': options += ['--max-input-bytes', str(len(raw) - 1)]
    calls = []; trusted = checker.validate_evidence
    def observed(item): calls.append(item); return trusted(item)
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_evidence', observed)
        if stdin: patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        assert checker.main(options) == 2
    message = {'budget': 'byte', 'stale-pin': 'expected_sha256', 'decode': 'Expecting property',
               'inventory': 'expected inventory', 'drawing': 'drawing.id exactly',
               'unknown-operands': 'must name an embedded comparison', 'wrong-operands': 'ordered before/after',
               'forged': 'Derived count claim', 'duplicate': 'globally unique'}[kind]
    captured = capsys.readouterr()
    assert message in captured.err and captured.out == '' and 'Traceback' not in captured.err
    assert len(calls) == int(kind == 'forged')
    assert source.read_bytes() == raw and output.read_bytes() == b'previous'
    corrected = operand_packet()
    code, output = invoke(corrected, stdin, ['--expected-comparison-operands', 'c=a,b'], tmp_path, monkeypatch)
    assert code == 0 and json.loads(output.read_bytes()) == checker.validate_packet(corrected)
