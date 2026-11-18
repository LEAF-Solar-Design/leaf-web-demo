# X05 offline bundle integrity replay

Parent: Studio X-bench X05 evidence reconciliation. This library checks supplied
evidence inventories and bytes, without files, archive extraction, transport or
live grading. A member declares `name`, `fixture`, `stage`, `byte_length`,
`sha256` and `design_version`. Expected fixture IDs, stages and versions come
from the caller. Exactly one inventory member covers each expected slot.

Names are relative ASCII segments. Slash and backslash compare as `/`; absolute,
drive, UNC, empty, dot and parent segments are rejected. The default limits are
128 members, 1 MiB per member and 8 MiB cumulative. Any invalid inventory stops
the byte auditor before payload access or hashing. Unchecked member results use
`bytes_equal: null`; they do not claim verification. Payloads must be supplied `bytes`.
Results contain fixed diagnostic codes and indices, exact per-code counts and
totals, and at most 32 diagnostic examples. Byte results retain every member.
Metadata/reference diagnostic indices use canonical rows ordered by normalized
name, fixture, stage, version, SHA and size. Name-only checks sort normalized
names. Byte-result `member_index` retains caller order. Foreign payload indices
use sorted normalized names. No supplied payload or raw filename is echoed.

From the repository root, run the following with `python -P` after saving it as
a temporary script outside git. Set `repo` to this checkout. It needs only the
standard library and the owned `benches.x05_bundle` module. The setup constructs
two synthetic fixtures with seven stages and deliberately identical totals.

```python
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import json
import sys

repo = Path('D:/GitHub/xbench-x05-late-subscribers')
sys.path.insert(0, str(repo / 'tools/xbench'))
from benches.x05_bundle import validate_inventory, audit_bundle_bytes, check_stage_references

fixtures = ['alpha', 'beta']
stages = ['import', 'grids', 'strings', 'grouping', 'table', 'deltas', 'reopened']
versions = {'alpha': 'design-a', 'beta': 'design-b'}
members, payloads, references = [], {}, []
for fixture in fixtures:
    for stage in stages:
        name, data = f'{fixture}/{stage}.json', b'{"panels":1}'
        payloads[name] = data
        members.append(dict(name=name, fixture=fixture, stage=stage,
                            byte_length=len(data), sha256=sha256(data).hexdigest(),
                            design_version=versions[fixture]))
        references.append(dict(name=name, fixture=fixture, stage=stage,
                               design_version=versions[fixture]))

options = dict(expected_fixture_ids=fixtures, required_stages=stages)
original = deepcopy((members, payloads, references))
clean = audit_bundle_bytes(members, payloads, **options)
assert clean['ok'] and clean['integrity_verified'] and not clean['origin_authenticated']
assert validate_inventory(members, **options)['ok']
missing = validate_inventory(members[1:], **options)
assert missing['diagnostic_counts'] == {'FIXTURE_STAGE_MISSING': 1}

changed = dict(payloads)
changed[members[0]['name']] = b'{"panels":2}'  # One byte, unchanged length.
substitution = audit_bundle_bytes(members, changed, **options)
assert substitution['diagnostic_counts'] == {'PAYLOAD_SHA256_MISMATCH': 1}
assert [m['bytes_equal'] for m in substitution['members']] == [False] + [True] * 13

swapped = deepcopy(references)
swapped[0]['name'] = 'beta/import.json'  # Identical bytes and total, wrong owner.
ownership = check_stage_references(members, swapped,
    expected_fixture_versions=versions, required_stages=stages)
assert ownership['diagnostic_counts'] == {
    'REFERENCE_CROSS_FIXTURE': 1, 'REFERENCE_MEMBER_VERSION_CONFLICT': 1}
assert audit_bundle_bytes(members, payloads, **options)['ok']
assert (members, payloads, references) == original
print(json.dumps(dict(clean_integrity=True, origin_authenticated=False,
    missing=missing['diagnostic_counts'], substitution=substitution['diagnostic_counts'],
    equal_total_swap=ownership['diagnostic_counts']), sort_keys=True))
```

Stop on any failed assertion. The replay leaves no files, services, windows or
unsaved product state. The temporary script and captured output may be retained
as synthetic proof. Hashes verify equality against supplied pins; an attacker
who rewrites both bytes and pins can pass this audit. Origin authentication,
oracle independence and the frozen seven-step live grade require separate
source-bound evidence. `ok` is not a product PASS, and this module grants no
staging admission or upload authority.

## Strict decoding and metadata-first replay

The JSON envelope has exactly `members`, `expected_fixture_ids` and
`required_stages`. Every member has exactly the six fields above. Text is
limited to 262,144 characters and 16 container levels. Duplicate keys, NaN,
infinities (including overflowing numeric literals), malformed JSON and missing
fields are rejected without hashing payloads or echoing rejected input.
`decode_inventory_json` returns validated data in `inventory` only on success.
`audit_inventory_json` composes that decoder with the byte auditor.

Run this second standalone block with `python -P` using the same checkout.
It instruments only the library's digest reference and restores it in `finally`.
The NUL-byte digest is a literal reference vector cross-checked independently
using Windows .NET SHA256, as are the empty and non-UTF-8 vectors in the tests.

