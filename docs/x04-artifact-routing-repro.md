# Offline artifact routing

Parent: Studio X-bench X04 intake, track 2. Evidence JSON carries synthetic
entities, dependency records, a selected-set request and recomputed result claims.
Comparison and batch reports summarize evidence they validate. Their schemas have
different roles in the four-tool replay.

Use [the four-tool fixture recipe](x04-four-tool-offline-walk.md) to create
request.json and after.json, then run from the worktree root:

```powershell
python scripts/export_x04_offline_evidence.py request.json --out evidence.json
python scripts/validate_x04_offline_evidence.py evidence.json --out validated.json
python scripts/compare_x04_offline_evidence.py evidence.json after.json --out comparison.json
python scripts/batch_validate_x04_evidence.py evidence.json after.json --out batch.json
```

All four exit 0. Evidence has count 7 and highlights A/B. The after artifact has
count 6 and highlight A. Comparison reports delta -1 and removed B. Batch totals
are 2 passed and 0 failed. Source-byte integrity stays unverified.

| Artifact schema | Consumer role |
| --- | --- |
| leaf.x04.offline-evidence.v1 | Export recomputes claims, validation checks claims, comparison validates each side, batch validates each explicit occurrence |
| leaf.x04.offline-validation.v1 | Validation summary for inspection |
| leaf.x04.offline-comparison.v1 | Delta report for inspection |
| leaf.x04.offline-batch-validation.v1 | Ordered validation report for inspection |

Exercise wrong-schema routing and preserve an existing output on refusal:

```powershell
python -c "from pathlib import Path; Path('sentinel.json').write_bytes(b'previous')"
python scripts/export_x04_offline_evidence.py comparison.json --out sentinel.json
python scripts/validate_x04_offline_evidence.py batch.json --out sentinel.json
python scripts/compare_x04_offline_evidence.py comparison.json evidence.json --out sentinel.json
python -c "from pathlib import Path; assert Path('sentinel.json').read_bytes() == b'previous'"
```

Each tool exits 2 with a schema diagnostic and preserves the sentinel. Batch
validation instead returns a complete report with one failed occurrence:

```powershell
python scripts/batch_validate_x04_evidence.py evidence.json comparison.json evidence.json --out routed.json
```

Expect exit 1, totals 3/2/1 and indexed validity true/false/true. A tiny output
budget refuses publication with exit 2 and preserves the old sentinel:

```powershell
python scripts/batch_validate_x04_evidence.py evidence.json batch.json evidence.json --out sentinel.json --max-output-bytes 1
python -c "from pathlib import Path; assert Path('sentinel.json').read_bytes() == b'previous'"
python scripts/batch_validate_x04_evidence.py evidence.json after.json --out sentinel.json
```

The final retry exits 0 and writes totals 2/2/0. Dependency targets remain unopened.
Both optimization modes use the same runtime validation. Concurrent writers use
unique temporary files and atomic replacement. The last successful replacement
determines the complete report at a shared destination. Original drawing-byte
identity and live staging acceptance still require their own source-bound proof.
