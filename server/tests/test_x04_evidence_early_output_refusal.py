"""X04 output encoding stops at the first over-budget producer chunk."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('destination', ['stdout', 'file'])
@pytest.mark.parametrize('mode', ['tiny-refusal', 'exact-success'])
def test_real_encoder_consumption_and_exact_bytes(main, destination, mode, tmp_path, monkeypatch, capsys):
    payload = worked_example()
    payload['drawing']['future_extension'] = {'entries': [{'text': 'LATE_ENCODER_SENTINEL' * 20} for _ in range(1000)]}
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(payload), encoding='utf-8')
    before = source.read_bytes()
    assert main([str(source)]) == 0
    baseline = capsys.readouterr()
    assert baseline.err == ''
    expected = baseline.out.encode('utf-8')
    full_chunks = list(json.JSONEncoder(ensure_ascii=True, allow_nan=False, indent=2).iterencode(json.loads(expected)))
    consumed = []
    original = json.JSONEncoder.iterencode
    def observed(encoder, value, *args, **kwargs):
        for chunk in original(encoder, value, *args, **kwargs):
            if encoder.indent == 2:
                consumed.append(chunk)
            yield chunk
    monkeypatch.setattr(json.JSONEncoder, 'iterencode', observed)
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    limit = 64 if mode == 'tiny-refusal' else len(expected)
    args = [str(source), '--max-output-bytes', str(limit)]
    if destination == 'file':
        args += ['--out', str(target)]
    status = main(args)
    streams = capsys.readouterr()
    assert source.read_bytes() == before and not list(tmp_path.glob('.x04-*'))
    if mode == 'tiny-refusal':
        assert status == 2 and streams.out == '' and 'Encoded output exceeds' in streams.err
        assert target.read_bytes() == b'previous'
        assert 0 < len(consumed) < len(full_chunks)
        assert sum(map(len, consumed)) + 1 > limit
        assert 'LATE_ENCODER_SENTINEL' not in ''.join(consumed)
    else:
        assert status == 0 and streams.err == '' and consumed == full_chunks
        if destination == 'file':
            assert streams.out == '' and target.read_bytes() == expected
        else:
            assert streams.out.encode('utf-8') == expected