```python
from pathlib import Path
import json
import sys

repo = Path('D:/GitHub/xbench-x05-late-subscribers')
sys.path.insert(0, str(repo / 'tools/xbench'))
from benches import x05_bundle as bundle

member = dict(name='unit/saved.bin', fixture='unit', stage='saved',
    byte_length=1, design_version='v1',
    sha256='6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d')
def envelope(row):
    return json.dumps(dict(members=[row], expected_fixture_ids=['unit'], required_stages=['saved']))
payloads = {member['name']: b'\x00'}
original_digest, calls = bundle.sha256, []
def tracked(data):
    calls.append(len(data))
    return original_digest(data)
bundle.sha256 = tracked
try:
    duplicate = bundle.audit_inventory_json('{"members":[],"members":[]}', payloads)
    assert duplicate['diagnostic_counts'] == {'JSON_DUPLICATE_KEY': 1}
    oversized = bundle.audit_inventory_json(envelope(dict(member,
        byte_length=bundle.MAX_MEMBER_BYTES + 1)), payloads)
    assert oversized['diagnostic_counts'] == {'MEMBER_BYTES_EXCEEDED': 1}
    missing = dict(member)
    del missing['fixture']
    absent = bundle.audit_inventory_json(envelope(missing), payloads)
    assert absent['diagnostic_counts'] == {'MEMBER_FIELDS_INVALID': 1}
    assert calls == []
    assert all(not r['origin_authenticated'] for r in (duplicate, oversized, absent))
    decoded = bundle.decode_inventory_json(envelope(member))
    assert decoded['ok'] and not decoded['integrity_verified']
    clean = bundle.audit_inventory_json(envelope(member), payloads)
    assert clean['integrity_verified'] and calls == [1]
    assert clean['origin_authenticated'] is False
    print(json.dumps(dict(duplicate=duplicate['diagnostic_counts'],
        budget=oversized['diagnostic_counts'], missing=absent['diagnostic_counts'],
        rejected_hashes=0, valid_hashes=len(calls), origin_authenticated=False), sort_keys=True))
finally:
    bundle.sha256 = original_digest
```

Stop on any failed assertion. No file, browser, archive, network service or
product state is created. Hash equality still establishes no origin identity.

## Bundle-to-seven-step semantic replay

`reconcile_bundle` first audits the complete inventory and supplied bytes, then
checks fixture/version stage references. Only intact, bound fixture members
reach strict UTF-8/JSON evidence decoding. All embedded fixture claims and saved
design versions must match the explicitly supplied outer bindings before the
existing bound seven-step comparator runs. Integrity and reference diagnostics
remain separate from semantic results. A byte failure in one fixture does not
hide a readable sibling's PASS or FAIL. The offline aggregate gives UNREADABLE
precedence; it does not change the live grader's frozen FAIL dominance.

`decode_replay_json` accepts exactly `inventory`, `references`,
`expected_fixture_versions`, `evidence_stage` and `member_bytes_hex`. The inventory
uses the strict three-field envelope above. Hex values allow only paired hex
digits, with no spaces. The decoder checks all lengths, individual and total
decoded budgets before creating any `bytes.fromhex` output. Its encoded-text
limit is twice the 8 MiB decoded budget plus 262,144 metadata characters, with
16 container levels. Optional `max_member_bytes` and `max_total_bytes` can lower
the hard limits; they cannot raise them. Successful decoding alone proves no
integrity. `replay_bundle_json` then invokes the adapter.

This executable uses the checkout's existing complete synthetic seven-step
builder. It requires the test dependency `pytest` for importing that builder;
it does not run or collect a test suite. All product helpers remain standard
library only. Save the block outside git and run `python -P -B` from this
checkout. The support builder supplies fake provenance, oracles and design IDs;
none is an independent real-world authority.

```python
from copy import deepcopy
from pathlib import Path
import json
import sys

repo = Path('D:/GitHub/xbench-x05-late-subscribers')
sys.path.insert(0, str(repo / 'tools/xbench'))
from benches import x05_bundle as bundle
from tests.test_x05_bundle import reconciliation_bundle, replay_envelope

clean_source = reconciliation_bundle()
before = deepcopy(clean_source)
clean = bundle.replay_bundle_json(json.dumps(replay_envelope(clean_source)))
assert clean['integrity']['integrity_verified'] and clean['semantic']['verdict'] == 'PASS'
failed_source = reconciliation_bundle(failed=('alpha',))
failed = bundle.replay_bundle_json(json.dumps(replay_envelope(failed_source)))
assert failed['integrity']['integrity_verified'] and failed['semantic']['verdict'] == 'FAIL'
assert failed['semantic']['fixtures']['alpha']['result']['reconciliation']['steps']['1']['verdict'] == 'FAIL'

tampered_source = deepcopy(clean_source)
tampered_source['payloads']['alpha/seven.json'] += b' '  # Still valid JSON, wrong pinned bytes.
calls = []
original_decode = bundle.reconciliation.decode_evidence_json
original_reconcile = bundle.reconciliation.reconcile_bound_seven_steps
def forbidden(*args, **kwargs):
    calls.append(1)
    raise AssertionError('Tampered evidence reached semantics')
bundle.reconciliation.decode_evidence_json = forbidden
bundle.reconciliation.reconcile_bound_seven_steps = forbidden
try:
    tampered = bundle.replay_bundle_json(json.dumps(replay_envelope(tampered_source)))
    assert not tampered['integrity']['integrity_verified']
    assert tampered['semantic']['verdict'] == 'UNREADABLE' and calls == []
    assert tampered['integrity']['diagnostic_counts'] == {
        'PAYLOAD_LENGTH_MISMATCH': 1, 'PAYLOAD_SHA256_MISMATCH': 1}
finally:
    bundle.reconciliation.decode_evidence_json = original_decode
    bundle.reconciliation.reconcile_bound_seven_steps = original_reconcile
assert clean_source == before
assert all(not r['origin_authenticated'] for r in (clean, failed, tampered))
print(json.dumps(dict(clean='PASS', readable_mismatch='FAIL', tampered='UNREADABLE',
    tampered_diagnostics=tampered['integrity']['diagnostic_counts'],
    tampered_semantic_calls=len(calls), origin_authenticated=False), sort_keys=True))
```

