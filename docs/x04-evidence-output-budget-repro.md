# X04 encoded output budgets

Studio X-bench X04's offline evidence CLIs accept --max-output-bytes as an
optional positive ceiling. It counts the complete UTF-8 JSON output, including
ASCII escaping, indentation and the final newline. Omitting the option keeps
output unlimited. The input-byte ceiling remains independent.

Use the synthetic worked example from x04-offline-report-export-contract.md
as example.json. Run these commands on the allocated Linux verification host
from a scratch directory, with REPO set to the checkout's absolute path.

```sh
python "$REPO/scripts/export_x04_offline_evidence.py" example.json --out baseline.json
export_bytes=$(python -c "from pathlib import Path; print(Path('baseline.json').stat().st_size)")
python "$REPO/scripts/export_x04_offline_evidence.py" example.json --max-output-bytes "$export_bytes" > stdout.json
cmp baseline.json stdout.json
python "$REPO/scripts/export_x04_offline_evidence.py" example.json --max-output-bytes "$((export_bytes - 1))" > refused.stdout
```

The exact limit exits 0 and produces the baseline bytes. The one-byte-under
limit exits 2 with an encoded-output diagnostic on stderr and zero stdout
bytes. For preserved-file checks, use the CLI's --out option.

```sh
cp baseline.json protected.json
python "$REPO/scripts/export_x04_offline_evidence.py" example.json --max-output-bytes "$((export_bytes - 1))" --out protected.json
cmp baseline.json protected.json
python "$REPO/scripts/export_x04_offline_evidence.py" example.json --max-output-bytes "$export_bytes" --out protected.json
cmp baseline.json protected.json
```

The failed command exits 2 and leaves protected.json byte-identical to its
baseline. Recovery exits 0 at the exact encoded size. Refusal happens before
stdout writes, temporary-file creation or output replacement.

Validation has its own output size because it emits a recomputed summary.

```sh
python "$REPO/scripts/validate_x04_offline_evidence.py" baseline.json --out validation-baseline.json
validation_bytes=$(python -c "from pathlib import Path; print(Path('validation-baseline.json').stat().st_size)")
python "$REPO/scripts/validate_x04_offline_evidence.py" baseline.json --max-output-bytes "$validation_bytes" > validation.stdout
cmp validation-baseline.json validation.stdout
python "$REPO/scripts/validate_x04_offline_evidence.py" baseline.json --max-output-bytes "$((validation_bytes - 1))" > validation-refused.stdout
cp validation-baseline.json validation-protected.json
python "$REPO/scripts/validate_x04_offline_evidence.py" baseline.json --max-output-bytes "$((validation_bytes - 1))" --out validation-protected.json
cmp validation-baseline.json validation-protected.json
python "$REPO/scripts/validate_x04_offline_evidence.py" baseline.json --max-output-bytes "$validation_bytes" --out validation-protected.json
```

The same exit and stream rules apply. Successful validation reports count 7,
canonical highlights A and B, and unverified original-byte integrity. A
positive partial stdout write is completed before flushing. Zero progress
returns exit 2 with a diagnostic and retires the failed stream.

```sh
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_evidence_output_budget.py server/tests/test_x04_evidence_short_writes.py server/tests/test_x04_evidence_input_size_races.py server/tests/test_x04_evidence_seeded_mutations.py -q
python scripts/bench_x04_evidence_structural_width.py
```

The tests cover exact byte limits for both destinations, partial writes,
stat/read growth and shrink races, and seeded mutations with independently
specified expected outcomes. Every rejection preserves prior output and
valid controls replay. The benchmark compares scalar arrays with equal-byte
long strings at fixed physical entities and dependency count. It records
bounded-input parsing, export and validation time and traced peak memory,
excluding fixture construction, with no thresholds. First export includes
tool loading. These checks establish synthetic consistency. Original bytes,
raw dependency completeness and staging browser acceptance retain their own
evidence requirements.
