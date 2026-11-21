"""Retained packet labels cannot hide a bound drawing identity substitution."""
from copy import deepcopy
import json
import pytest
from compare_x04_offline_evidence import compare_evidence
from test_x04_packet_drawing_arguments import invoke, two_drawing_packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('identity,index', [('a', 0), ('b', 1)])
def test_valid_same_packet_label_substitution_refuses_and_recovers(stdin, identity, index, tmp_path, monkeypatch):
    value = two_drawing_packet()
    old_drawing = value['artifacts'][index]['artifact']['drawing']['id']
    value['artifacts'][index]['artifact']['drawing']['id'] = 'replacement'
    value['artifacts'][index]['artifact']['request']['drawing_id'] = 'replacement'
    assert checker.validate_packet(value)['valid']
    output = tmp_path / 'summary.json'
    output.write_bytes(b'previous')
    with monkeypatch.context() as patch:
        def forbidden(*args):
            raise AssertionError('Drawing mismatch must precede trusted calls')
        patch.setattr(checker, 'validate_packet', forbidden)
        patch.setattr(checker, 'validate_evidence', forbidden)
        code, output = invoke(value, stdin, ['--expected-artifact-drawing', identity + '=' + old_drawing], tmp_path, patch)
        assert code == 2 and output.read_bytes() == b'previous'
    code, output = invoke(value, stdin, ['--expected-artifact-drawing', identity + '=replacement'], tmp_path, monkeypatch)
    assert code == 0 and json.loads(output.read_bytes()) == checker.validate_packet(value)


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('key', ['missing', 'c'])
def test_unknown_or_report_mapping_key_refuses_before_trusted_calls(stdin, key, tmp_path, monkeypatch):
    value = two_drawing_packet()
    artifact = value['artifacts'][0]['artifact']
    value['comparisons'] = [{'id': 'c', 'before': 'a', 'after': 'a', 'report': compare_evidence(artifact, artifact)}]
    assert checker.validate_packet(value)['valid']
    output = tmp_path / 'summary.json'
    output.write_bytes(b'previous')
    with monkeypatch.context() as patch:
        def forbidden(*args):
            raise AssertionError('Unknown drawing binding key cannot invoke trusted validators')
        patch.setattr(checker, 'validate_packet', forbidden)
        code, output = invoke(value, stdin, ['--expected-artifact-drawing', key + '=drawing=one'], tmp_path, patch)
        assert code == 2 and output.read_bytes() == b'previous'
    code, output = invoke(value, stdin, ['--expected-artifact-drawing', 'a=drawing=one'], tmp_path, monkeypatch)
    assert code == 0


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('actual,expected', [('first', 'FIRST'), ('\u00e9', 'e\u0301'), ('a ', 'a'), ('a=b', 'a')])
def test_literal_identity_difference_refuses_without_trusted_calls(stdin, actual, expected, tmp_path, monkeypatch):
    value = two_drawing_packet(first=actual)
    output = tmp_path / 'summary.json'
    output.write_bytes(b'previous')
    def forbidden(*args):
        raise AssertionError('Drawing identity must compare literally before trusted checks')
    monkeypatch.setattr(checker, 'validate_packet', forbidden)
    code, output = invoke(value, stdin, ['--expected-artifact-drawing', 'a=' + expected], tmp_path, monkeypatch)
    assert code == 2 and output.read_bytes() == b'previous'


def test_duplicate_actual_artifact_ids_still_refuse_before_trusted_calls(tmp_path, monkeypatch):
    value = two_drawing_packet()
    value['artifacts'].append(deepcopy(value['artifacts'][0]))
    def forbidden(*args):
        raise AssertionError('Duplicate actual artifact IDs must retain original preflight refusal')
    monkeypatch.setattr(checker, 'validate_evidence', forbidden)
    code, output = invoke(value, False, ['--expected-artifact-drawing', 'a=drawing=one'], tmp_path, monkeypatch)
    assert code == 2 and not output.exists()
