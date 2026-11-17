"""Report inventory expectations precede trusted packet checks and publication."""
from copy import deepcopy
import io
import json
from types import SimpleNamespace

import pytest

from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


REPORT_OPTIONS = [
    ('comparisons', '--expected-comparison-id'),
    ('batches', '--expected-batch-id'),
]


def run_packet(value, stdin, options, tmp_path, monkeypatch):
    raw = json.dumps(value).encode()
    source = tmp_path / 'packet.json'
    source.write_bytes(raw)
    output = tmp_path / 'summary.json'
    if stdin:
        monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
    code = checker.main(['-' if stdin else str(source), '--out', str(output), *options])
    assert source.read_bytes() == raw
    assert not list(tmp_path.glob('.x04-*'))
    return code, output


@pytest.mark.parametrize('section,flag', REPORT_OPTIONS)
@pytest.mark.parametrize('kind', ['blank', 'space', 'path', 'nonascii', 'long',
                                 'duplicate', 'missing', 'abbreviation', 'prefix'])
def test_report_expectation_usage_precedes_io(section, flag, kind, tmp_path, monkeypatch, capsys):
    values = {'blank': '', 'space': ' ', 'path': '../x', 'nonascii': '\u00e9', 'long': 'a' * 65}
    if kind in values:
        options = [flag, values[kind]]
    elif kind == 'duplicate':
        options = [flag, 'same', flag, 'same']
    elif kind == 'missing':
        options = [flag]
    elif kind == 'abbreviation':
        options = [flag[:-1], 'a']
    else:
        options = [flag.removesuffix('-id'), 'a']

    def forbidden(*args, **kwargs):
        raise AssertionError('Argument refusal must precede I/O and validation')

    for name in ('check_output_path', 'read_json', 'validate_packet', 'validate_evidence', 'write_json'):
        monkeypatch.setattr(checker, name, forbidden)
    output = tmp_path / 'never.json'
    with pytest.raises(SystemExit) as caught:
        checker.main(['-', '--out', str(output), *options])
    assert caught.value.code == 2
    assert not output.exists()
    captured = capsys.readouterr()
    assert 'usage:' in captured.err and 'Traceback' not in captured.err and captured.out == ''


@pytest.mark.parametrize('section,flag', REPORT_OPTIONS)
@pytest.mark.parametrize('stdin', [False, True])
def test_exact_reordered_report_ids_and_all_guards(section, flag, stdin, tmp_path, monkeypatch):
    value = packet()
    extra = deepcopy(value[section][0])
    extra['id'] += '_second'
    value[section].append(extra)
    before = deepcopy(value)
    options = []
    for identity in reversed([row['id'] for row in value[section]]):
        options.extend([flag, identity])
    options.extend(['--expected-artifact-id', 'b', '--expected-artifact-id', 'a'])
    code, output = run_packet(value, stdin, options, tmp_path, monkeypatch)
    assert code == 0
    assert json.loads(output.read_bytes()) == checker.validate_packet(value)
    assert value == before


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


@pytest.mark.parametrize('section,flag', REPORT_OPTIONS)
@pytest.mark.parametrize('stdin', [False, True])
def test_matching_report_ids_retain_semantic_refusal(section, flag, stdin, tmp_path, monkeypatch):
    value = packet()
    identity = value[section][0]['id']
    if section == 'comparisons':
        value[section][0]['report']['count']['delta'] = 999
    else:
        value[section][0]['report']['passed'] = 999
    output = tmp_path / 'summary.json'
    output.write_bytes(b'prior report')
    calls = []
    trusted = checker.validate_evidence

    def observed(artifact):
        calls.append(artifact)
        return trusted(artifact)

    monkeypatch.setattr(checker, 'validate_evidence', observed)
    code, output = run_packet(value, stdin, [flag, identity], tmp_path, monkeypatch)
    assert code == 2 and len(calls) == 2 and output.read_bytes() == b'prior report'


@pytest.mark.parametrize('stdin', [False, True])
def test_omitted_report_expectations_preserve_default(stdin, tmp_path, monkeypatch):
    value = packet()
    before = deepcopy(value)
    expected = checker.validate_packet(value)
    code, output = run_packet(value, stdin, [], tmp_path, monkeypatch)
    assert code == 0 and json.loads(output.read_bytes()) == expected and value == before
