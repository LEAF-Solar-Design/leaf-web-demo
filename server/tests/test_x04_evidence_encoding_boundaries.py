"""X04 CLI evidence accepts UTF-8 BOM and refuses invalid Unicode atomically."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
def test_utf8_bom_is_accepted_with_strict_json_success(main, tmp_path, capsys):
    payload = worked_example()
    payload['drawing']['id'] = 'synthetic-東'
    payload['request']['drawing_id'] = payload['drawing']['id']
    source = tmp_path / 'bom.json'
    source.write_bytes(b'\xef\xbb\xbf' + json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    original = source.read_bytes()
    assert main([str(source)]) == 0
    streams = capsys.readouterr()
    assert streams.err == ''
    result = json.loads(streams.out, parse_constant=lambda value: pytest.fail(value))
    assert result.get('drawing_id', result.get('drawing', {}).get('id')) == 'synthetic-東'
    assert streams.out.encode('utf-8')
    assert source.read_bytes() == original


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('bad_bytes', [b'\xff', b'{"value":"\xc3("}'])
def test_invalid_utf8_bytes_refuse_without_replacement(main, bad_bytes, tmp_path, capsys):
    assert_encoding_refusal(main, bad_bytes, tmp_path, capsys)


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('placement', ['key', 'high-surrogate', 'low-surrogate'])
def test_escaped_lone_surrogates_refuse_before_emission(main, placement, tmp_path, capsys):
    payload = worked_example()
    if placement == 'key':
        payload['\ud800'] = 'extension'
    else:
        payload['opaque'] = '\ud800' if placement == 'high-surrogate' else '\udfff'
    encoded = json.dumps(payload, ensure_ascii=True).encode('utf-8')
    assert_encoding_refusal(main, encoded, tmp_path, capsys)


def assert_encoding_refusal(main, encoded, tmp_path, capsys):
    source = tmp_path / 'bad.json'
    source.write_bytes(encoded)
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    assert main([str(source), '--out', str(target)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'refused' in streams.err and 'Traceback' not in streams.err
    assert target.read_bytes() == b'previous' and source.read_bytes() == encoded
    assert not list(tmp_path.glob('.x04-*'))
