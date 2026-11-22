"""Synthetic command validation only, no simulator or session admission."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_preview_session_contract as contract

IDS = [f'{n:08x}-1111-4111-8111-111111111111' for n in range(1, 10)]


def context():
    return dict(session_id=IDS[0], org_id=IDS[1], project_id=IDS[2], actor_binding_id=IDS[3],
                source=dict(repo_key=IDS[4], source_request_id=IDS[5], source_version=2,
                            source_commit='a' * 40, source_tree='b' * 40, archive_sha256='c' * 64),
                job_id=IDS[6], attempt=1, lease_owner=IDS[7], allocator_ticket='preview-owned.1',
                deadline=120.0, next_sequence=3, state='active')


def command(operation='inspect'):
    arguments = {}
    if operation in ('tap', 'set_text'):
        arguments = dict(snapshot_id=IDS[4], element_id=IDS[5])
    if operation == 'set_text':
        arguments['value'] = 'Synthetic quote 1,25'
    return dict(schema='leaf.native-session-command.v1', session_id=IDS[0], org_id=IDS[1],
                project_id=IDS[2], actor_binding_id=IDS[3], request_id=IDS[8], sequence=3,
                source_commit='a' * 40, operation=operation, arguments=arguments)


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def rejected(value, trusted=None, now=100):
    with pytest.raises(contract.ContractError) as caught:
        contract.validate_command(value, context() if trusted is None else trusted, now=now)
    assert str(caught.value) == 'Invalid native session command.'
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize('operation', ['inspect', 'tap', 'set_text', 'terminate', 'relaunch',
                                      'screenshot', 'record_start', 'record_stop', 'close'])
def test_closed_commands_validate_without_changing_context(operation):
    trusted = context()
    original = copy.deepcopy(trusted)
    value = command(operation)
    result = contract.validate_command(wire(value), trusted, now=100)
    assert result == value
    result['arguments']['ignored'] = True
    assert trusted == original


@pytest.mark.parametrize('field', ['session_id', 'org_id', 'project_id', 'actor_binding_id', 'source_commit', 'sequence'])
def test_foreign_scope_source_and_out_of_order_commands_are_rejected(field):
    value = command()
    value[field] = 4 if field == 'sequence' else 'd' * 40 if field == 'source_commit' else IDS[8]
    rejected(wire(value))


@pytest.mark.parametrize('field', list(command()))
def test_missing_command_fields_are_rejected(field):
    value = command()
    del value[field]
    rejected(wire(value))


@pytest.mark.parametrize('change', [dict(sequence=True), dict(sequence=3.0), dict(sequence=0),
    dict(request_id='ABCDEFAB-1111-4111-8111-111111111111'), dict(request_id='not-an-id'),
    dict(schema='other'), dict(operation='shell'), dict(operation=['inspect']), dict(arguments=[]),
    dict(command='/usr/bin/anything'), dict(path='/private/data')])
def test_malformed_or_executable_command_shapes_are_rejected(change):
    value = command()
    value.update(change)
    rejected(wire(value))


@pytest.mark.parametrize('raw', [b'{}', b'null', b'[]', b'\xff', b'{', b'{"a":1,"a":2}',
    b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e999}', b' ' * 16385])
def test_invalid_raw_envelopes_are_rejected(raw):
    rejected(raw)


def test_noncanonical_duplicate_and_nonbyte_commands_are_rejected():
    raw = wire(command())
    rejected(raw + b'\n')
    rejected(raw.replace(b'"sequence":3', b'"sequence":3,"sequence":3'))
    rejected(raw.decode())
    rejected(bytearray(raw))


@pytest.mark.parametrize('operation', ['inspect', 'tap', 'set_text', 'terminate', 'relaunch',
                                      'screenshot', 'record_start', 'record_stop'])
@pytest.mark.parametrize('now,state', [(120, 'active'), (121, 'active'), (100, 'closing')])
def test_expired_or_closing_session_cannot_accept_app_effects(operation, now, state):
    trusted = context()
    trusted['state'] = state
    rejected(wire(command(operation)), trusted, now)


@pytest.mark.parametrize('state', ['active', 'closing'])
def test_expired_close_can_request_owned_teardown(state):
    trusted = context()
    trusted['state'] = state
    assert contract.validate_command(wire(command('close')), trusted, now=121)['operation'] == 'close'


@pytest.mark.parametrize('arguments', [dict(snapshot_id=IDS[4]), dict(snapshot_id=IDS[4], element_id='bad'),
    dict(snapshot_id=IDS[4], element_id=IDS[5], x=10), dict(snapshot_id=IDS[4], element_id=IDS[5], path='secret'),
    dict(snapshot_id=IDS[4], element_id=IDS[5], value='text')])
def test_tap_accepts_only_opaque_handle_shape(arguments):
    value = command('tap')
    value['arguments'] = arguments
    rejected(wire(value))


@pytest.mark.parametrize('text', ['\0', 'x' * 4097, 'é' * 2049, 123, None, True])
def test_text_limits_use_utf8_bytes_and_reject_nontext(text):
    value = command('set_text')
    value['arguments']['value'] = text
    rejected(wire(value))


def test_utf8_text_at_exact_bound_and_empty_text_are_valid():
    for text in ('é' * 2048, ''):
        value = command('set_text')
        value['arguments']['value'] = text
        assert contract.validate_command(wire(value), context(), now=100) == value


@pytest.mark.parametrize('field,value', [('attempt', True), ('attempt', 0), ('next_sequence', 3.0),
    ('deadline', float('nan')), ('deadline', float('inf')), ('deadline', True),
    ('deadline', 10 ** 1000), ('allocator_ticket', '../foreign'), ('allocator_ticket', ''),
    ('state', 'complete'), ('job_id', 'bad'), ('lease_owner', 'ABCDEFAB-1111-4111-8111-111111111111'), ('extra', 'private')])
def test_invalid_trusted_context_fails_closed(field, value):
    trusted = context()
    trusted[field] = value
    rejected(wire(command()), trusted)


@pytest.mark.parametrize('field', list(context()))
def test_missing_context_fields_fail_closed(field):
    trusted = context()
    del trusted[field]
    rejected(wire(command()), trusted)


@pytest.mark.parametrize('field,value', [('source_version', True), ('source_version', 0),
    ('source_commit', 'A' * 40), ('source_tree', 'b' * 39), ('archive_sha256', 'c' * 63),
    ('repo_key', 'bad'), ('source_request_id', 'bad'), ('extra', 'private')])
def test_invalid_source_context_fails_closed(field, value):
    trusted = context()
    trusted['source'][field] = value
    rejected(wire(command()), trusted)


@pytest.mark.parametrize('field', list(context()['source']))
def test_missing_source_context_cannot_be_filled_by_caller(field):
    trusted = context()
    del trusted['source'][field]
    rejected(wire(command()), trusted)


@pytest.mark.parametrize('raw', [b'private raw input', b'{"private upstream":', b'\xffprivate',
    b'{"value":"\\ud800"}'])
def test_public_error_drops_raw_parser_context(raw):
    rejected(raw)


@pytest.mark.parametrize('now', [True, float('nan'), float('inf'), '100', 10 ** 1000])
def test_invalid_clock_is_not_authority(now):
    rejected(wire(command()), now=now)
