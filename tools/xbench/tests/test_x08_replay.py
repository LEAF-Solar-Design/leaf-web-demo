"""Generic synthetic evidence and receipts; never a qualified live run."""

from copy import deepcopy
import json
import os

import pytest

from benches import x08_replay
from benches.x08_provenance import validate_provenance


@pytest.fixture
def samples():
    handles = [f'synthetic-{i}' for i in range(500)]
    return {
        'synthetic': True,
        'geometry': dict(rendered_module_count=12, marker_module_count=0,
                         omitted_module_count=0),
        'single_select_ms': [10] * 20,
        'window_select': [dict(latency_ms=20, expected_handles=handles.copy(),
                               selected_handles=handles.copy(), oracle_independent=True)
                          for _ in range(5)],
        'job_ack': [dict(latency_ms=30, http_status=202, rendered=True) for _ in range(10)],
        'frame_deltas_ms': [16] * 600, 'cold_run_discarded': True,
        'sequence': dict(pan_drags=4, zoom_in_steps=10, zoom_out_steps=10, duration_ms=10000),
        'reference': dict(hostname='EVANS-DEKSTOP', os='Windows 11 synthetic',
                          cpu='synthetic CPU', gpu=['synthetic GPU'], free_ram_bytes=1024,
                          chromium_version='synthetic', headed=True,
                          viewport=dict(width=1920, height=1080), device_scale_factor=1),
    }


def replay(tmp_path, samples):
    source, output = tmp_path / 'samples.json', tmp_path / 'diagnostics.json'
    original = (' \n' + json.dumps(samples) + '\n').encode()
    source.write_bytes(original)
    code = x08_replay.main(['--samples', str(source), '--expected-modules', '12',
                            '--out', str(output)])
    assert source.read_bytes() == original
    return code, json.loads(output.read_bytes())


def test_valid_six_diagnostics_and_input_preserved(tmp_path, samples):
    code, report = replay(tmp_path, samples)
    assert code == 0 and report['verdict'] == 'PASS'
    assert report['schema'] == 'x08_offline_replay_diagnostic_v1'
    assert report['diagnostic_kind'] == 'synthetic_non_scored'
    assert report['qualified_live_grade'] is False
    assert report['provenance_validated'] is False
    assert [step['n'] for step in report['steps']] == list(range(1, 7))
    assert all(step['verdict'] == 'PASS' and step['evidence'] and step['expected']
               for step in report['steps'])
    assert list(tmp_path.glob('*.tmp')) == []


@pytest.mark.parametrize('key,budget', [('single_select_ms', 100), ('window_select', 100),
                                       ('job_ack', 200), ('frame_deltas_ms', 33)])
def test_strict_budget_equality_readable_fail(tmp_path, samples, key, budget):
    if key in ('window_select', 'job_ack'):
        for row in samples[key]:
            row['latency_ms'] = budget
    else:
        samples[key] = [budget] * len(samples[key])
    code, report = replay(tmp_path, samples)
    assert code == 1 and report['verdict'] == 'FAIL'


def test_nearest_rank(tmp_path, samples):
    samples['single_select_ms'] = [1] * 18 + [99, 1000]
    code, report = replay(tmp_path, samples)
    assert code == 0
    assert report['steps'][1]['observed']['p95_ms'] == 99


@pytest.mark.parametrize('cold', [False, None, 1])
def test_cold_discard_refusal(tmp_path, samples, cold):
    samples['cold_run_discarded'] = cold
    code, report = replay(tmp_path, samples)
    assert code == 4 and report['verdict'] == 'UNREADABLE'
    assert [s['verdict'] for s in report['steps'][1:5]] == ['UNREADABLE'] * 4


def test_unreadable_takes_precedence(tmp_path, samples):
    samples['geometry']['rendered_module_count'] = 0
    del samples['reference']
    code, report = replay(tmp_path, samples)
    assert code == 4 and report['verdict'] == 'UNREADABLE'


def test_missing_metadata_and_599(tmp_path, samples):
    samples['frame_deltas_ms'].pop()
    code, report = replay(tmp_path, samples)
    assert code == 4 and report['steps'][4]['verdict'] == 'UNREADABLE'
    code, report = replay(tmp_path, {})
    assert code == 4 and len(report['steps']) == 6


def test_non_synthetic_still_not_qualified(tmp_path, samples):
    del samples['synthetic']
    code, report = replay(tmp_path, samples)
    assert code == 0 and report['diagnostic_kind'] == 'offline_non_scored'
    assert report['qualified_live_grade'] is False


@pytest.mark.parametrize('payload', [b'{', b'{"a":', b'[]', b'null', b'1', b'"text"',
                                    b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}',
                                    b'{"a":1e999}', b'\xff'])