Stop on any failed assertion. Instrumentation is restored in `finally`. The
replay starts no browser, service, extraction or product mutation. Hash-correct
duplicate-key JSON or foreign fixture/version claims also remain UNREADABLE.
An attacker who rewrites every pin and matching claim can still pass this
offline comparison. Origin authentication and the admitted live grade remain
separate controls.

## Stdin-only audited replay CLI

Invoke `python -P -B tools/xbench/benches/x05_bundle.py --replay` from the
checkout, passing the replay envelope as UTF-8 bytes on stdin. Omitting
`--replay` selects the same mode. No positional input paths are accepted. The
trusted peer comparator is loaded relative to the script, so invocation also
works from another directory without setting an import path or reading an
operator environment.

Successful delivery emits exactly one newline-terminated ASCII JSON result.
Exits are `0` offline semantic PASS, `1` semantic FAIL, `4` UNREADABLE, `2`
usage error, and `5` output write/flush failure. Delivery failure overrides every
semantic exit; a partially written result is not restarted or reported through
another traceback. `--help` returns text with exit 0. Help and usage errors
consume no stdin and invoke no replay. The CLI's wire limit is 16.25 MiB; it
reads at most 8192 bytes per call, joins short reads before strict UTF-8 decoding,
and rejects over-limit data before replay. Read failures and invalid UTF-8 emit
sanitized `CLI_INPUT_UNREADABLE`, while overflow uses `CLI_INPUT_TOO_LARGE`.

Fixture identifiers keep the existing ASCII policy. Synthetic Unicode IDs
therefore produce named UNREADABLE results, byte-identically under the tested
hash seeds, UTF-8 modes and stdout encodings. No credentials or operator
environment are copied into those test subprocess profiles. This does not
authenticate origin or grant live-pass admission.

This executable example reuses the same pytest-backed synthetic builders as
the preceding recipe, without running a test suite. Save it outside git and
execute with `python -P -B`. Every evidence byte goes through stdin; only the
trusted program path is passed as a command argument.

```python
from pathlib import Path
import json
import subprocess
import sys

repo = Path('D:/GitHub/xbench-x05-late-subscribers')
sys.path.insert(0, str(repo / 'tools/xbench'))
from benches import x05_bundle as bundle
from tests.test_x05_bundle import cli_payload

command = [sys.executable, '-P', '-B',
    str(repo / 'tools/xbench/benches/x05_bundle.py'), '--replay']
observed = {}
for kind, expected_exit in [('PASS', 0), ('FAIL', 1), ('UNREADABLE', 4), ('malformed', 4)]:
    raw = cli_payload(kind)
    completed = subprocess.run(command, input=raw, capture_output=True, timeout=15)
    assert completed.returncode == expected_exit and completed.stderr == b''
    assert len(completed.stdout.splitlines()) == 1 and all(b < 128 for b in completed.stdout)
    expected = bundle.replay_bundle_json(raw.decode('utf-8'))
    assert completed.stdout == (bundle.reconciliation.serialize_result(expected) + '\n').encode('ascii')
    result = json.loads(completed.stdout)
    assert result['origin_authenticated'] is False
    if kind == 'UNREADABLE':
        assert result['integrity']['diagnostic_counts'] == {
            'PAYLOAD_LENGTH_MISMATCH': 1, 'PAYLOAD_SHA256_MISMATCH': 1}
    if kind == 'malformed':
        assert result['integrity']['diagnostic_counts'] == {'JSON_INVALID': 1}
    observed[kind] = dict(exit=completed.returncode, semantic=result['semantic']['verdict'])
print(json.dumps(dict(cases=observed, origin_authenticated=False), sort_keys=True))
```

Stop on any mismatch. Each subprocess has a 15-second deadline; the standard
runner terminates and waits for an expired child. No child, browser, service,
extraction or product mutation is retained. The live grader and its source-bound
admission remain separate from these offline CLI verdicts.

## Borrowed stream failure and recovery replay

