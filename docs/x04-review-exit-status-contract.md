# Review CLI exit status contract

Parent: Studio X-bench X04 raw drawing intake, track 2. These offline commands
use synthetic files and leave original-byte integrity unverified.

Create before.json and after.json with the Python recipe in
[the delta review](x04-evidence-delta-review.md). Both independently validate.
Create a malformed input, then run an all-valid batch:

```powershell
python -c "from pathlib import Path; Path('broken.json').write_bytes(b'{broken')"
python scripts/batch_validate_x04_evidence.py before.json after.json --out batch.json
```

Expect exit 0 and a complete report with total 2, passed 2, failed 0. Now include
one refused input and repeat the valid path after it:

```powershell
python scripts/batch_validate_x04_evidence.py before.json broken.json before.json --out batch.json
```

Expect exit 1 and a new complete report with indexed results 0, 1, 2 and totals
3, 2, 1. The middle result has valid=false and an error. The later valid result
is still present. Exit 1 describes validation results, so it publishes its full
failure report. It does not promise that an older destination is retained.

Compare the valid artifacts, then force publication refusal:

```powershell
python scripts/compare_x04_offline_evidence.py before.json after.json --out delta.json
python scripts/compare_x04_offline_evidence.py before.json after.json --out delta.json --max-output-bytes 1
python scripts/batch_validate_x04_evidence.py before.json after.json --out batch.json --max-output-bytes 1
python scripts/batch_validate_x04_evidence.py - --out batch.json
```

The first command exits 0, count delta -1 and removed highlight B. The other
three exit 2 with diagnostic-only stderr, no success JSON and unchanged existing
destination bytes. Batch stdin is forbidden because inputs must be explicit
paths. Use the comparison CLI's single-stdin convention when needed. An input
ceiling is per occurrence, so an oversized batch path becomes one failed row,
later entries recover, and the complete batch report exits 1. Aggregate input
bytes do not reduce another path's allowance.

Argument usage errors also exit 2 before publication. Handled temporary-write,
flush or replace failures exit 2 and preserve prior file output. Partial writes
are completed when they report forward progress. A failed cleanup names the
actual remaining temporary file after the primary refusal. It does not claim
that cleanup succeeded. Stdout cannot roll back bytes already emitted after an
I/O failure. Use --out for atomic destination publication. Cancellation keeps
its existing propagated semantics and is outside the handled exit 2 contract.

Retry valid commands after a refusal:

```powershell
python scripts/batch_validate_x04_evidence.py before.json after.json --out batch.json
python scripts/compare_x04_offline_evidence.py before.json after.json --out delta.json
```

Both exit 0. Invalid comparison claims on either side refuse, including two
identically forged artifacts. Dependency targets remain unopened. Whole-row
dependency changes are compared after trusted validation. A same-kind font
remap or disposition that contradicts the embedded records refuses validation.
Valid producer kind transitions retain their literal names and show their full
old and new rows, including disposition and remap fields.

The membership benchmark runs the comparison's membership helper directly over
equal, disjoint and asymmetric synthetic highlight and dependency lists at
1k, 10k and 100k sizes. It checks independent index-set differences and unchanged
inputs, and records time and peak memory without thresholds. It does not measure
artifact parsing or trusted recomputation. Run
`python scripts/bench_x04_comparison_membership.py`.

An internally valid synthetic replay is not source authentication. Matching
drawing IDs do not establish unchanged content. Original bytes and a trusted
independent SHA-256 history are required for byte identity. This slice provides
no raw customer, browser, staging or real ten-drawing acceptance proof.
