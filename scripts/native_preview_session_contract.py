"""Dormant I02 command syntax, never runtime authority or an effect dispatcher.

Trusted context must be assembled by a separately authorized session owner.
Validation does not prove inspector handles, atomic idempotency or app storage.
This module has no runtime entrypoint and performs no external I/O.
"""
import json
import math
import re
import uuid

SCHEMA = 'leaf.native-session-command.v1'
MAX_COMMAND = 16 * 1024
MAX_TEXT = 4096
OPERATIONS = frozenset(('inspect', 'tap', 'set_text', 'terminate', 'relaunch',
                        'screenshot', 'record_start', 'record_stop', 'close'))
CONTEXT_FIELDS = frozenset(('session_id', 'org_id', 'project_id', 'actor_binding_id',
                            'source', 'job_id', 'attempt', 'lease_owner',
                            'allocator_ticket', 'deadline', 'next_sequence', 'state'))
SOURCE_FIELDS = frozenset(('repo_key', 'source_request_id', 'source_version',
                           'source_commit', 'source_tree', 'archive_sha256'))
COMMAND_FIELDS = frozenset(('schema', 'session_id', 'org_id', 'project_id',
                            'actor_binding_id', 'request_id', 'sequence',
                            'source_commit', 'operation', 'arguments'))
SCOPE_FIELDS = ('session_id', 'org_id', 'project_id', 'actor_binding_id')


class ContractError(ValueError):
    def __init__(self):
        super().__init__('Invalid native session command.')


def _need(condition):
    if not condition:
        raise ContractError()


def _closed(value, fields):
    _need(type(value) is dict and set(value) == fields)


def _uuid(value):
    _need(type(value) is str and len(value) == 36 and str(uuid.UUID(value)) == value)


def _digest(value, length):
    _need(type(value) is str and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None)


def _positive(value):
    _need(type(value) is int and value > 0)


def _finite(value):
    _need(type(value) in (int, float) and math.isfinite(value))


def _pairs(items):
    value = {}
    for key, item in items:
        _need(key not in value)
        value[key] = item
    return value


def _constant(_value):
    raise ContractError()


def _context(context, now):
    _closed(context, CONTEXT_FIELDS)
    for key in (*SCOPE_FIELDS, 'job_id', 'lease_owner'):
        _uuid(context[key])
    source = context['source']
    _closed(source, SOURCE_FIELDS)
    for key in ('repo_key', 'source_request_id'):
        _uuid(source[key])
    for key in ('source_commit', 'source_tree'):
        _digest(source[key], 40)
    _digest(source['archive_sha256'], 64)
    for value in (source['source_version'], context['attempt'], context['next_sequence']):
        _positive(value)
    ticket = context['allocator_ticket']
    _need(type(ticket) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', ticket) is not None)
    _finite(context['deadline'])
    _finite(now)
    _need(type(context['state']) is str and context['state'] in ('active', 'closing'))


def _validate(raw, context, now):
    _context(context, now)
    _need(type(raw) is bytes and 0 < len(raw) <= MAX_COMMAND)
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    _closed(value, COMMAND_FIELDS)
    _need(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                     allow_nan=False).encode('utf-8') == raw)
    _need(type(value['schema']) is str and value['schema'] == SCHEMA)
    for key in SCOPE_FIELDS:
        _uuid(value[key])
        _need(value[key] == context[key])
    _uuid(value['request_id'])
    _positive(value['sequence'])
    _need(value['sequence'] == context['next_sequence'])
    _digest(value['source_commit'], 40)
    _need(value['source_commit'] == context['source']['source_commit'])
    operation = value['operation']
    _need(type(operation) is str and operation in OPERATIONS)
    _need(operation == 'close' or (context['state'] == 'active' and now < context['deadline']))
    arguments = value['arguments']
    fields = {'snapshot_id', 'element_id'} if operation in ('tap', 'set_text') else set()
    if operation == 'set_text':
        fields.add('value')
    _closed(arguments, fields)
    if operation in ('tap', 'set_text'):
        _uuid(arguments['snapshot_id'])
        _uuid(arguments['element_id'])
    if operation == 'set_text':
        text = arguments['value']
        _need(type(text) is str and '\0' not in text and len(text.encode('utf-8')) <= MAX_TEXT)
    return value


def validate_command(raw, context, *, now):
    """Return a fresh parsed command or one fixed, input-independent error.

    A valid result grants no capability; a future owner must authorize and
    serialize effects and resolve opaque inspector handles independently.
    """
    try:
        return _validate(raw, context, now)
    except Exception:
        pass
    # Raise outside the handler so a raw parse error cannot retain input text
    # in the public exception's context or message.
    raise ContractError() from None