The CLI rejects `None`, text, `bytearray` and overreported read chunks, including
after a complete valid prefix, without replaying that prefix. Zero, negative,
boolean, absent and overreported write counts stop with exit 5. Interrupted
reads return sanitized exit 4; interrupted writes/flushes return 5 without
restarting output or semantics. The CLI never closes caller-owned stream or
buffer objects. A caller can invoke it again with working streams.

The following executable uses the same synthetic builder dependency as the
prior examples. Run it with `python -P -B` after saving it outside git. It
checks failure exits `4,5,5`, then recovery exit `0`, exact partial prefixes,
one replay per deliverable invocation and no CLI stream closure. The caller
closes its own memory streams only after those assertions.

```python
from io import BytesIO
from pathlib import Path
import json
import sys
from pytest import MonkeyPatch

repo = Path('D:/GitHub/xbench-x05-late-subscribers')
sys.path.insert(0, str(repo / 'tools/xbench'))
from benches import x05_bundle as bundle
from tests.test_x05_bundle import cli_payload

raw = cli_payload()
original = bundle.replay_bundle_json
expected = (bundle.reconciliation.serialize_result(original(raw.decode())) + '\n').encode('ascii')
calls, owned, observed = [], [], []
class Buffer(BytesIO):
    def __init__(self, data=b'', mode='clean'):
        super().__init__(data); self.mode, self.writes, self.closes = mode, 0, 0
    def read(self, count):
        if self.mode == 'invalid_read' and self.tell() == len(raw): return None
        return super().read(min(7, count))
    def write(self, data):
        self.writes += 1
        if self.mode in ('zero_write', 'interrupted_write'):
            if self.writes == 1: return super().write(data[:7])
            if self.mode == 'zero_write': return 0
            raise InterruptedError('synthetic interrupted delivery')
        return super().write(data)
    def close(self): self.closes += 1; super().close()
class Stream:
    def __init__(self, buffer): self.buffer, self.closes = buffer, 0
    def write(self, text): return len(text)
    def flush(self): pass
    def fileno(self): raise OSError('memory stream has no descriptor')
    def close(self): self.closes += 1; self.buffer.close()
def replayed(text): calls.append(text); return original(text)
with MonkeyPatch.context() as patch:
    patch.setattr(bundle, 'replay_bundle_json', replayed)
    for mode, expected_exit in [('invalid_read',4), ('zero_write',5), ('interrupted_write',5), ('clean',0)]:
        incoming = Stream(Buffer(raw, mode))
        outgoing = Stream(Buffer(mode=mode))
        errors = Stream(Buffer())
        owned.extend((incoming, outgoing, errors))
        patch.setattr(sys, 'stdin', incoming); patch.setattr(sys, 'stdout', outgoing); patch.setattr(sys, 'stderr', errors)
        code = bundle.main(['--replay'])
        output = outgoing.buffer.getvalue()
        assert code == expected_exit and all(s.closes == 0 and s.buffer.closes == 0 for s in owned)
        if mode == 'invalid_read':
            assert calls == [] and json.loads(output)['integrity']['diagnostic_counts'] == {'CLI_INPUT_UNREADABLE':1}
        elif mode != 'clean': assert output == expected[:7]
        else: assert output == expected and len(calls) == 3
        observed.append(code)
assert all(not s.buffer.closed for s in owned)
for stream in owned: stream.close()  # Caller cleanup, after ownership checks.
print(json.dumps(dict(exits=observed, semantic_calls=len(calls), cli_closed_streams=0, origin_authenticated=False), sort_keys=True))
```

Stop on any mismatch. The monkeypatch context restores interpreter streams and
the replay function even on failure. No child, browser, service or product state
is retained. Exact-number tests additionally exercise signed DC deltas down to
`1e-60`, the maximum supported per-string count `9007199254740991`, and its
aggregate `9007199254740998`. JSON retains exact values and does not change the
caller's Decimal context. Counts outside the per-string contract remain
UNREADABLE rather than being reclassified as readable mismatches.

## Deterministic encoder-to-CLI replay

`encode_replay_json` accepts an explicit inventory envelope, references and
member bytes, with expected fixture versions and an evidence stage. It returns
compact, sorted, ASCII JSON in `text`, plus its exact `wire_bytes`. Inventory
and reference arrays use canonical order. Caller objects and supplied length/
SHA pins remain unchanged. Encoding does not hash payloads or run semantics;
validly shaped but mismatched pins are intentionally passed to the auditor.

The encoder bounds declared and actual member bytes, then computes exact hex
and JSON escape expansion before creating hex or output JSON strings. The
optional `max_wire_bytes` can lower the CLI wire ceiling, never raise it. A
budget rejection returns `ENCODER_WIRE_BUDGET_EXCEEDED`, `text: null`, and a
`wire_bytes_lower_bound`, without launching a CLI process. Encoder `ok` proves
format and resource validity only; `integrity_verified` and
`origin_authenticated` remain false.

Run this executable example with `python -P -B`, using the same synthetic
builder/test dependency as the earlier examples. It checks deterministic output,
unchanged mismatched pins, exact CLI exits `0,1,4`, and producer-side wire-budget
rejection before a CLI launch.

