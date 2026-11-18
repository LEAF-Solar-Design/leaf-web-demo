"""One packet may bind separate drawings, but comparison operands must match."""
from copy import deepcopy
import builtins
import io
import os
import pytest
from test_x04_packet_cross_binding import packet
from compare_x04_offline_evidence import compare_evidence
from validate_x04_evidence_packet import validate_packet


def test_multiple_drawings_and_cross_drawing_refusal(monkeypatch):
    first, second = packet(), packet()
    for row in second['artifacts']:
        row['id'] += '2'
        row['artifact']['drawing']['id'] = 'synthetic-second'
        row['artifact']['request']['drawing_id'] = 'synthetic-second'
    a, b = [row['artifact'] for row in second['artifacts']]
    second['comparisons'][0].update(id='c2', before='a2', after='b2', report=compare_evidence(a, b))
    for row in second['batches'][0]['report']['results']:
        row['drawing_id'] = 'synthetic-second'
    value = first
    value['artifacts'] += second['artifacts']; value['comparisons'] += second['comparisons']
    batch = value['batches'][0]
    batch['artifact_ids'] = ['a', 'a2', 'b', 'b2']
    rows = [batch['report']['results'][0], second['batches'][0]['report']['results'][0],
            batch['report']['results'][1], second['batches'][0]['report']['results'][1]]
    batch['report'].update(results=rows, total=4, passed=4)
    for index, row in enumerate(rows): row['index'] = index
    snapshot = deepcopy(value)
    opened = []
    def guard(opener):
        def checked(path, *args, **kwargs):
            if str(path).endswith(('.shx', 'unopened.json')):
                opened.append(path); raise AssertionError('External target opened')
            return opener(path, *args, **kwargs)
        return checked
    for module in (builtins, io, os): monkeypatch.setattr(module, 'open', guard(module.open))
    result = validate_packet(value)
    assert [(r['drawing_id'], r['recomputed_result']['count']) for r in result['validated_artifacts']] == [
        (a['drawing']['id'], count) for a, count in zip(
            [row['artifact'] for row in value['artifacts']], [6, 1, 6, 1])]
    assert [r['report']['count'] for r in result['comparisons']] == [{'before': 6, 'after': 1, 'delta': -5}] * 2
    assert [r['drawing_id'] for r in result['batches'][0]['report']['results']] == [
        first['artifacts'][0]['artifact']['drawing']['id'], 'synthetic-second'] * 2
    assert [r['recomputed_result']['count'] for r in result['batches'][0]['report']['results']] == [6, 6, 1, 1]
    value['comparisons'][0]['after'] = 'b2'
    bad = deepcopy(value)
    with pytest.raises(ValueError, match='identities'): validate_packet(value)
    assert value == bad
    value['comparisons'][0]['after'] = 'b'
    assert validate_packet(value) == result and value == snapshot and opened == []
