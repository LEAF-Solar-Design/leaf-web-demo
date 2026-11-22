# Offline batch and comparison replay

Parent: Studio X-bench X04 intake, track 2. These commands use synthetic JSON
artifacts and retain original-byte integrity as unverified.

First create before.json and after.json with the Python recipe in
[the delta review](x04-evidence-delta-review.md). It selects both sets before,
then only the INSERT set after, changing the exact count from 7 to 6 and removing
highlight B. Both retain highlight A. Font dependency strings differ literally.
Create a malformed middle input without changing either valid artifact:

```powershell
python -c "from pathlib import Path; Path('broken.json').write_bytes(b'{broken')"
python scripts/batch_validate_x04_evidence.py before.json broken.json before.json after.json --out batch.json
```

Expect exit 1 and a complete JSON report with total 4, passed 3 and failed 1.
Indices 0, 1, 2 and 3 follow argument order. Both occurrences of before.json
report count 7 and highlights A/B. Index 1 is refused. The last occurrence
recovers with count 6 and highlight A. Repeated paths remain repeated results.
The command never opens the recorded font reference or scans for more inputs.
Missing paths and directories also receive per-occurrence failure rows.

```powershell
python scripts/compare_x04_offline_evidence.py before.json after.json --out delta.json
python scripts/compare_x04_offline_evidence.py after.json before.json
python scripts/compare_x04_offline_evidence.py before.json before.json
```

Expect exit 0 for all three. Forward count delta is -1 with B removed. Reverse
count delta is +1 with B added. Swapping inputs exchanges dependency additions
and removals too. Self-comparison has zero count delta and empty membership
deltas. Comparison independently validates each artifact before publishing.

Save output bytes, force a budget refusal, then recover:

```powershell
python -c "from pathlib import Path; Path('saved.bin').write_bytes(Path('batch.json').read_bytes())"
python scripts/batch_validate_x04_evidence.py before.json after.json --out batch.json --max-output-bytes 1
python -c "from pathlib import Path; assert Path('batch.json').read_bytes() == Path('saved.bin').read_bytes()"
python scripts/batch_validate_x04_evidence.py before.json after.json --out batch.json
```

The budget refusal exits 2, emits only stderr diagnostics and preserves the
previous output bytes. The final valid batch exits 0 with total 2, passed 2,
failed 0. An output alias to any input, including a symlink or hardlink, also
refuses before replacement. Both comparison sides accept a single binary stdin
stream via `-`; two stdin arguments refuse before consumption. Byte ceilings
include input whitespace and the final output newline. Tests cover exact and
one-byte-under limits and recovery with the same synthetic data.

Run the focused checks in test_x04_comparison_delta_algebra.py,
test_x04_review_path_aliases.py, test_x04_batch_occurrence_order.py and
test_x04_comparison_stdin_budgets.py. The retention observation is
`python scripts/bench_x04_batch_retention.py`, with 10, 100 and 1000 explicit
occurrences at fixed artifact size. It checks ordered counts and results and
records elapsed time, peak memory and encoded output bytes without thresholds.

These outputs establish internal consistency under the trusted replay code.
They cannot authenticate source bytes or prove unchanged drawing content.
Matching drawing IDs remain embedded claims. Retained original bytes and a
trusted independent SHA-256 history are required for byte identity. Raw customer
drawings, staging delivery, browser rendering and the real ten-drawing grader
remain separate X04 acceptance evidence.
