# X01 offline repros

These synthetic checks support X01 steps 1, 3, 6, and 7. They do not establish
PASS on staging or validate customer input. Run from the repository root with
Python 3 and its standard library; no installed modules or login are needed:

```bash
python tools/xbench/x01_offline/check_cases.py --self-test
python tools/xbench/x01_offline/check_plot_layers.py --self-test
python tools/xbench/x01_offline/check_unit_equivalence.py --self-test
```

The first two commands are sibling work. The unit checker reads no environment
or DXF file and changes no headers. INSUNITS 1 declares 0.0254 metres/unit and 6
declares 1.0. INSUNITS 0 requires an explicit positive finite metres/unit value;
missing, zero, negative, bool, nonfinite, unsupported codes and conflicting
explicit/header scales are named refusals. Positions convert to metres.
Model-space text height converts to metres, then paper millimetres as
`height * metres_per_unit * 1000 * sheet_scale`. The explicit dimensionless
`sheet_scale` is paper-mm/model-mm, **not** ezdxf viewport `custom_scale` across
arbitrary paper/model units. Legibility is separate: 1.8 mm is accepted and
values below it are refused. Physical comparisons use `math.isclose` with zero
relative tolerance and 1e-12 absolute tolerance (metres or mm as applicable).

Hand-authored equivalents include 100 inches, 2.54 metres and 200 unitless units
at .0127 metres/unit, complete signed point arrays including zero, and text
heights 10 inches/.254 metres/20 unitless units. At sheet ratio .01 all give
2.54 mm. A deliberately wrong inches/metres pairing must produce
`UNIT_EQUIVALENCE_MISMATCH` inside a passing refusal test.

## Tiny ASCII DXF layer repro (step 4)

Use a scratch directory outside git. The following Bash command writes the
inline ASCII group-code pairs without needing ezdxf:

```bash
python - /tmp/x01-offline-layer.dxf <<'PY'
import pathlib, sys
pairs = [
    (0, 'SECTION'), (2, 'TABLES'), (0, 'TABLE'), (2, 'LAYER'), (70, 1),
    (0, 'LAYER'), (2, 'X01_NO_PLOT'), (70, 0), (62, 7),
    (6, 'CONTINUOUS'), (290, 0), (0, 'ENDTAB'), (0, 'ENDSEC'),
    (0, 'SECTION'), (2, 'ENTITIES'), (0, 'LINE'), (5, 'A1'),
    (8, 'X01_NO_PLOT'), (10, 0), (20, 0), (30, 0),
    (11, 100), (21, 0), (31, 0), (0, 'ENDSEC'), (0, 'EOF'),
]
pathlib.Path(sys.argv[1]).write_text(
    ''.join(f'{code}\n{value}\n' for code, value in pairs), encoding='ascii')
PY
python tools/xbench/x01_offline/check_plot_layers.py /tmp/x01-offline-layer.dxf --module-layer X01_NO_PLOT
```

Expected: exit 1 with offending entity handle `A1` on layer `X01_NO_PLOT`
(layer group 290 is 0: plotting disabled). A parser refusal is not proof of the
layer finding; retain the named handle/layer diagnostic. The sibling checker
must be integrated before running this command.

## Local hash custody replay (steps 1 and 7)

Save the following as a scratch `custody.py` **outside git**. Arguments are the
original fixture, separately supplied export, validation JSON, diagnostic JSON,
then `--` and a caller-supplied **local** export command with its arguments.
The command runs between the original's before/after hashes, without a shell.
It must not invoke a remote export. Use fresh scratch output paths per replay;
the operation must finish writing the export and validation before returning.
Do not use the original as any output. Never commit customer bytes or paths.

Validation is an explicit offline adapter contract, not an invented DXF tag:
`{"export_sha256":"<hash of validated export bytes>","checks_complete":true,"tag_count":1,"objects":[{"name":"A1:layer:X01_NO_PLOT","verdict":"FAIL","diagnostic":"plot disabled"}]}`.
The local validator must enumerate all required objects/checks, use stable names,
and include this export hash, a positive measured string-tag count, and an explicit
completion flag. Missing string tags, empty objects, unknown verdicts,
missing validation, or hash mismatch mean UNREADABLE. Do not manufacture PASS
tags merely because an export exists. Named object diagnostics are preserved.