def test_invalid_json_no_traceback_or_output(tmp_path, capsys, payload):
    source, output = tmp_path / 'samples.json', tmp_path / 'out.json'
    source.write_bytes(payload)
    assert x08_replay.main(['--samples', str(source), '--expected-modules', '12',
                             '--out', str(output)]) == 4
    assert source.read_bytes() == payload and not output.exists()
    error = capsys.readouterr().err
    assert 'unreadable' in error and 'Traceback' not in error


def test_missing_sample_file(tmp_path):
    assert x08_replay.main(['--samples', str(tmp_path / 'absent.json'),
                             '--expected-modules', '12', '--out', str(tmp_path / 'out')]) == 4


@pytest.mark.parametrize('alias', ['same', 'relative', 'hardlink', 'symlink'])
def test_output_alias_refused(tmp_path, samples, alias):
    source = tmp_path / 'samples.json'
    original = json.dumps(samples).encode()
    source.write_bytes(original)
    output = tmp_path / 'alias.json'
    if alias == 'same':
        output = source
    elif alias == 'relative':
        output = tmp_path / '.' / 'samples.json'
    else:
        try:
            if alias == 'hardlink':
                os.link(source, output)
            else:
                output.symlink_to(source)
        except (OSError, NotImplementedError):
            pytest.skip('filesystem does not support this alias')
    assert x08_replay.main(['--samples', str(source), '--expected-modules', '12',
                             '--out', str(output)]) == 4
    assert source.read_bytes() == original


@pytest.mark.parametrize('failure', ['temporary', 'replace'])
def test_output_oserror_portable(tmp_path, samples, monkeypatch, capsys, failure):
    source, output = tmp_path / 'samples.json', tmp_path / 'out.json'
    original = json.dumps(samples).encode()
    source.write_bytes(original)
    output.write_bytes(b'previous diagnostic')

    def denied(*args, **kwargs):
        raise OSError('synthetic write denial')

    if failure == 'temporary':
        monkeypatch.setattr(x08_replay.tempfile, 'NamedTemporaryFile', denied)
    else:
        monkeypatch.setattr(x08_replay.os, 'replace', denied)
    assert x08_replay.main(['--samples', str(source), '--expected-modules', '12',
                             '--out', str(output)]) == 4
    assert source.read_bytes() == original
    assert output.read_bytes() == b'previous diagnostic'
    assert not list(tmp_path.glob('.*.tmp'))
    assert 'Traceback' not in capsys.readouterr().err


@pytest.fixture
def receipts(samples):
    binding = dict(source_sha='b' * 40, fixture_sha256='a' * 64,
                   source_binding_verified=True)
    return dict(binding, bytes=123), dict(binding, reference=samples['reference'])


def validate(receipts, **kwargs):
    expected = dict(expected_fixture_sha256='a' * 64, expected_source_sha='b' * 40)
    expected.update(kwargs)
    return validate_provenance(*receipts, **expected)


def test_provenance_normalized_copied_and_hardware_diagnostic(receipts):
    original = deepcopy(receipts)
    result = validate(receipts, expected_fixture_sha256='A' * 64)
    assert result['fixture_sha256'] == 'a' * 64 and result['bytes'] == 123
    result['reference']['gpu'].append('other')
    assert receipts == original
    receipts[1]['reference']['hostname'] = 'synthetic-other-host'
    result = validate(receipts)
    assert result['reference_diagnostic']['verdict'] == 'FAIL'


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('key,value', [('source_sha', 'c' * 40),
                                       ('fixture_sha256', 'c' * 64),
                                       ('sha256', 'c' * 64),
                                       ('source_binding_verified', False),
                                       ('source_binding_verified', 1)])
def test_provenance_conflicting_unverified(receipts, index, key, value):
    receipts[index][key] = value
    with pytest.raises(ValueError):
        validate(receipts)


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('key', ['source_sha', 'fixture_sha256', 'source_binding_verified'])
def test_provenance_missing_bindings(receipts, index, key):
    del receipts[index][key]
    with pytest.raises(ValueError):
        validate(receipts)


@pytest.mark.parametrize('value', [0, -1, True, 1.5, None])
def test_fixture_bytes_required(receipts, value):
    receipts[0]['bytes'] = value
    with pytest.raises(ValueError):
        validate(receipts)


@pytest.mark.parametrize('key', ['hostname', 'os', 'cpu', 'gpu', 'free_ram_bytes',
                                'chromium_version', 'headed', 'viewport', 'device_scale_factor'])
def test_reference_fields_required(receipts, key):
    del receipts[1]['reference'][key]
    with pytest.raises(ValueError, match='metadata'):
        validate(receipts)


@pytest.mark.parametrize('key', ['expected_fixture_sha256', 'expected_source_sha'])
@pytest.mark.parametrize('value', [None, '', 'a', 'g' * 64, ' a' * 32])
def test_expected_hashes_strict(receipts, key, value):
    with pytest.raises(ValueError):
        validate(receipts, **{key: value})


def test_expected_hashes_mandatory(receipts):
    with pytest.raises(TypeError):
        validate_provenance(*receipts)
