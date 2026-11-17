"""X04 report inventory precedence controls using synthetic packets."""
from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
import pytest
from test_x04_packet_cross_binding import packet
from test_x04_report_inventory import REPORT_OPTIONS, run_packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('section,flag', REPORT_OPTIONS)
@pytest.mark.parametrize('stdin', [False, True])
@pytest.mark.parametrize('kind', ['budget', 'stale-utf8', 'stale-json', 'matching-json', 'inventory'])
def test_report_guard_refusal_order_and_retry(section, flag, stdin, kind, tmp_path, monkeypatch, capsys):
    value = packet()
    raw = json.dumps(value).encode()
    if kind == 'stale-utf8':
        raw += b'\xff'
    if kind in ('stale-json', 'matching-json'):
        raw = b'{broken'
    source = tmp_path / 'packet.json'
    source.write_bytes(raw)
    output = tmp_path / 'summary.json'
    output.write_bytes(b'prior report')
    pin = '0' * 64 if kind.startswith('stale') or kind == 'budget' else hashlib.sha256(raw).hexdigest()
    options = ['-' if stdin else str(source), '--out', str(output),
               '--expected-packet-sha256', pin, flag, 'wrong']
    if kind == 'budget':
        options += ['--max-input-bytes', str(len(raw) - 1)]

    def forbidden(*args, **kwargs):
        raise AssertionError('Earlier packet refusal must precede trusted checks')

    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_packet', forbidden)
        if stdin:
            patch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        assert checker.main(options) == 2
    expected = {'budget': 'byte', 'stale-utf8': 'expected_sha256',
                'stale-json': 'expected_sha256', 'matching-json': 'Expecting property',
                'inventory': 'expected inventory'}[kind]
    captured = capsys.readouterr()
    assert expected in captured.err and captured.out == '' and 'Traceback' not in captured.err
    assert output.read_bytes() == b'prior report' and source.read_bytes() == raw
    code, output = run_packet(value, stdin, [flag, value[section][0]['id']], tmp_path, monkeypatch)
    assert code == 0 and json.loads(output.read_bytes()) == checker.validate_packet(value)


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
