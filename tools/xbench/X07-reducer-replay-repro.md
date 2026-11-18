# X07 offline reducer replay

This recipe belongs to Studio X-bench X07's synthetic profile recovery checks.
Run from the repository root with Python. It reads no customer drawing or store.
These results establish offline reducer behavior, not LIVE acceptance.

```powershell
@'
import json
import sys
sys.path.insert(0, 'tools/xbench')
from x07_history_reducer import HistoryOracleError, reduce_history

def ordered(rows):
    return 'ORDERED' if any(value == 'open' and 'commit' in rows[index+1:]
                            for index, value in enumerate(rows)) else None

history = ['open', 'noise', 'commit']
complete = reduce_history(history, ordered)
assert complete['complete'] and not complete['budget_exhausted']
assert complete['commands'] == ['open', 'commit']
for index in range(len(complete['commands'])):
    assert ordered(complete['commands'][:index] + complete['commands'][index+1:]) != complete['signature']
exhausted = reduce_history(history, ordered, max_calls=1)
assert not exhausted['complete'] and exhausted['budget_exhausted']
assert exhausted['commands'] == history and exhausted['oracle_calls'] == 1

def broken(rows):
    raise RuntimeError('synthetic private payload')

def changing(rows):
    changing.calls += 1
    return 'FIRST' if changing.calls == 1 else 'CHANGED'
changing.calls = 0
diagnostics = []
for oracle, code in [(broken, 'ORACLE_EXCEPTION'), (changing, 'ORACLE_SIGNATURE_CHANGED')]:
    try:
        reduce_history([], oracle)
    except HistoryOracleError as error:
        assert error.diagnostic['error'] == code
        assert 'private' not in json.dumps(error.diagnostic)
        diagnostics.append(error.diagnostic)
    else:
        raise AssertionError('expected explicit diagnostic')
assert history == ['open', 'noise', 'commit']
print(json.dumps({'complete': complete, 'exhausted': exhausted, 'diagnostics': diagnostics}, sort_keys=True))
'@ | python -
```

Both replay objects use `leaf.x07.synthetic-history-replay.v1` and
`proof_mode=synthetic_offline`. The completed result has signature `ORDERED`,
two retained commands and original_count 3. The exhausted result retains all
three commands and makes no minimality claim. Every oracle invocation,
including final signature validation, counts toward oracle_calls.

Diagnostics use `leaf.x07.reducer-diagnostic.v1`. The exception makes one call;
the changing oracle makes two calls on the same empty candidate. Neither returns
a successful reduction or exposes exception text. The recipe assumes each
injected oracle call terminates. No hard wall-clock bound is claimed.

Completion means no single retained command can be deleted while preserving
the original signature, under a deterministic oracle. The loop above checks
that property independently. Budget exhaustion is distinct from completion.
Replay commands preserve their order and JSON numeric representation.
