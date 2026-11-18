"""The X04 schema version discriminator and derived count use exact types."""
from copy import deepcopy
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main
from validate_x04_offline_evidence import validate_evidence


def assert_atomic_refusal(main, payload, tmp_path, capsys):
    before = deepcopy(payload)
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(payload), encoding='utf-8')
    original = source.read_bytes()
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    assert main([str(source), '--out', str(target)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'refused' in streams.err and 'Traceback' not in streams.err
    assert target.read_bytes() == b'previous' and source.read_bytes() == original
    assert payload == before and not list(tmp_path.glob('.x04-*'))


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('version', [True, 1, '1', 1.5, None])
def test_schema_version_discriminator_requires_exact_schema_string(main, version, tmp_path, capsys):
    payload = worked_example()
    payload['schema'] = version
    assert_atomic_refusal(main, payload, tmp_path, capsys)


@pytest.mark.parametrize('count', [True, '7', 7.0, False, 7.5, None])
def test_count_claim_requires_exact_integer_type(count, tmp_path, capsys):
    payload = worked_example()
    payload['result']['count'] = count
    assert_atomic_refusal(validate_main, payload, tmp_path, capsys)


def test_valid_version_and_integer_count_remain_accepted():
    payload = worked_example()
    before = deepcopy(payload)
    assert validate_evidence(payload)['recomputed_result']['count'] == 7
    assert payload == before
