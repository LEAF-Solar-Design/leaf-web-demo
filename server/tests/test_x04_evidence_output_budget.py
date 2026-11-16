"""X04 encoded-output limits refuse before stdout or file publication."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('destination', ['stdout', 'file'])
@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_exact_encoded_output_boundary(main, destination, offset, tmp_path, capsys):
    payload = worked_example()
    payload['drawing']['future_extension'] = {'literal': '東\U0001f680'}
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    before = source.read_bytes()
    assert main([str(source)]) == 0
    baseline = capsys.readouterr()
    assert baseline.err == ''
    expected = baseline.out.encode('utf-8')
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous complete output')
    args = [str(source), '--max-output-bytes', str(len(expected) + offset)]
    if destination == 'file':
        args += ['--out', str(target)]
    status = main(args)
    streams = capsys.readouterr()
    assert source.read_bytes() == before and not list(tmp_path.glob('.x04-*'))
    if offset == -1:
        assert status == 2 and streams.out == ''
        assert 'Encoded output exceeds' in streams.err
        assert target.read_bytes() == b'previous complete output'
    else:
        assert status == 0 and streams.err == ''
        if destination == 'file':
            assert streams.out == '' and target.read_bytes() == expected
        else:
            assert streams.out.encode('utf-8') == expected
            assert target.read_bytes() == b'previous complete output'
        output = json.loads(expected)
        result = output.get('result', output.get('recomputed_result'))
        assert result['count'] == 7 and result['highlight_handles'] == ['A', 'B']
