"""Reduce synthetic X07 command histories through an injected offline oracle."""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile


MAX_HISTORY_BYTES = 262144
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10000


def validate_json(value, *, max_depth=MAX_JSON_DEPTH, max_nodes=MAX_JSON_NODES):
    """Count values including root at depth zero; object keys are not nodes."""
    if (type(max_depth) is not int or not 0 <= max_depth <= MAX_JSON_DEPTH
            or type(max_nodes) is not int or not 1 <= max_nodes <= MAX_JSON_NODES):
        raise ValueError('invalid JSON budgets')
    stack, active, nodes = [(value, 0, False)], set(), 0
    while stack:
        item, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValueError('history JSON budget exceeded')
        if type(item) in (list, dict):
            if id(item) in active:
                raise ValueError('history JSON cycle')
            if type(item) is dict and any(type(key) is not str for key in item):
                raise ValueError('history JSON object keys must be strings')
            if type(item) is dict:
                for key in item:
                    key.encode('utf-8')
            if len(item) > max_nodes - nodes:
                raise ValueError('history JSON budget exceeded')
            active.add(id(item))
            stack.append((item, depth, True))
            children = item.values() if type(item) is dict else item
            stack.extend((child, depth + 1, False) for child in children)
        elif type(item) not in (str, int, float, bool, type(None)) or type(item) is float and not math.isfinite(item):
            raise ValueError('history must be finite JSON')
        elif type(item) is str:
            item.encode('utf-8')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def read_history(path):
    # Reject named pipes/devices before open, then verify the opened descriptor.
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('history must be a regular file')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_HISTORY_BYTES:
            raise ValueError('history must be a bounded regular file')
        def fingerprint(value):
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        initial = fingerprint(metadata)
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_HISTORY_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_HISTORY_BYTES:
                raise ValueError('history exceeds byte budget')
        current = path.lstat()
        if (size != metadata.st_size or not stat.S_ISREG(current.st_mode)
                or fingerprint(os.fstat(descriptor)) != initial or fingerprint(current) != initial):
            raise ValueError('history changed during read')
    finally:
        os.close(descriptor)
    parsed = json.loads(b''.join(chunks).decode('utf-8'), object_pairs_hook=unique_object)
    validate_json(parsed)
    return parsed


def atomic_publish(path, encoded):
    descriptor, name = tempfile.mkstemp(prefix='.x07-reducer-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_stdout(encoded):
    try:
        print(encoded, end='')
        sys.stdout.flush()
        return 0
    except OSError:
        with open(os.devnull, 'w') as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 2


def diagnostic(code):
    publish_stdout(json.dumps({'schema': 'leaf.x07.reducer-diagnostic.v1', 'error': code}) + '\n')
    return 2


class ReducerParser(argparse.ArgumentParser):
    def error(self, message):
        diagnostic('INVALID_ARGUMENTS')
        raise SystemExit(2)


class HistoryOracleError(ValueError):
    def __init__(self, code, calls):
        super().__init__(code)
        self.diagnostic = {'schema': 'leaf.x07.reducer-diagnostic.v1', 'error': code, 'oracle_calls': calls}


def reduce_history(commands, oracle, *, max_calls=128, max_depth=MAX_JSON_DEPTH, max_nodes=MAX_JSON_NODES):
    if not isinstance(commands, list) or len(commands) > 256:
        raise ValueError('history must be a list of at most256 synthetic commands')
    if type(max_calls) is not int or not 1 <= max_calls <= 4096:
        raise ValueError('oracle call budget must be between1 and4096')
    validate_json(commands, max_depth=max_depth, max_nodes=max_nodes)
    try:
        encoded = json.dumps(commands, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError('history must be finite JSON') from error
    if len(encoded.encode('utf-8')) > 262144:
        raise ValueError('history exceeds256KiB')
    current = json.loads(encoded)
    calls = 0
    observed = {}

    def evaluate(candidate):
        nonlocal calls
        calls += 1
        key = hashlib.sha256(json.dumps(candidate, sort_keys=True, separators=(',', ':')).encode()).digest()
        try:
            signature = oracle(copy.deepcopy(candidate))
        except Exception:
            raise HistoryOracleError('ORACLE_EXCEPTION', calls) from None
        if signature is not None and (not isinstance(signature, str) or not signature or len(signature) > 200):
            raise HistoryOracleError('INVALID_ORACLE_SIGNATURE', calls)
        if key in observed and observed[key] != signature:
            raise HistoryOracleError('ORACLE_SIGNATURE_CHANGED', calls)
        observed[key] = signature
        return signature

    signature = evaluate(current)
    complete = signature is None
    granularity = 2
    while signature is not None and calls < max_calls:
        if not current:
            complete = True
            break
        width = max(1, (len(current) + granularity - 1) // granularity)
        reduced = False
        for start in range(0, len(current), width):
            if calls == max_calls:
                break
            candidate = current[:start] + current[start + width:]
            if evaluate(candidate) == signature:
                current = candidate
                granularity = max(2, granularity - 1)
                reduced = True
                break
        if not reduced:
            if calls == max_calls:
                break
            if granularity >= len(current):
                complete = True
                break
            granularity = min(len(current), granularity * 2)
    if signature is not None and not current:
        complete = True
    if complete:
        if calls == max_calls:
            complete = False
        elif evaluate(current) != signature:
            raise HistoryOracleError('ORACLE_SIGNATURE_CHANGED', calls)
    return {'schema': 'leaf.x07.synthetic-history-replay.v1', 'proof_mode': 'synthetic_offline',
            'signature': signature, 'commands': current, 'original_count': len(commands),
            'oracle_calls': calls, 'complete': complete,
            'budget_exhausted': not complete and calls == max_calls}


def main():
    parser = ReducerParser(description=__doc__)
    parser.add_argument('--history', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--max-calls', type=int, default=128)
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 4096:
        parser.error('invalid call budget')
    commands = [{'op': 'inspect'}, {'op': 'edit'}, {'op': 'synthetic_failure'}, {'op': 'reopen'}]
    try:
        if args.history:
            commands = read_history(args.history)
        result = reduce_history(commands, lambda rows: 'SYNTHETIC_FAILURE' if any(
            isinstance(row, dict) and row.get('op') == 'synthetic_failure' for row in rows) else None, max_calls=args.max_calls)
    except HistoryOracleError as error:
        publish_stdout(json.dumps(error.diagnostic, sort_keys=True) + '\n')
        return 2
    except (OSError, ValueError, RecursionError):
        return diagnostic('INVALID_HISTORY_INPUT')
    encoded = json.dumps(result, indent=2, allow_nan=False) + '\n'
    try:
        if args.out:
            if args.history and (args.out.resolve() == args.history.resolve()
                                 or args.out.exists() and args.out.samefile(args.history)):
                raise ValueError('output aliases input history')
            atomic_publish(args.out, encoded)
    except (OSError, ValueError):
        return diagnostic('OUTPUT_PUBLICATION_FAILED')
    return publish_stdout(encoded)


if __name__ == '__main__':
    raise SystemExit(main())
