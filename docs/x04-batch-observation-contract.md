# Batch observation contract

Parent: Studio X-bench X04 raw drawing intake, track 2. A batch report validates
each explicit path occurrence in order. It rereads the file for each occurrence.
It does not cache one result per path or establish one simultaneous filesystem
snapshot. Every synthetic result keeps original-byte integrity unverified.

Use the worked artifact in
[the portable contract](x04-offline-report-export-contract.md). This controlled
offline hook replaces one path between occurrences, then retries unchanged bytes:

```python
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
sys.path.insert(0, 'scripts')
import batch_validate_x04_evidence as batch

text = Path('docs/x04-offline-report-export-contract.md').read_text(encoding='utf-8')
valid = json.loads(text.split('```json\n', 1)[1].split('```', 1)[0])
with TemporaryDirectory() as directory:
    source = Path(directory) / 'input.json'
    source.write_text(json.dumps(valid), encoding='utf-8')
    original_read = batch.read_json
    observed = []
    def changing_read(path, limit):
        source.write_text('{broken' if len(observed) == 1 else json.dumps(valid), encoding='utf-8')
        observed.append(str(path))
        return original_read(path, limit)
    with patch.object(batch, 'read_json', changing_read):
        report = batch.validate_paths([source, source, source])
    assert [row['valid'] for row in report['results']] == [True, False, True]
    assert (report['total'], report['passed'], report['failed']) == (3, 2, 1)
    assert [row['index'] for row in report['results']] == [0, 1, 2]
    assert batch.validate_paths([source, source, source])['passed'] == 3
```

The first and last results recompute count 7 and highlights A/B. The malformed
middle occurrence has its own error. A different hook can replace the file with
another valid selection, so repeated paths can also yield different valid counts.
This is ordered observation, not a transaction over all input files. Callers that
need a simultaneous snapshot must supply independently retained immutable inputs.

Comparison refuses duplicate JSON keys, trailing documents and excessive nesting
before publishing deltas. Batch validation isolates those failures as ordered
rows and continues. Input and output byte ceilings belong to each call. Separate
destinations let independent calls interleave without sharing their budgets.
Standard error is a process-wide diagnostic stream, so callers requiring separate
captured diagnostics should use separate processes or their own routing.

KeyboardInterrupt propagates with its original exception identity during second
artifact validation or delta encoding. No partial success report is published and
prior destination bytes survive. The focused tests restore the injected fault and
verify a complete retry. A complete failed batch report exits 1, while comparison
or publication refusal exits 2. See
[the exit contract](x04-review-exit-status-contract.md).

The failure-payload benchmark uses 10/100/1000 invalid occurrences followed by a
valid control. It checks bounded per-entry error text and escaped control bytes in
JSON, exact totals and recovery, and records report bytes, time and peak memory
without thresholds. Run `python scripts/bench_x04_batch_failure_payload.py`.
These reports cannot authenticate original drawing bytes. Matching identities,
repeated paths and valid replay do not replace independently retained original
bytes and a trusted SHA-256 history. Real DWG, staging, browser and ten-drawing
acceptance evidence remain separate.
