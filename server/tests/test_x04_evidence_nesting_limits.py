"""X04 deeply nested evidence refuses atomically and leaves the CLI reusable."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('shape', ['array', 'object'])
def test_nested_json_refuses_then_valid_request_recovers(main, shape, tmp_path, capsys):
    opening, closing = ('[', ']') if shape == 'array' else ('{"value":', '}')
    nested = opening * 5000 + '0' + closing * 5000
    text = json.dumps(worked_example())[:-1] + ',"opaque":' + nested + '}'
    source = tmp_path / 'nested.json'
    source.write_text(text, encoding='utf-8')
    original = source.read_bytes()
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous complete output')
    assert main([str(source), '--out', str(target)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'nesting' in streams.err
    assert 'Traceback' not in streams.err
    assert source.read_bytes() == original
    assert target.read_bytes() == b'previous complete output'
    assert not list(tmp_path.glob('.x04-*'))
    valid = tmp_path / 'valid.json'
    valid.write_text(json.dumps(worked_example()), encoding='utf-8')
    assert main([str(valid), '--out', str(target)]) == 0
    streams = capsys.readouterr()
    assert streams.out == streams.err == ''
    output = json.loads(target.read_bytes())
    result = output.get('result', output.get('recomputed_result'))
    assert result['count'] == 7 and result['highlight_handles'] == ['A', 'B']


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('list_levels,expected_status', [(255, 0), (256, 2)])
def test_explicit_container_depth_boundary(main, list_levels, expected_status, tmp_path, capsys):
    payload = worked_example()
    value = 'scalar with literal [brackets] and {braces}'
    for _ in range(list_levels):
        value = [value]
    payload['opaque'] = value
    source = tmp_path / 'boundary.json'
    source.write_text(json.dumps(payload), encoding='utf-8')
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    assert main([str(source), '--out', str(target)]) == expected_status
    streams = capsys.readouterr()
    assert streams.out == ''
    if expected_status == 2:
        assert '256 container levels' in streams.err
        assert target.read_bytes() == b'previous'
    else:
        assert streams.err == '' and json.loads(target.read_bytes())
    assert not list(tmp_path.glob('.x04-*'))
