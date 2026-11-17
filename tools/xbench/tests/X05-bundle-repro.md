# X05 offline bundle integrity replay

Parent: Studio X-bench X05 evidence reconciliation. This library checks supplied
evidence inventories and bytes, without files, archive extraction, transport or
live grading. A member declares `name`, `fixture`, `stage`, `byte_length`,
`sha256` and `design_version`. Expected fixture IDs, stages and versions come
from the caller. Exactly one inventory member covers each expected slot.

Names are relative ASCII segments. Slash and backslash compare as `/`; absolute,
drive, UNC, empty, dot and parent segments are rejected. The default limits are
128 members, 1 MiB per member and 8 MiB cumulative. Invalid size metadata stops
the byte auditor before payload access. Payloads must be supplied `bytes`.
Results contain fixed diagnostic codes and indices, exact per-code counts and
totals, and at most 32 diagnostic examples. Byte results retain every member.
Indices refer to caller member/reference order, or sorted normalized payload
names for foreign payloads. No supplied payload or raw filename is echoed.

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
