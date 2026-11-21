"""X04 rejects oversized integer tokens independently of Python digit settings."""
import json
import sys
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('runtime_limit', [0, 640, 4300])
@pytest.mark.parametrize('digits', [4301, 10000])
def test_integer_token_refusal_and_valid_recovery(main, runtime_limit, digits, tmp_path, capsys):
    source = tmp_path / 'oversized.json'
    source.write_text(json.dumps(worked_example())[:-1] + ',"opaque":' + '9' * digits + '}', encoding='utf-8')
    original = source.read_bytes()
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    previous_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(runtime_limit)
        assert main([str(source), '--out', str(target)]) == 2
        streams = capsys.readouterr()
        assert streams.out == '' and '4300 digits' in streams.err and 'Traceback' not in streams.err
        assert target.read_bytes() == b'previous' and source.read_bytes() == original
        assert not list(tmp_path.glob('.x04-*'))
        valid = tmp_path / 'valid.json'
        valid.write_text(json.dumps(worked_example()), encoding='utf-8')
        assert main([str(valid), '--out', str(target)]) == 0
        streams = capsys.readouterr()
        assert streams.out == streams.err == ''
        output = json.loads(target.read_bytes())
        result = output.get('result', output.get('recomputed_result'))
        assert result['count'] == 7 and result['highlight_handles'] == ['A', 'B']
    finally:
        sys.set_int_max_str_digits(previous_limit)
