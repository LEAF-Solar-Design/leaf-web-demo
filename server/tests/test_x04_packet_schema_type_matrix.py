"""Packet section/payload/ID types refuse without interpreting target paths."""
from copy import deepcopy
from pathlib import Path
import pytest
import tool_loader
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


def checked_refusal(value, monkeypatch):
    before = deepcopy(value)
    real_open = Path.open
    def guarded(path, *args, **kwargs):
        assert path.resolve() == tool_loader.PLATFORM_PACKAGE_REGISTRY.resolve(), 'Unexpected target read'
        return real_open(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', guarded)
        with pytest.raises((ValueError, TypeError)):
            checker.validate_packet(value)
    assert value == before
    assert checker.validate_packet(packet())['valid']


@pytest.mark.parametrize('section', ['artifacts', 'comparisons', 'batches'])
@pytest.mark.parametrize('bad', [None, {}, True, 'rows'])
def test_section_containers(section, bad, monkeypatch):
    value = packet(); value[section] = bad
    checked_refusal(value, monkeypatch)


@pytest.mark.parametrize('section,field', [('artifacts', 'artifact'), ('comparisons', 'report'), ('batches', 'report')])
@pytest.mark.parametrize('bad', [None, [], True])
def test_embedded_payload_types(section, field, bad, monkeypatch):
    value = packet(); value[section][0][field] = bad
    checked_refusal(value, monkeypatch)


@pytest.mark.parametrize('where', ['identity', 'operand', 'occurrence'])
@pytest.mark.parametrize('bad', [None, False, 1, [], {}, '', 'a' * 65, '1abc'])
def test_local_id_types(where, bad, monkeypatch):
    value = packet()
    if where == 'identity': value['artifacts'][0]['id'] = bad
    elif where == 'operand': value['comparisons'][0]['before'] = bad
    else: value['batches'][0]['artifact_ids'][0] = bad
    checked_refusal(value, monkeypatch)


@pytest.mark.parametrize('section,other', [('comparisons', 'batches'), ('batches', 'comparisons')])
def test_swapped_report_schemas(section, other, monkeypatch):
    value = packet(); value[section][0]['report'] = deepcopy(value[other][0]['report'])
    checked_refusal(value, monkeypatch)


def test_maximum_label_length_and_retry(monkeypatch):
    value = packet(); identity = 'A' + 'a' * 63
    value['artifacts'][0]['id'] = identity
    value['comparisons'][0]['before'] = identity
    value['batches'][0]['artifact_ids'] = [identity, 'b', identity]
    assert checker.validate_packet(value)['valid']
    value['artifacts'][0]['id'] += 'x'
    checked_refusal(value, monkeypatch)
