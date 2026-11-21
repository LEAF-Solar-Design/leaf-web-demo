# X06 offline evidence checks

Parent: Studio X-bench X06, a steel takeoff authored through normal Studio controls.

These checks use synthetic values. They need no staging service, drawing file,
credential, browser, or network connection.

From the repository root, run:

```powershell
python -P -m pytest tools/xbench/tests/test_x06.py -q -p no:cacheprovider -p no:xonsh -o pythonpath=tools/xbench
```

`-P` keeps the repository's `platform/` package from shadowing Python's standard
library at startup. `-p no:xonsh` excludes the host's unrelated console plugin.

## Synthetic examples

Start `python -P` from the repository root and run:

```python
import base64
import sys
sys.path.insert(0, 'tools/xbench')
from benches.x06 import schedule_verdict, spreadsheet_verdict, reuse_verdict

rows = [{'layer': 'steel_test', 'count': 2, 'total_length': 100}]
oracle = {'sha256': 'a' * 64, 'rows': rows}
table = {'drawing_sha256': 'a' * 64, 'length_units': 'inches', 'rows': rows}
assert schedule_verdict(table, oracle) == 'PASS'
assert schedule_verdict({**table, 'length_units': None}, oracle) == 'UNREADABLE'
assert schedule_verdict({**table, 'drawing_sha256': 'b' * 64}, oracle) == 'FAIL'

cells = [['Layer', 'Count', 'Total length'], ['steel_test', '2', '100.000']]
content = b'Layer,Count,Total length\nsteel_test,2,100.000\n'
artifact = {'bytes': len(content), 'mime': 'text/csv',
            'base64': base64.b64encode(content).decode('ascii')}
assert spreadsheet_verdict(artifact, cells) == 'PASS'
assert spreadsheet_verdict({**artifact, 'base64': '!!!'}, cells) == 'UNREADABLE'

first = {'published_tool_id': 'xbench-x06-synthetic', 'session_id': 'session-a'}
second = {**first, 'session_id': 'session-b', 'drawing_sha256': 'b' * 64}
fixture = {'id': 'second', 'sha256': 'b' * 64}
assert reuse_verdict(first, second, fixture) == 'PASS'
assert reuse_verdict(first, {**second, 'session_id': 'session-a'}, fixture) == 'FAIL'
```

These fields form the offline helpers' input contract. A future collector must
derive them from product evidence. The helpers do not claim that the product
already emits this shape. Reuse checks only the saved tool, session, and drawing
binding. Compare the second drawing's rows separately with its frozen oracle.

## Verdicts

- `UNREADABLE`: evidence is missing, malformed, unsupported, or corrupt.
- `FAIL`: readable evidence proves the wrong drawing, units, rows, cells, tool,
  or session. The collector also reports the known protected-authoring refusal
  only for integer HTTP503, `tenant_repository_unavailable`, and `BAD_PARAMS`.
- A helper's `PASS` proves only its local check. It cannot pass the whole bench.
  The current first-failure collector has no whole-bench PASS path.

Counts and layer sets match exactly. Lengths retain the frozen 0.5 percent
tolerance. CSV display cells match exactly, including row and column order.
The CSV helper accepts `text/csv`, checks bytes and strict base64, and caps input
at 8 MiB. Missing or oversized artifacts remain unreadable.

The `_run` tests replace `signed_in` and every page call. They inject response
decoding errors, capture errors, sign-in failure, and UI timeouts. Temporary JSON
evidence must survive each failure, and the fake session must close. A readable
HTTP refusal stays FAIL if a later capture fails. No real browser starts.

## Evidence still needed for the frozen steps

Additional pure helpers use the same collector-normalized contract:

- `intake_verdict` binds `project_id`, `created_project_id`, and
  `import_project_id`. It requires the fresh-project and normal-control flags,
  frozen request, first drawing hash, no user code, and at most two clarifications.
- `publication_verdict` checks `staged`, `reviewed`, `published`, and `run`
  records. Each binds the same change set and actual source digest. The published
  tool must match the completed first run. Pending evidence stays unreadable.
- `second_run_verdict` adds second-drawing oracle comparison to reuse identity.
- `provenance_verdict` binds source bytes and `engine_run_id` to the published
  tool and run. Missing bindings are unreadable; foreign bindings or solar markers fail.
- `event_metrics` reads ordered `start`, `action`, `note`, and `useful_result`
  events with numeric `at_s` values. An action needs a Boolean `deliberate` field.
  The window stops at the first useful result. Later events do not affect it.

`assemble_offline_evidence(bundle, drawings, recorded_grade=...)` returns the
separate schema `leaf.xbench.offline-evidence.v1`. It retains each missing or
failed step, checks reuse against the publication chain, and never upgrades a
recorded failure through passing synthetic helpers. It leaves its inputs intact
and writes no live grade or readiness marker. `_run` does not use this assembler.

| Step | Missing end-to-end evidence |
|---|---|
| 1 | One fresh-project import and normal request-entry receipt with clarification count |
| 2 | Successful authoring, normal review, publication, and first-drawing run |
| 3 | The actual table, drawing identity, and declared units compared with the oracle |
| 4 | The actual normal download and exact displayed table cells |
| 5 | The saved published tool in a distinct session, second drawing, and correct rows |
| 6 | Authored source plus the run's engine provenance |
| 7 | Start-to-useful-result timing and deliberate-action count |

Offline checks preserve the recorded live failure. They do not replace these
receipts or change the frozen benchmark requirements.
