# X04 stream budgets and escaped output

Studio X-bench X04 track 2 emits ASCII JSON with escaped Unicode. A portable
artifact can therefore exceed the UTF-8 input size of its source. Both CLIs
apply the input-byte ceiling independently. The default is 16777216 bytes.

Run this synthetic example on the allocated Linux verification host in a
scratch directory. Set REPO to the checkout's absolute path. Save the worked
JSON from x04-offline-report-export-contract.md as example.json.

```sh
python - <<'PY'
import json
from pathlib import Path
source = json.loads(Path('example.json').read_text(encoding='utf-8'))
source['drawing']['future_extension'] = {'literal': '東' * 3000000}
Path('amplification-input.json').write_text(json.dumps(source, ensure_ascii=False), encoding='utf-8')
Path('validation.json').write_bytes(b'previous complete output')
PY
python "$REPO/scripts/export_x04_offline_evidence.py" amplification-input.json --out escaped.json
python "$REPO/scripts/export_x04_offline_evidence.py" amplification-input.json | python "$REPO/scripts/validate_x04_offline_evidence.py" - --out validation.json
```

The source contains about 9 MB of literal UTF-8 text. The exported ASCII JSON
contains about 18 MB of escaped text. Export to escaped.json exits 0. The
validator in the pipeline exits 2 with a byte-ceiling diagnostic, empty stdout
and preserved validation.json bytes. The producer can also report a broken
pipe when the validator closes its input. Flush failures return 2 without an
interpreter-shutdown traceback.

Read the actual exported byte count, then give the validator that explicit
matching budget. Export uses the same stable serialization for file and
stdout output.

```sh
budget=$(python -c "from pathlib import Path; print(Path('escaped.json').stat().st_size)")
python "$REPO/scripts/export_x04_offline_evidence.py" amplification-input.json | python "$REPO/scripts/validate_x04_offline_evidence.py" - --max-input-bytes "$budget" --out validation.json
python - <<'PY'
import json
from pathlib import Path
result = json.loads(Path('validation.json').read_bytes())
assert result['valid'] is True
assert result['recomputed_result'] == {'count': 7, 'highlight_handles': ['A', 'B'], 'original_bytes_integrity': 'unverified'}
PY
```

Recovery exits 0 for both commands, with empty stderr. A focused subprocess
test uses the same amplification mechanism with smaller explicit byte limits,
checks preserved output on refusal and succeeds at the exact exported size.

```sh
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_evidence_export_fixed_point.py server/tests/test_x04_evidence_short_reads.py server/tests/test_x04_evidence_stdout_flush.py server/tests/test_x04_evidence_diagnostic_injection.py -q
python scripts/bench_x04_evidence_escape_amplification.py
```

Short-read tests cover one-byte and irregular chunks plus a read error after
a valid prefix, followed by successful recovery. Diagnostics escape control
characters into ASCII, retain at most 128 message characters and fit within
2048 bytes per line. Long input names cannot inject terminal controls or
extra diagnostic lines. Repeated export/validation cycles retain stable
serialized bytes after the first normalized export and preserve opaque fields.

The benchmark compares ASCII, CJK, astral and escaped-control strings at
4096 decoded characters. Input bytes use literal UTF-8 JSON. Output bytes use
the CLI's ASCII escaping and indentation. Timings include JSON encoding for
export and decoding for validation. It checks literal round trips and exact
trusted claims, with no timing thresholds. First export includes tool loading.
Original-byte integrity stays unverified. Independent raw dependency coverage
and staging browser acceptance retain their own evidence requirements.
