"""Explicit, inert configuration adapter for the managed native preview host."""
import json
import ntpath
import re
import sys

import native_preview_host as host
import native_preview_store as store

MAX_FILE_BYTES = 16384
ERROR_CODES = frozenset({'invalid-config', 'read-failed', 'entry-failed',
                         'host-failed', 'invalid-result'})
_FIELDS = frozenset({'schema', 'scope', 'credential_store_path', 'expected_client_id',
                     'audience', 'output_root', 'poll_seconds', 'error_seconds'})
_EXIT = {'stopped': 0, 'busy': 3, 'reconciliation-required': 4}
_USAGE = 'usage: native_preview_entry.py --config <absolute-local-Windows-file>\n'


class EntryError(RuntimeError):
    def __init__(self, code):
        super().__init__(code if type(code) is str and code in ERROR_CODES else 'entry-failed')


def _check(condition):
    if not condition:
        raise EntryError('invalid-config')


def _local_path(value, *, directory=False):
    if type(value) is not str or not value or any(ord(c) < 32 for c in value):
        return False
    drive, tail = ntpath.splitdrive(value)
    if re.fullmatch(r'[A-Za-z]:', drive) is None or not tail.startswith(('\\', '/')):
        return False
    if any(c in tail for c in ':<>"|?*'):
        return False
    if directory and tail in ('\\', '/'):
        return True
    local = value.rstrip('\\/') if directory else value
    return store._local_path(local) and all(
        part not in ('.', '..') and not part.endswith((' ', '.'))
        for part in re.split(r'[\\/]', tail) if part)


def _validate(config):
    valid = False
    try:
        _check(type(config) is dict and set(config) == _FIELDS)
        _check(type(config['schema']) is str and config['schema'] == 'leaf.native-preview-host.v1')
        host.service.worker.binding(config['scope'])
        _check(_local_path(config['credential_store_path']))
        _check(_local_path(config['output_root'], directory=True))
        _check(store._credential(config['expected_client_id']))
        _check(type(config['audience']) is str and config['audience'] in host.session.AUDIENCES)
        host.service._wait_setting(config['poll_seconds'])
        host.service._wait_setting(config['error_seconds'])
        _check(len(json.dumps(config, ensure_ascii=False, allow_nan=False).encode('utf-8'))
               <= MAX_FILE_BYTES)
        valid = True
    except Exception:
        pass
    if not valid:
        raise EntryError('invalid-config')


def _pairs(items):
    value = {}
    for key, item in items:
        _check(key not in value)
        value[key] = item
    return value


def load_config(path):
    """Read bounded UTF-8 JSON from one explicit local Windows file path."""
    code = 'invalid-config'
    result = None
    try:
        _check(_local_path(path))
        code = 'read-failed'
        with open(path, 'rb') as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        code = 'invalid-config'
        _check(type(raw) is bytes and 1 <= len(raw) <= MAX_FILE_BYTES)
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: _check(False))
        _validate(value)
        result = value
    except Exception:
        pass
    if result is None:
        raise EntryError(code)
    return result


def run_config(config):
    """Validate before construction, then delegate once without a preflight."""
    _validate(config)
    code = 'entry-failed'
    result = None
    try:
        resolver = store.make_credential_resolver(config['credential_store_path'],
                                                  config['expected_client_id'], config['audience'])
        code = 'host-failed'
        value = host.run_host(config['scope'], resolver, config['audience'], config['output_root'],
                              poll_seconds=config['poll_seconds'], error_seconds=config['error_seconds'])
        code = 'invalid-result'
        _check(type(value) is dict and set(value) == {'state', 'completed'})
        _check(type(value['state']) is str and value['state'] in _EXIT)
        _check(type(value['completed']) is int and value['completed'] >= 0)
        result = {'state': value['state'], 'completed': value['completed']}
    except Exception:
        pass
    if result is None:
        raise EntryError(code)
    return result


def main(argv=None):
    """Emit only fixed codes; application arguments are never echoed."""
    result = {'state': 'invalid-config'}
    status = 2
    try:
        args = sys.argv[1:] if argv is None else argv
        if type(args) is list and args in (['--help'], ['-h']):
            sys.stdout.write(_USAGE)
            return 0
        if (type(args) is list and len(args) == 2 and type(args[0]) is str
                and args[0] == '--config' and _local_path(args[1])):
            result = run_config(load_config(args[1]))
            status = _EXIT[result['state']]
    except EntryError as error:
        code = (error.args[0] if type(error) is EntryError and len(error.args) == 1
                and type(error.args[0]) is str else 'entry-failed')
        result = {'state': code if code in ERROR_CODES else 'entry-failed'}
        status = 2 if result['state'] in ('invalid-config', 'read-failed') else 1
    except Exception:
        result, status = {'state': 'entry-failed'}, 1
    sys.stdout.write(json.dumps(result, separators=(',', ':')) + '\n')
    return status


if __name__ == '__main__':
    raise SystemExit(main())