```python
from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys

repo = Path('D:/GitHub/xbench-x05-late-subscribers')
sys.path.insert(0, str(repo / 'tools/xbench'))
from benches import x05_bundle as bundle
from tests.test_x05_bundle import reconciliation_bundle, encoder_args, cli_command

observed = []
for kind, expected_exit in [('PASS',0), ('FAIL',1), ('UNREADABLE',4)]:
    source = reconciliation_bundle(('alpha','beta'), failed=('alpha',) if kind == 'FAIL' else ())
    if kind == 'UNREADABLE': source['payloads']['alpha/seven.json'] += b' '
    arguments = encoder_args(source)
    preserved = deepcopy(arguments)
    encoded = bundle.encode_replay_json(**arguments)
    assert encoded['ok'] and not encoded['integrity_verified'] and not encoded['origin_authenticated']
    assert arguments == preserved and len(encoded['text'].encode('ascii')) == encoded['wire_bytes']
    assert json.loads(encoded['text'])['inventory']['members'] == sorted(source['members'], key=lambda m:m['name'])
    reordered = deepcopy(arguments)
    reordered['inventory']['members'].reverse(); reordered['inventory']['expected_fixture_ids'].reverse()
    reordered['references'].reverse()
    reordered['member_bytes'] = dict(reversed(list(reordered['member_bytes'].items())))
    assert bundle.encode_replay_json(**reordered)['text'] == encoded['text']
    rejected = bundle.encode_replay_json(**arguments, max_wire_bytes=encoded['wire_bytes']-1)
    assert rejected['diagnostic_counts'] == {'ENCODER_WIRE_BUDGET_EXCEEDED':1} and rejected['text'] is None
    assert rejected['wire_bytes_lower_bound'] == encoded['wire_bytes']
    completed = subprocess.run(cli_command('--replay'), input=encoded['text'].encode('ascii'), capture_output=True, timeout=15)
    result = json.loads(completed.stdout)
    assert completed.returncode == expected_exit and completed.stderr == b''
    assert len(completed.stdout.splitlines()) == 1 and not result['origin_authenticated']
    assert result['semantic']['verdict'] == kind
    if kind == 'UNREADABLE':
        assert result['integrity']['diagnostic_counts'] == {'PAYLOAD_LENGTH_MISMATCH':1,'PAYLOAD_SHA256_MISMATCH':1}
    observed.append(completed.returncode)
print(json.dumps(dict(cli_exits=observed, deterministic=True, supplied_pins_preserved=True,
    wire_rejection='ENCODER_WIRE_BUDGET_EXCEEDED', origin_authenticated=False), sort_keys=True))
```

Stop on any mismatch. Encoding is pure and launches no process itself. The
example's three children have 15-second deadlines and terminate before the
summary. No browser, service, extraction or product state is retained. Pins
and agreeing claims can all be forged together; this encoder authenticates
neither evidence origin nor a live X05 result.

## Standalone seven-step fixture and replay

This example needs only Python's standard library and the two product helper
modules in this checkout. It does not import pytest or any test module. All
values, IDs and input bytes below are synthetic. Run with `python -P -B`.
The builder supplies explicit independent claims for this small fixture;
agreement between those claims does not authenticate their origin.

