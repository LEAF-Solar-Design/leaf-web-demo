"""Comparison operand expectations parse atomically before packet I/O."""
from copy import deepcopy
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
from compare_x04_offline_evidence import compare_evidence
import validate_x04_evidence_packet as checker


def operand_packet():
    value = packet()
    value['batches'] = []
    return value


def invoke(value, stdin, options, tmp_path, monkeypatch):
    raw = json.dumps(value).encode()
    source = tmp_path / 'packet.json'; source.write_bytes(raw)
    output = tmp_path / 'summary.json'
    if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
    code = checker.main(['-' if stdin else str(source), '--out', str(output), *options])
    assert source.read_bytes() == raw and not list(tmp_path.glob('.x04-*'))
    return code, output


@pytest.mark.parametrize('options', [
    *[['--expected-comparison-operands', value] for value in (
        '', 'c', 'c=', 'c=a', 'c=a,', 'c=,b', 'c=a,b,c', 'c=a=b,b',
        '../c=a,b', 'c=../a,b', 'c=a,../b', 'c=a, b', 'c= a,b',
        'c=1,b', '1=a,b', 'c=a,\u00e9', 'c=a,b=', 'c=a,b,')],
    ['--expected-comparison-operands', 'c=a,b', '--expected-comparison-operands', 'c=b,a'],
    ['--expected-comparison-operands', 'c=a,b', '--expected-comparison-operands', 'c=a,b'],
    ['--expected-comparison-operands'], ['--expected-comparison-operand', 'c=a,b'],
])
def test_operand_usage_refuses_before_io(options, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs): raise AssertionError('Usage must precede I/O')
    for name in ('check_output_path', 'read_json', 'validate_packet', 'validate_evidence', 'write_json'):
        monkeypatch.setattr(checker, name, forbidden)
    output = tmp_path / 'never.json'
    with pytest.raises(SystemExit) as caught:
        checker.main(['-', '--out', str(output), *options])
    captured = capsys.readouterr()
    assert caught.value.code == 2 and not output.exists()
    assert 'usage:' in captured.err and captured.out == '' and 'Traceback' not in captured.err


@pytest.mark.parametrize('stdin', [False, True])
def test_partial_operand_maps_and_omitted_compatibility(stdin, tmp_path, monkeypatch):
    value = operand_packet()
    second = deepcopy(value['comparisons'][0])
    second.update(id='d', before='b', after='a', report=compare_evidence(
        value['artifacts'][1]['artifact'], value['artifacts'][0]['artifact']))
    value['comparisons'].append(second)
    expected = checker.validate_packet(value)
    before = deepcopy(value)
    for mappings in ([], ['c=a,b'], ['d=b,a', 'c=a,b']):
        options = []
        for mapping in mappings: options += ['--expected-comparison-operands', mapping]
        code, output = invoke(value, stdin, options, tmp_path, monkeypatch)
        assert code == 0 and json.loads(output.read_bytes()) == expected
    assert value == before


@pytest.mark.parametrize('stdin', [False, True])
def test_self_comparison_is_an_ordered_valid_pair(stdin, tmp_path, monkeypatch):
    value = operand_packet(); row = value['comparisons'][0]
    row.update(after='a', report=compare_evidence(value['artifacts'][0]['artifact'], value['artifacts'][0]['artifact']))
    code, output = invoke(value, stdin, ['--expected-comparison-operands', 'c=a,a'], tmp_path, monkeypatch)
    assert code == 0 and json.loads(output.read_bytes()) == checker.validate_packet(value)