```python
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def digest(path):
    try:
        with path.open('rb') as stream:
            h = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


if len(sys.argv) < 7 or sys.argv[5] != '--':
    raise SystemExit('usage: custody.py FIXTURE EXPORT VALIDATION DIAGNOSTIC -- LOCAL_COMMAND [ARGS...]')
fixture, export, validation, output = map(Path, sys.argv[1:5])
if len({p.resolve() for p in (fixture, export, validation, output)}) != 4:
    raise SystemExit('paths must be distinct; output must be outside git')
expected = digest(fixture)
operation = None
try:
    if expected is not None:
        # Keep command output local; do not embed paths/customer content in JSON.
        operation = subprocess.run(sys.argv[6:], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, check=False).returncode
except OSError:
    pass
observed = digest(fixture)
export_hash = digest(export)


def reject_nonfinite(token):
    raise ValueError('nonfinite JSON number')


try:
    tags = json.loads(validation.read_text(encoding='utf-8'),
                      parse_constant=reject_nonfinite)
except (OSError, ValueError):
    tags = None
objects = tags.get('objects') if isinstance(tags, dict) else None
valid_tags = (
    isinstance(tags, dict) and export_hash is not None
    and tags.get('export_sha256') == export_hash
    and tags.get('checks_complete') is True
    and type(tags.get('tag_count')) is int and tags['tag_count'] > 0
    and isinstance(objects, list) and bool(objects)
    and all(isinstance(item, dict)
            and isinstance(item.get('name'), str) and bool(item['name'].strip())
            and item.get('verdict') in ('PASS', 'FAIL', 'UNREADABLE')
            and isinstance(item.get('diagnostic'), str)
            and bool(item['diagnostic'].strip()) for item in objects)
)
diagnostics = []
if expected is not None and observed is not None and expected != observed:
    verdict, reason = 'FAIL', 'FIXTURE_BYTES_MUTATED'
elif expected is None or observed is None:
    verdict, reason = 'UNREADABLE', 'FIXTURE_UNREADABLE'
elif export_hash is None:
    verdict, reason = 'UNREADABLE', 'EXPORT_UNREADABLE'
elif operation != 0:
    verdict, reason = 'UNREADABLE', 'LOCAL_EXPORT_OPERATION_UNREADABLE'
elif not valid_tags:
    verdict, reason = 'UNREADABLE', 'EXPORT_VALIDATION_TAGS_ABSENT_OR_INVALID'
elif any(item['verdict'] == 'FAIL' for item in objects):
    verdict, reason = 'FAIL', 'NAMED_OBJECT_FAILURE'
elif any(item['verdict'] == 'UNREADABLE' for item in objects):
    verdict, reason = 'UNREADABLE', 'NAMED_OBJECT_UNREADABLE'
else:
    verdict, reason = 'PASS', 'LOCAL_REPLAY_VALIDATED'
diagnostics.append({'name': reason, 'verdict': verdict})
record = {
    'schema': 'x01.offline.custody.v1', 'verdict': verdict,
    'fixture_sha256_expected': expected, 'fixture_sha256_observed': observed,
    'export_sha256_observed': export_hash, 'local_operation_exit': operation,
    'diagnostics': diagnostics, 'validation': tags,
}
output.parent.mkdir(parents=True, exist_ok=True)
temporary = None
try:
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                     dir=output.parent, delete=False) as stream:
        temporary = stream.name
        json.dump(record, stream, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
finally:
    if temporary is not None and os.path.exists(temporary):
        os.unlink(temporary)
print(json.dumps({'verdict': verdict, 'diagnostic': reason}))
raise SystemExit({'PASS': 0, 'FAIL': 1, 'UNREADABLE': 2}[verdict])
```

Copyable Bash invocation, replacing the generic scratch filenames with local
arguments (the local exporter/validator is caller supplied, not a dependency):

```bash
python /tmp/custody.py /tmp/original.dxf /tmp/export.dxf /tmp/validation.json /tmp/evidence/custody.json -- python /tmp/local_export.py /tmp/original.dxf /tmp/export.dxf /tmp/validation.json
```

For a stdlib-only synthetic replay, a caller can use `python -c` as the operation
instead. It copies bytes and writes explicitly synthetic named validation:

```bash
python /tmp/custody.py /tmp/original.dxf /tmp/export.dxf /tmp/validation.json /tmp/evidence/custody.json -- python -c 'import hashlib,json,pathlib,sys; src,dst,tags=map(pathlib.Path,sys.argv[1:]); dst.write_bytes(src.read_bytes()); tags.write_text(json.dumps({"export_sha256":hashlib.sha256(dst.read_bytes()).hexdigest(),"checks_complete":True,"tag_count":1,"objects":[{"name":"synthetic:byte-copy","verdict":"PASS","diagnostic":"synthetic copy only, fabricated tag count for adapter exercise"}]}))' /tmp/original.dxf /tmp/export.dxf /tmp/validation.json
```

Precedence: observed fixture-byte mutation is FAIL even with an absent export.
Missing/unreadable export or absent validation tags is UNREADABLE, never PASS
or a successful deliverable. A readable export with valid, bound validation and
an observed named failure is FAIL. An existing export without validation is
UNREADABLE. Fixture deletion is UNREADABLE because its after-bytes cannot be
hashed. All recorded hashes describe locally observed bytes only. The diagnostic
schema is offline, **not** `leaf.xbench.grade.v1`; it makes no `source_sha`, live,
readiness, or staging assertion. Keep all evidence outside git.