```python
from copy import deepcopy
from hashlib import sha256
from io import StringIO
from pathlib import Path
import csv
import json
import subprocess
import sys

repo = Path('D:/GitHub/xbench-x05-late-subscribers')
sys.path.insert(0, str(repo / 'tools/xbench'))
from benches import x05_bundle as bundle
from benches import x05_reconcile as reconciliation

def fixture_record(fixture, version):
    strings = [dict(string_id='A', optimizers=5), dict(string_id='B', optimizers=7)]
    panels = [dict(string_id='A', panels=10), dict(string_id='B', panels=14)]
    power = [dict(string_id='A', kw_dc='4'), dict(string_id='B', kw_dc='5.6')]
    owners = [dict(string_id=key, grid_id='g') for key in ('A', 'B')]
    inverters = [dict(inverter_id='INV-A', grid_id='g')]
    connections = [dict(string_id=key, inverter_id='INV-A') for key in ('A', 'B')]
    values = dict(panels=24, optimizers=12, inverters=1, strings=2, kw_dc='9.6')
    rows = [dict(metric=key, studio=value, oracle=value, delta=0, verdict='PASS') for key, value in values.items()]
    output = StringIO(newline='')
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    columns = ('metric', 'studio', 'oracle', 'delta', 'verdict')
    writer.writerow(columns)
    writer.writerows([row[key] for key in columns] for row in rows)
    hashes = reconciliation.canonical_snapshot(strings, rows)
    evidence = dict(oracle_strings=deepcopy(strings), saved_strings=deepcopy(strings),
        report_grid_count=2, recommendations=[dict(group_count=2)], view_rows=rows,
        saved_values=deepcopy(values), oracle_values=deepcopy(values), fixture_id=fixture,
        discrepancy_records=[], run_started_at='2026-11-15T13:00:00Z', before=deepcopy(hashes),
        reopened=deepcopy(hashes), csv_text=output.getvalue(),
        before_rows=dict(strings=deepcopy(strings), table_rows=deepcopy(rows)),
        reopened_rows=dict(strings=deepcopy(strings), table_rows=deepcopy(rows)),
        oracle_inverter_ids=['INV-A'], oracle_inverter_owners=deepcopy(inverters),
        oracle_string_owners=deepcopy(owners), oracle_panels=deepcopy(panels), oracle_dc_power=deepcopy(power),
        inverter_loads=dict(strings={'INV-A':2}, optimizers={'INV-A':12}, panels={'INV-A':24}, kw_dc={'INV-A':'9.6'}))
    for field in ('saved_inverters', 'before_inverters', 'reopened_inverters'):
        evidence[field] = deepcopy(inverters)
    for field in ('oracle_assignments', 'saved_assignments', 'before_assignments', 'reopened_assignments'):
        evidence[field] = deepcopy(connections)
    inputs = dict(dwg=b'synthetic drawing', report=b'synthetic report', bom=b'synthetic BOM')
    manifest = dict(fixture_id=fixture, inputs={key:sha256(raw).hexdigest() for key, raw in inputs.items()})
    identity = dict(project_id='project', version_id=version)
    assurance = dict(evidence=evidence, manifest=deepcopy(manifest),
        bindings={key:deepcopy(manifest) for key in reconciliation.PROVENANCE_STAGES},
        inputs_hex={key:raw.hex() for key, raw in inputs.items()}, identity=identity,
        stage_identities={key:deepcopy(identity) for key in reconciliation.IDENTITY_STAGES})
    grid = dict(expected_grid_ids=['g'], saved_group_ids=['p'],
        rows=[dict(grid_id='g', matches=['p'], cause=None)], require_all_matched=True,
        optimizer_totals={'g':12}, panel_totals={'g':24}, string_totals={'g':2},
        inverter_totals={'g':1}, dc_totals={'g':'9.6'})
    for prefix in ('', 'before_', 'reopened_'):
        grid[prefix + 'string_assignments'] = deepcopy(owners)
        grid[prefix + 'panel_counts'] = deepcopy(panels)
        grid[prefix + 'dc_power'] = deepcopy(power)
    envelope = dict(assurance=assurance, grid=grid,
        **{'import':dict(normal_user=True, dwg_imported=True, report_imported=True)})
    return dict(envelope=envelope, manifest=manifest,
        import_binding=deepcopy(manifest), grid_binding=deepcopy(manifest))

observed = []
for kind, expected_exit in [('PASS',0), ('FAIL',1), ('UNREADABLE',4), ('HASH_CORRECT_FAIL',1)]:
    record = fixture_record('unit', 'v1')
    if kind == 'FAIL': record['envelope']['import']['normal_user'] = False
    if kind == 'HASH_CORRECT_FAIL':
        # Change one saved count, retain the independent oracle, then pin the
        # changed bytes. Byte integrity must not substitute for reconciliation.
        record['envelope']['assurance']['evidence']['saved_strings'][0]['optimizers'] = 6
    raw = json.dumps(record, sort_keys=True).encode('ascii')
    member = dict(name='unit/seven.json', fixture='unit', stage='seven_step', design_version='v1',
                  byte_length=len(raw), sha256=sha256(raw).hexdigest())
    reference = {key:member[key] for key in ('name', 'fixture', 'stage', 'design_version')}
    # Retain the original pins while changing the delivered bytes.
    if kind == 'UNREADABLE': raw += b' '
    encoded = bundle.encode_replay_json(
        dict(members=[member], expected_fixture_ids=['unit'], required_stages=['seven_step']),
        [reference], {member['name']:raw}, expected_fixture_versions={'unit':'v1'}, evidence_stage='seven_step')
    assert encoded['ok'] and not encoded['integrity_verified'] and not encoded['origin_authenticated']
    completed = subprocess.run([sys.executable, '-P', '-B', str(Path(bundle.__file__).resolve()), '--replay'],
        input=encoded['text'].encode('ascii'), capture_output=True, timeout=15)
    assert completed.returncode == expected_exit and completed.stderr == b''
    assert len(completed.stdout.splitlines()) == 1
    result = json.loads(completed.stdout)
    assert result['semantic']['verdict'] == ('FAIL' if kind == 'HASH_CORRECT_FAIL' else kind)
    assert not result['origin_authenticated']
    if kind == 'HASH_CORRECT_FAIL':
        assert result['integrity']['integrity_verified']
        assert result['semantic']['fixtures']['unit']['result']['reconciliation']['steps']['3']['verdict'] == 'FAIL'
    if kind == 'UNREADABLE':
        assert result['integrity']['diagnostic_counts'] == {'PAYLOAD_LENGTH_MISMATCH':1, 'PAYLOAD_SHA256_MISMATCH':1}
    else: assert result['integrity']['integrity_verified']
    observed.append(completed.returncode)
renaming_exits = []
raw = json.dumps(fixture_record('unit', 'v1'), sort_keys=True).encode('ascii')
original = dict(name='unit/seven.json', fixture='unit', stage='seven_step', design_version='v1',
                byte_length=len(raw), sha256=sha256(raw).hexdigest())
renamed = dict(original, name='renamed/safe.bin')
reference = {key:renamed[key] for key in ('name', 'fixture', 'stage', 'design_version')}
args = dict(inventory=dict(members=[renamed], expected_fixture_ids=['unit'], required_stages=['seven_step']),
    references=[reference], member_bytes={renamed['name']:raw},
    expected_fixture_versions={'unit':'v1'}, evidence_stage='seven_step')
baseline_args = deepcopy(args)
baseline_args['inventory']['members'][0]['name'] = original['name']
baseline_args['references'][0]['name'] = original['name']
baseline_args['member_bytes'] = {original['name']:raw}
baseline_wire = bundle.encode_replay_json(**baseline_args)
wire = bundle.encode_replay_json(**args)
assert wire['ok'] and baseline_wire['ok']
assert bundle.replay_bundle_json(wire['text']) == bundle.replay_bundle_json(baseline_wire['text'])
for stale, expected_exit in [(False,0), (True,4), (False,0)]:
    candidate = json.loads(wire['text'])
    if stale: candidate['references'][0]['name'] = original['name']
    completed = subprocess.run([sys.executable, '-P', '-B', str(Path(bundle.__file__).resolve()), '--replay'],
        input=json.dumps(candidate).encode('ascii'), capture_output=True, timeout=15)
    assert completed.returncode == expected_exit and completed.stderr == b''
    assert len(completed.stdout.splitlines()) == 1
    result = json.loads(completed.stdout)
    assert result['semantic']['verdict'] == ('UNREADABLE' if stale else 'PASS')
    assert not result['origin_authenticated']
    if stale: assert result['integrity']['diagnostic_counts'] == {'REFERENCE_DANGLING':1}
    renaming_exits.append(completed.returncode)
identity_exits = []
for repaired, expected_exit in [(False,4), (True,0)]:
    members, references, payloads = [], [], {}
    alpha = json.dumps(fixture_record('alpha', 'v1'), sort_keys=True).encode('ascii')
    for fixture in ('alpha', 'beta'):
        # Initially both paths contain exactly the same alpha evidence.
        raw = (json.dumps(fixture_record('beta', 'v1'), sort_keys=True).encode('ascii')
               if repaired and fixture == 'beta' else alpha)
        name = fixture + '/seven.json'
        member = dict(name=name, fixture=fixture, stage='seven_step', design_version='v1',
                      byte_length=len(raw), sha256=sha256(raw).hexdigest())
        members.append(member)
        references.append({key:member[key] for key in ('name','fixture','stage','design_version')})
        payloads[name] = raw
    if not repaired:
        assert payloads['alpha/seven.json'] == payloads['beta/seven.json']
        assert members[0]['sha256'] == members[1]['sha256']
    encoded = bundle.encode_replay_json(
        dict(members=members, expected_fixture_ids=['alpha','beta'], required_stages=['seven_step']),
        references, payloads, expected_fixture_versions={'alpha':'v1','beta':'v1'}, evidence_stage='seven_step')
    assert encoded['ok']
    completed = subprocess.run([sys.executable, '-P', '-B', str(Path(bundle.__file__).resolve()), '--replay'],
        input=encoded['text'].encode('ascii'), capture_output=True, timeout=15)
    assert completed.returncode == expected_exit and completed.stderr == b''
    result = json.loads(completed.stdout)
    assert result['integrity']['integrity_verified'] and not result['origin_authenticated']
    assert result['semantic']['fixtures']['alpha']['verdict'] == 'PASS'
    if not repaired:
        assert result['semantic']['fixtures']['beta']['result']['reason'] == 'bundle_evidence_fixture_conflict'
    else: assert result['semantic']['verdict'] == 'PASS'
    identity_exits.append(completed.returncode)
assert not any(name == 'pytest' or name.startswith('tests.') for name in sys.modules)
print(json.dumps(dict(cli_exits=observed, renaming_cli_exits=renaming_exits,
    equal_hash_identity_exits=identity_exits, standard_library_builder=True, origin_authenticated=False), sort_keys=True))
```

