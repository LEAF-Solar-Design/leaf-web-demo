"""X04 report inventory arguments controls using synthetic packets."""
from copy import deepcopy
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
from test_x04_report_inventory import REPORT_OPTIONS, run_packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('section,flag', REPORT_OPTIONS)
def test_each_report_expectation_allows_unconstrained_other_section(section, flag, tmp_path, monkeypatch):
    value = packet()
    other = 'batches' if section == 'comparisons' else 'comparisons'
    value[other][0]['id'] += '_changed'
    code, output = run_packet(value, False, [flag, value[section][0]['id']], tmp_path, monkeypatch)
    assert code == 0 and json.loads(output.read_bytes()) == checker.validate_packet(value)


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


@pytest.mark.parametrize('stdin', [False, True])
def test_omitted_report_expectations_preserve_default(stdin, tmp_path, monkeypatch):
    value = packet()
    before = deepcopy(value)
    expected = checker.validate_packet(value)
    code, output = run_packet(value, stdin, [], tmp_path, monkeypatch)
    assert code == 0 and json.loads(output.read_bytes()) == expected and value == before
