# X04 evidence consistency and source history

Studio X-bench X04's offline validator recomputes counts and highlighted
parents from the entities embedded in an artifact. It compares dependency
claims with the embedded decoded records. Recipients can edit those inputs
and recompute a consistent artifact. Validation establishes that consistency.
Original-byte integrity remains unverified.

Save the worked synthetic example from x04-offline-report-export-contract.md
as example.json and run on the allocated verification host.

```sh
python scripts/export_x04_offline_evidence.py example.json --out baseline.json
python scripts/validate_x04_offline_evidence.py baseline.json
python - <<'PY'
import json
from pathlib import Path
artifact = json.loads(Path('baseline.json').read_bytes())
artifact['drawing']['intake']['inserts'][0]['rows'] = 3
Path('edited-input.json').write_text(json.dumps(artifact), encoding='utf-8')
PY
python scripts/validate_x04_offline_evidence.py edited-input.json
```

The baseline validates with count 7, highlights A and B, and unverified byte
integrity. Editing the embedded INSERT from two rows to three gives nine
modules plus the one polygon. The retained count claim of 7 then contradicts
the entities, so validation exits 2 with empty stdout and a count diagnostic.

```sh
python scripts/export_x04_offline_evidence.py edited-input.json --out recomputed.json
python scripts/validate_x04_offline_evidence.py recomputed.json
```

Export recomputes count 10 with highlights A and B. The new artifact validates
with exit 0 and unverified original-byte integrity. The baseline remains a
separate file. A recipient can therefore change the model and produce new
internally consistent evidence. Independent retained source bytes, a trusted
digest and producer receipts are needed to establish source history. Supplied
opaque digest fields remain claims.

```sh
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_evidence_seeded_mutations.py server/tests/test_x04_evidence_temp_short_writes.py server/tests/test_x04_evidence_cleanup_failures.py server/tests/test_x04_evidence_absent_stdin.py server/tests/test_x04_evidence_ascii_locale.py -q
python scripts/bench_x04_evidence_float_tokens.py
```

The mutation regression exercises both the contradictory intermediate model
and its accepted recomputation. Runtime tests cover exact temporary writes,
cleanup failure reports, cancellation identity, unavailable stdin and ASCII
standard streams with Unicode paths and references. If unlink fails, the
primary refusal remains first and a diagnostic names the remaining temporary
file. Cancellation carries the same note and still propagates. After the
injected fault is removed, fixtures delete only their known leftover file and
verify successful retry.

The float benchmark grows fractional mantissas and exponent tokens under
explicit input ceilings. Standard binary floating-point rounding applies.
Finite results, including underflow to zero, are accepted in opaque fields.
Overflow to infinity is refused before publication and a valid control then
succeeds. Parsing, export and validation time and peak memory are observations
without thresholds. These checks leave raw DWG completeness and staging
browser acceptance to their source-bound evidence.