Stop on any mismatch. Each child has a 15-second deadline. `subprocess.run`
kills and waits for a timed-out child. No file extraction, browser, network,
service or product state is created. A PASS here proves synthetic replay only,
never live X05 acceptance or origin authentication.

The final `HASH_CORRECT_FAIL` case is a semantic-corruption replay. It changes
one saved string from five optimizers to six and computes fresh member pins.
The auditor accepts those bytes, but step 3 fails against the unchanged
independent synthetic oracle. Expected exits for the extended example are
`0,1,4,1`. Origin remains unauthenticated in all four cases.

The same executable example also renames the inventory member, its reference
and its payload-map key together. It requires the full replay result to equal
the original. Leaving the reference at its old path returns named
`REFERENCE_DANGLING` uncertainty, and restoring the new path recovers PASS.
Expected renaming exits are `0,4,0`. This is a path-identity test, never archive
extraction or evidence-origin authentication.

The equal-hash identity replay gives two distinct fixture paths identical
pinned alpha evidence. Integrity passes for both, while beta returns named
`bundle_evidence_fixture_conflict` uncertainty. Repairing beta's explicit
evidence bindings and repinning its bytes recovers PASS. Expected identity
exits are `4,0`; equal hashes never establish a fixture's identity or origin.

## Offline inventory comparison

