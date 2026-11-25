"""X04 report inventory substitution controls using synthetic packets."""
from copy import deepcopy
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
from test_x04_report_inventory import REPORT_OPTIONS, run_packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('guard', ['--expected-comparison-id', '--expected-batch-id'])
def test_cross_section_ids_cannot_satisfy_report_inventory(stdin, guard, tmp_path, monkeypatch):
    value = packet()
    value['comparisons'][0]['id'], value['batches'][0]['id'] = 'batch', 'c'
    assert checker.validate_packet(value)['valid']
    expected = 'c' if guard == '--expected-comparison-id' else 'batch'
    output = tmp_path / 'summary.json'
    output.write_bytes(b'prior report')

    def forbidden(*args, **kwargs):
        raise AssertionError('A matching ID in another report section must not reach trusted checks')

    monkeypatch.setattr(checker, 'validate_packet', forbidden)
    code, output = run_packet(value, stdin, [guard, expected], tmp_path, monkeypatch)
    assert code == 2 and output.read_bytes() == b'prior report'


@pytest.mark.parametrize('section,flag', REPORT_OPTIONS)
@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('change', ['missing', 'extra', 'renamed'])
def test_report_inventory_mismatch_refuses_atomically_and_recovers(
        section, flag, stdin, change, tmp_path, monkeypatch, capsys):
    value = packet()
    identity = value[section][0]['id']
    options = [flag, identity]
    output = tmp_path / 'summary.json'
    output.write_bytes(b'prior report')
    changed = deepcopy(value)
    if change == 'missing':
        changed[section].clear()
    elif change == 'extra':
        extra = deepcopy(changed[section][0])
        extra['id'] += '_extra'
        changed[section].append(extra)
    else:
        changed[section][0]['id'] += '_renamed'
    assert checker.validate_packet(changed)['valid']

    with monkeypatch.context() as patch:
        def forbidden(*args, **kwargs):
            raise AssertionError('Mismatched report inventory must precede trusted checks')
        patch.setattr(checker, 'validate_packet', forbidden)
        patch.setattr(checker, 'validate_evidence', forbidden)
        code, output = run_packet(changed, stdin, options, tmp_path, patch)
        assert code == 2 and output.read_bytes() == b'prior report'
    code, output = run_packet(value, stdin, options, tmp_path, monkeypatch)
    assert code == 0 and json.loads(output.read_bytes()) == checker.validate_packet(value)
    captured = capsys.readouterr()
    assert 'expected inventory' in captured.err and captured.out == ''


@pytest.mark.parametrize('section,flag', REPORT_OPTIONS)
@pytest.mark.parametrize('bad_rows', [None, {}, [False]])
def test_invalid_report_container_refuses_before_trusted_checks(
        section, flag, bad_rows, tmp_path, monkeypatch):
    value = packet()
    identity = value[section][0]['id']
    value[section] = bad_rows
    output = tmp_path / 'summary.json'
    output.write_bytes(b'prior report')

    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid inventory shape must precede trusted checks')

    monkeypatch.setattr(checker, 'validate_packet', forbidden)
    code, output = run_packet(value, False, [flag, identity], tmp_path, monkeypatch)
    assert code == 2 and output.read_bytes() == b'prior report'
