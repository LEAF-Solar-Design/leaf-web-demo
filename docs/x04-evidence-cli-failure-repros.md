# X04 evidence CLI failure replay

Studio X-bench X04 track 2 uses these synthetic replay checks for its portable
evidence tools. Save the worked JSON in x04-offline-report-export-contract.md
as example.json in a scratch directory.

```sh
python scripts/export_x04_offline_evidence.py example.json --out evidence.json
python scripts/validate_x04_offline_evidence.py evidence.json
```

Success exits 0. Export with --out writes the complete file and emits empty
stdout. Validation without --out emits one JSON document with valid true.
Successful commands emit empty stderr.

Create malformed.json containing `{"synthetic":true,"synthetic":false}`.

```sh
python scripts/export_x04_offline_evidence.py malformed.json --out evidence.json
python scripts/validate_x04_offline_evidence.py malformed.json --out evidence.json
```

Both exit 2 with empty stdout and a refusal diagnostic on stderr. Existing
evidence.json bytes survive. NaN, Infinity and a second trailing JSON document
have the same refusal behavior.

Copy evidence.json to forged.json and change result.count to 999. Change an
observed dependency name or coverage_note in another copy while keeping the
embedded decoded pairs intact.

```sh
python scripts/validate_x04_offline_evidence.py forged.json --out evidence.json
```

Validation exits 2 with empty stdout and diagnostic stderr. Existing output
bytes survive. Dependency rows and coverage must equal the report recomputed
from drawing.dependency_pairs. Counts and highlights must equal selected
physical entities.

Run the focused exporter fault checks on an allocated verification host.

```sh
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_export_atomic_failures.py -q
```

The tests inject serialization, partial temporary-write and os.replace errors.
Each requires exit 2, diagnostic-only stderr, exact preservation of previous
output bytes and zero remaining .x04-* files. The benchmark command is
`python scripts/bench_x04_evidence_pipeline.py`. It asserts exact results for
100, 1000 and 10000 parents and records time and traced memory without thresholds.

Validation checks consistency with embedded synthetic inputs. Independent raw
DWG dependency completeness and original-byte authentication require their own
retained evidence. The replay does not establish staging browser acceptance.