Run this standard-library-only example with `python -P -B`. The helper counts
change facts and changed members separately. A member with both new pins and
new ownership contributes two facts. Examples share one sorted cap of 32.
The CLI accepts a strict JSON object with explicit `before` and `after`
inventory envelopes through `--inventory-diff`. It returns exit 0 for equal
inventories, 1 for readable differences, 4 for unreadable input and 5 for an
output-delivery failure. `--replay` and `--inventory-diff` are exclusive.

```python
from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys

repo = Path('D:/GitHub/xbench-x05-late-subscribers')
sys.path.insert(0, str(repo / 'tools/xbench'))
from benches import x05_bundle as bundle

def inventory(fixtures):
    members = [dict(name=fixture + '/saved.bin', fixture=fixture, stage='saved',
        design_version='v1', byte_length=1, sha256='0' * 64) for fixture in fixtures]
    return dict(members=members, expected_fixture_ids=list(fixtures), required_stages=['saved'])

before = inventory(['alpha', 'beta', 'removed'])
after = inventory(['alpha', 'beta', 'added'])
after['members'][0]['sha256'] = '1' * 64
after['members'][1]['design_version'] = 'v2'
preserved = deepcopy((before, after))
result = bundle.diff_inventories(before, after)
assert result['ok'] and result['difference_total'] == result['changed_member_total'] == 4
assert result['difference_counts'] == dict(added=1, removed=1, repinned=1, reassigned=1)
assert result['differences'] == [dict(name='added/saved.bin', kind='added'),
    dict(name='alpha/saved.bin', kind='repinned'), dict(name='beta/saved.bin', kind='reassigned'),
    dict(name='removed/saved.bin', kind='removed')]
assert (before, after) == preserved
assert not result['integrity_verified'] and not result['origin_authenticated']
equal = bundle.diff_inventories(before, deepcopy(before))
assert equal['inventory_equal'] and equal['difference_total'] == 0
assert not equal['origin_authenticated']
observed = []
for pair, expected_exit, verdict in [(dict(before=before, after=before), 0, 'PASS'),
    (dict(before=before, after=after), 1, 'FAIL'),
    (dict(before=before, after={'SYNTHETIC_PRIVATE_DIFF':True}), 4, 'UNREADABLE')]:
    completed = subprocess.run([sys.executable, '-P', '-B', str(Path(bundle.__file__).resolve()), '--inventory-diff'],
        input=json.dumps(pair).encode('ascii'), capture_output=True, timeout=15)
    assert completed.returncode == expected_exit and completed.stderr == b''
    assert len(completed.stdout.splitlines()) == 1
    decoded = json.loads(completed.stdout)
    assert decoded['verdict'] == verdict and not decoded['origin_authenticated']
    assert b'SYNTHETIC_PRIVATE_DIFF' not in completed.stdout
    observed.append(completed.returncode)
print(json.dumps(dict(change_total=result['difference_total'], cli_exits=observed, origin_authenticated=False), sort_keys=True))
```

Inventory equality compares caller-supplied metadata. Authenticating its
producer requires independently trusted source evidence. Stop on any mismatch.
Each child has a 15-second deadline and terminates before the summary. The
example retains its caller inputs and creates no service or browser state.

## A-to-B-to-A inventory replay

This standard-library-only example records both hops and the endpoint
comparison. Run with `python -P -B`.

```python
from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys

repo = Path('D:/GitHub/xbench-x05-late-subscribers')
sys.path.insert(0, str(repo / 'tools/xbench'))
from benches import x05_bundle as bundle

a = dict(expected_fixture_ids=['unit'], required_stages=['saved'], members=[dict(
    name='unit/saved.bin', fixture='unit', stage='saved', design_version='v1', byte_length=0, sha256='0'*64)])
b = deepcopy(a)
b['members'][0].update(sha256='1'*64, design_version='v2')
returned_a = deepcopy(a)
preserved = deepcopy((a,b,returned_a))
totals, exits = [], []
for before, after, expected_total, expected_exit in [(a,b,2,1),(b,returned_a,2,1),(a,returned_a,0,0)]:
    result = bundle.diff_inventories(before,after)
    assert result['ok'] and result['difference_total'] == expected_total
    assert result['changed_member_total'] == int(expected_total > 0)
    assert result['difference_counts'] == dict(added=0,removed=0,
        repinned=int(expected_total > 0),reassigned=int(expected_total > 0))
    assert not result['origin_authenticated']
    completed = subprocess.run([sys.executable,'-P','-B',str(Path(bundle.__file__).resolve()),'--inventory-diff'],
        input=json.dumps(dict(before=before,after=after)).encode('ascii'),capture_output=True,timeout=15)
    assert completed.returncode == expected_exit and completed.stderr == b''
    assert len(completed.stdout.splitlines()) == 1
    decoded = json.loads(completed.stdout)
    assert decoded['difference_total'] == expected_total and not decoded['origin_authenticated']
    totals.append(result['difference_total'])
    exits.append(completed.returncode)
assert (a,b,returned_a) == preserved
print(json.dumps(dict(change_totals=totals,cli_exits=exits,origin_authenticated=False),sort_keys=True))
```

Endpoint equality establishes matching caller-supplied metadata. History and
origin require independently trusted records. The intermediate B inventory
records the repin and reassignment on each hop. Stop on any mismatch. Each
child has a 15-second deadline and terminates before the summary.
