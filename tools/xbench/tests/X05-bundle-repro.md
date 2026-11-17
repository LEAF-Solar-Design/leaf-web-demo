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
