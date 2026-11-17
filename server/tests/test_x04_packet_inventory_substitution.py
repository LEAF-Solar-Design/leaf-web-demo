"""Internally valid packet substitutions cannot bypass caller artifact inventory."""
from copy import deepcopy
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('kind', ['missing', 'extra', 'renamed', 'reordered'])
@pytest.mark.parametrize('stdin', [False, True])
def test_valid_packet_inventory_substitution_and_recovery(kind, stdin, tmp_path, monkeypatch, capsys):
    value = packet()
    if kind == 'missing': value.update(artifacts=value['artifacts'][:1], comparisons=[], batches=[])
    elif kind == 'extra': value['artifacts'].append({'id': 'extra', 'artifact': deepcopy(value['artifacts'][0]['artifact'])})
    elif kind == 'renamed':
        value['artifacts'][1]['id'] = 'renamed'
        value['comparisons'][0]['after'] = 'renamed'
        value['batches'][0]['artifact_ids'] = ['renamed' if item == 'b' else item for item in value['batches'][0]['artifact_ids']]
    else: value['artifacts'].reverse()
    before = deepcopy(value); expected = checker.validate_packet(value); raw = json.dumps(value).encode()
    source = tmp_path / 'packet.json'; source.write_bytes(raw); output = tmp_path / 'summary.json'; output.write_bytes(b'previous')
    def run(ids):
        if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        args = ['-' if stdin else str(source), '--out', str(output)]
        for identity in ids: args += ['--expected-artifact-id', identity]
        return checker.main(args)
    if kind == 'reordered':
        assert run(['a', 'b']) == 0 and json.loads(output.read_bytes()) == expected
    else:
        calls = []
        with monkeypatch.context() as patch:
            def forbidden(item): calls.append(item); raise AssertionError('Wrong inventory must not trust artifacts')
            patch.setattr(checker, 'validate_evidence', forbidden)
            assert run(['a', 'b']) == 2
        assert calls == [] and output.read_bytes() == b'previous'
    assert source.read_bytes() == raw and value == before and not list(tmp_path.glob('.x04-*'))
    assert run([row['id'] for row in value['artifacts']]) == 0 and json.loads(output.read_bytes()) == expected
    assert capsys.readouterr().out == ''
