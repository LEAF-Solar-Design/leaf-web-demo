# X04 saved review report checks

Studio X-bench X04 can check the internal consistency of saved comparison and batch reports. This step never reads an embedded artifact or dependency path. It does not run the artifact validator or recompute entity counts. Use the artifact validator separately when entity-derived proof is needed.

`scripts/validate_x04_review_report.py` accepts `leaf.x04.offline-comparison.v1` and `leaf.x04.offline-batch-validation.v1`. Its success summary declares `saved_report_internal_consistency`, `underlying_artifacts_revalidated: false`, `embedded_paths_opened: false` and original-byte integrity `unverified`.

Comparison reports require a drawing identity, nonnegative integer before/after counts and a signed integer delta equal to after minus before. Boolean counts refuse. Selected IDs, highlights and literal dependencies have unique, disjoint removed/added memberships. IDs are opaque nonempty strings. Dependencies require nonempty kind, name and disposition strings; optional reason/remapped_to fields are nonempty strings. Whole dependency rows determine identity, independent of object key order. Selection changes can coexist with zero count and highlight deltas. The checker cannot reconstruct before/after sets from a delta alone.

Batch reports require ordered contiguous indices from zero, exact total/passed/failed arithmetic and exact boolean valid fields. Success rows have the validation schema, drawing identity, nonnegative integer count, unique highlights and unverified integrity, without an error claim. Failure rows have a diagnostic of at most 131 characters and no successful validation claims. Repeated literal paths, empty path labels, all-failed reports and internally consistent empty reports are accepted. Paths remain inert labels. A saved success row is a claim, not fresh artifact validation.

Both report types require original-byte integrity `unverified`. Input is strict finite UTF-8 JSON with duplicate keys refused and bounded nesting. Unknown schemas refuse. The CLI shares bounded input, exact output byte budgets, input/output alias refusal and atomic file publication with the evidence tools. Exact `-` means stdin; `./-` is a literal filename. Default input limit is 16 MiB; output limit is optional. Exit 0 means internally consistent and exit 2 means refusal, with diagnostics on stderr and no success JSON. Existing output survives a validation or pre-publication failure. Unknown extension fields do not establish extra guarantees.

## Synthetic replay

Run from the repository root with Python. This constructs a valid saved comparison and an arithmetic forgery without opening any drawing or dependency.

```python
import json
from pathlib import Path
report = {
    'schema': 'leaf.x04.offline-comparison.v1', 'drawing_id': 'synthetic-review',
    'count': {'before': 7, 'after': 6, 'delta': -1},
    'selected_ids': {'removed': ['opaque-quad'], 'added': []},
    'highlight_handles': {'removed': ['B'], 'added': []},
    'literal_dependencies': {'removed': [], 'added': []},
    'original_bytes_integrity': 'unverified',
}
Path('x04-review-valid.json').write_text(json.dumps(report), encoding='utf-8')
report['count']['delta'] = 0
Path('x04-review-forged.json').write_text(json.dumps(report), encoding='utf-8')
```

```text
python -P scripts/validate_x04_review_report.py x04-review-valid.json --out x04-review-check.json
python -P scripts/validate_x04_review_report.py x04-review-forged.json --out x04-review-check.json
python -P scripts/bench_x04_review_report_validation.py
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_comparison_report_contract.py server/tests/test_x04_batch_report_contract.py server/tests/test_x04_review_report_cli_boundaries.py -q
```

The first command exits 0. The forgery exits 2 and preserves the first summary. Run verification on the assigned off-host rail, not the CPU-bound desktop. The benchmark reports time and peak memory for growing memberships and indexed rows, checks immutable inputs and arithmetic refusal, and sets no timing thresholds.

Internal consistency proves neither artifact validity, a simultaneous filesystem snapshot nor authenticated source bytes. Self-consistent forged reports can pass these checks. This offline step is separate from real drawing intake, rendered selection and the staging full-ten grader.
