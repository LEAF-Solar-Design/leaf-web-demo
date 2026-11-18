# X04 offline evidence fields

Studio X-bench X04 track 2 exports a portable synthetic drawing and its
recomputed claims. Recipients can inspect the JSON directly and recover the
retained fields below.

| Exported field | Retained information |
| --- | --- |
| schema and synthetic | Exact format discriminator and synthetic marker |
| drawing | Complete supplied drawing object, including unknown JSON fields |
| drawing.id | Literal synthetic drawing identity |
| drawing.intake | Embedded entities, layer and block names, handles, coordinates, arrays and supplied opaque metadata |
| drawing.dependency_pairs | Literal decoded group codes and strings, including reference names and paths |
| drawing extensions | Supplied opaque objects, notes, filenames and paths |
| observed_dependencies | Exact report matching decoded pairs, including literal names, dispositions and coverage text |
| request | Drawing identity and explicitly selected IDs with duplicate IDs removed |
| result | Recomputed count, canonical parent highlights and unverified original-byte integrity |

Export drops unknown fields at the envelope, request and result levels.
Drawing and intake extensions remain present. Dependency reports and rows
must match the decoded-record report exactly. Unknown report fields refuse.
Any source digest retained inside opaque metadata remains a supplied claim.

Save the worked example from x04-offline-report-export-contract.md as
example.json. This synthetic example adds values that a recipient can recover.

```python
import json
from pathlib import Path
payload = json.loads(Path('example.json').read_text(encoding='utf-8'))
payload['drawing']['future_extension'] = {'note': 'SENTINEL_PROJECT_NOTE'}
payload['drawing']['intake']['source_path'] = 'C:/synthetic/SENTINEL_PROJECT/input.dwg'
Path('exposure-input.json').write_text(json.dumps(payload), encoding='utf-8')
```

```sh
python scripts/export_x04_offline_evidence.py exposure-input.json --out exposure.json
python scripts/validate_x04_offline_evidence.py exposure.json
```

Inspect exposure.json to recover both sentinel strings. Validation still
returns count 7 and highlights A and B. Before sharing, review drawing,
intake, decoded dependency pairs and opaque metadata for the intended
recipient. Review literal paths and notes as well as the visible count report.
The exporter retains those values. Byte integrity remains unverified.

Both CLIs accept - as stdin. This offline synthetic pipe carries the same
entities and literal reference metadata.

```sh
python scripts/export_x04_offline_evidence.py example.json | python scripts/validate_x04_offline_evidence.py -
```

The default input ceiling is 16777216 bytes, including BOM and whitespace.
Use --max-input-bytes with a positive integer to configure either CLI.
Files exceeding the ceiling refuse from metadata before reading. Stream
reads consume at most the ceiling plus one byte before oversized refusal,
using chunks of at most 65536 bytes. Integer tokens above 4300 digits refuse
even when Python's own digit limit is disabled. Long option names must be
complete. Help and argument errors occur before input/output access.

```sh
python scripts/export_x04_offline_evidence.py example.json --max-input-bytes 16777216 --out evidence.json
python scripts/validate_x04_offline_evidence.py - --max-input-bytes 16777216 < evidence.json
python scripts/bench_x04_evidence_rejection_positions.py
```

Invalid or truncated stdin exits 2 with diagnostic stderr and empty stdout.
Prior output bytes survive refusal. The benchmark places forged dependency
claims at first, middle and last positions, checks refusal and valid recovery,
and records time and peak memory without thresholds. These tools establish
consistency with embedded synthetic inputs. Independent raw completeness,
original-byte proof and staging browser acceptance retain their own evidence.
