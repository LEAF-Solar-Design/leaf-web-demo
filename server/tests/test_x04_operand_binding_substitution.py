"""Internally valid operand substitution still fails fixed caller bindings."""
from copy import deepcopy
import json
import pytest
from test_x04_operand_binding_arguments import operand_packet, invoke
from compare_x04_offline_evidence import compare_evidence
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('before,after', [('b', 'a'), ('a', 'a'), ('z', 'b'), ('a', 'z')])
def test_valid_operand_substitution_requires_caller_rebinding(stdin, before, after, tmp_path, monkeypatch, capsys):
    value = operand_packet()
    extra = deepcopy(value['artifacts'][0]); extra['id'] = 'z'; value['artifacts'].append(extra)
    by_id = {row['id']: row['artifact'] for row in value['artifacts']}
    value['comparisons'][0].update(before=before, after=after, report=compare_evidence(by_id[before], by_id[after]))
    expected = checker.validate_packet(value); original = deepcopy(value)
    output = tmp_path / 'summary.json'; output.write_bytes(b'previous')
    with monkeypatch.context() as patch:
        def forbidden(*args): raise AssertionError('Operand mismatch must precede trusted validation')
        patch.setattr(checker, 'validate_evidence', forbidden)
        code, output = invoke(value, stdin, ['--expected-comparison-operands', 'c=a,b'], tmp_path, monkeypatch)
        assert code == 2 and output.read_bytes() == b'previous'
    captured = capsys.readouterr()
    assert 'ordered before/after' in captured.err and captured.out == '' and 'Traceback' not in captured.err
    code, output = invoke(value, stdin, ['--expected-comparison-operands', f'c={before},{after}'], tmp_path, monkeypatch)
    assert code == 0 and json.loads(output.read_bytes()) == expected and value == original


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('identity', ['unknown', 'a', 'batch'])
def test_unknown_or_noncomparison_mapping_key_refuses(stdin, identity, tmp_path, monkeypatch):
    value = operand_packet(); output = tmp_path / 'summary.json'; output.write_bytes(b'previous')
    with monkeypatch.context() as patch:
        def forbidden(*args): raise AssertionError('Unknown comparison key must precede trusted calls')
        patch.setattr(checker, 'validate_evidence', forbidden)
        code, output = invoke(value, stdin, ['--expected-comparison-operands', identity + '=a,b'], tmp_path, monkeypatch)
        assert code == 2 and output.read_bytes() == b'previous'
    assert invoke(value, stdin, ['--expected-comparison-operands', 'c=a,b'], tmp_path, monkeypatch)[0] == 0
