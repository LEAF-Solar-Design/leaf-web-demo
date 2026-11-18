"""Packet drawing bindings parse before I/O and retain literal identity."""
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


def two_drawing_packet(first='drawing=one', second='drawing-two'):
    value = packet()
    value['comparisons'] = []
    value['batches'] = []
    value['artifacts'][0]['artifact']['drawing']['id'] = first
    value['artifacts'][0]['artifact']['request']['drawing_id'] = first
    value['artifacts'][1]['artifact']['drawing']['id'] = second
    value['artifacts'][1]['artifact']['request']['drawing_id'] = second
    return value


def invoke(value, stdin, options, tmp_path, monkeypatch):
    raw = json.dumps(value).encode()
    source = tmp_path / 'packet.json'
    source.write_bytes(raw)
    output = tmp_path / 'summary.json'
    if stdin:
        monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
    code = checker.main(['-' if stdin else str(source), '--out', str(output), *options])
    assert source.read_bytes() == raw and not list(tmp_path.glob('.x04-*'))
    return code, output


@pytest.mark.parametrize('options', [
    *[['--expected-artifact-drawing', value] for value in
      ('', 'a', 'a=', 'a= ', '=drawing', '../a=d', '1=d', '\u00e9=d', 'a' * 65 + '=d')],
    ['--expected-artifact-drawing', 'a=one', '--expected-artifact-drawing', 'a=two'],
    ['--expected-artifact-drawing', 'a=one', '--expected-artifact-drawing', 'a=one'],
    ['--expected-artifact-drawing'], ['--expected-artifact-drawin', 'a=one'],
])
def test_mapping_usage_refuses_before_io(options, monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError('Mapping argument refusal must precede I/O')
    for name in ('check_output_path', 'read_json', 'validate_packet', 'validate_evidence', 'write_json'):
        monkeypatch.setattr(checker, name, forbidden)
    output = tmp_path / 'never.json'
    with pytest.raises(SystemExit) as caught:
        checker.main(['-', '--out', str(output), *options])
    captured = capsys.readouterr()
    assert caught.value.code == 2 and not output.exists()
    assert 'usage:' in captured.err and 'Traceback' not in captured.err and captured.out == ''


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('drawing', [' alpha ', 'alpha=revision=2', '../drawing?x=1', '\u00e9', 'e\u0301', 'drawing'])
def test_first_equals_split_and_literal_drawing_label(stdin, drawing, tmp_path, monkeypatch):
    value = two_drawing_packet(first=drawing)
    assert checker.artifact_drawing('a=' + drawing) == ('a', drawing)
    code, output = invoke(value, stdin, ['--expected-artifact-drawing', 'a=' + drawing], tmp_path, monkeypatch)
    assert code == 0 and json.loads(output.read_bytes()) == checker.validate_packet(value)


@pytest.mark.parametrize('stdin', [False, True])
def test_partial_maps_and_omitted_compatibility(stdin, tmp_path, monkeypatch):
    value = two_drawing_packet()
    expected = checker.validate_packet(value)
    for mappings in ([], ['a=drawing=one'], ['b=drawing-two', 'a=drawing=one']):
        options = []
        for mapping in mappings:
            options += ['--expected-artifact-drawing', mapping]
        code, output = invoke(value, stdin, options, tmp_path, monkeypatch)
        assert code == 0 and json.loads(output.read_bytes()) == expected
