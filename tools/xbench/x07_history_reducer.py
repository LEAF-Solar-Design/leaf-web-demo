"""Reduce synthetic X07 command histories through an injected offline oracle."""
import argparse
import copy
import hashlib
import json
from pathlib import Path


class HistoryOracleError(ValueError):
    def __init__(self, code, calls):
        super().__init__(code)
        self.diagnostic = {'schema': 'leaf.x07.reducer-diagnostic.v1', 'error': code, 'oracle_calls': calls}


def reduce_history(commands, oracle, *, max_calls=128):
    if not isinstance(commands, list) or len(commands) > 256:
        raise ValueError('history must be a list of at most256 synthetic commands')
    if type(max_calls) is not int or not 1 <= max_calls <= 4096:
        raise ValueError('oracle call budget must be between1 and4096')
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--max-calls', type=int, default=128)
    args = parser.parse_args()
    commands = [{'op': 'inspect'}, {'op': 'edit'}, {'op': 'synthetic_failure'}, {'op': 'reopen'}]
    try:
        if args.history:
            if args.history.stat().st_size > 262144:
                raise ValueError('history exceeds256KiB')
            commands = json.loads(args.history.read_text(encoding='utf-8'))
        result = reduce_history(commands, lambda rows: 'SYNTHETIC_FAILURE' if any(
            isinstance(row, dict) and row.get('op') == 'synthetic_failure' for row in rows) else None, max_calls=args.max_calls)
    except HistoryOracleError as error:
        print(json.dumps(error.diagnostic, sort_keys=True))
        return 2
    except (OSError, ValueError, RecursionError):
        print(json.dumps({'schema': 'leaf.x07.reducer-diagnostic.v1', 'error': 'INVALID_HISTORY_INPUT'}))
        return 2
    encoded = json.dumps(result, indent=2, allow_nan=False) + '\n'
    if args.out:
        if args.history and (args.out.resolve() == args.history.resolve()
                             or args.out.exists() and args.out.samefile(args.history)):
            parser.error('output aliases input history')
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__':
    raise SystemExit(main())
