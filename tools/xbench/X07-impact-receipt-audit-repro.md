# X07 offline impact and receipt reference replay

Parent: Studio X-bench X07 customer profiles. Run from the repository root with
Python 3. This uses two synthetic customers and disposable stores. Save the
following Python block to a temporary script and run `python -P <script>`.
No service, browser, customer drawing or credential is required.

```python
import json
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path.cwd()
sys.path.insert(0, str(root / 'tools/xbench'))
from x07_profile_change_impact import CustomerProfileStore, preview_change
from x07_receipt_history_audit import ConfirmationReceiptStore, audit_receipts
from x07_profile_workflow import ProfileWorkflow

with tempfile.TemporaryDirectory(prefix='x07-replay-') as scratch:
    profile, journal = Path(scratch)/'profile.sqlite', Path(scratch)/'journal.sqlite'
    store = CustomerProfileStore(profile)
    receipts = ConfirmationReceiptStore(journal)
    workflow = ProfileWorkflow(store)
    pins = {}
    for customer, block in [('a', 'A'), ('b', 'B')]:
        context = dict(customer_key=customer, project_key='old-'+customer,
                       drawing_key='drawing-'+customer)
        intake = {'inserts': [{'name': block, 'layer': 'M', 'x': 1, 'y': 1}]}
        result = receipts.run(context, '0'*64,
            lambda: workflow.confirm(intake, **context,
                roles={'module': {'block': block, 'layer': 'M'}},
                window=[0, 0, 2, 2], expected_version=0), lambda value: None)
        pins[customer] = result['applied_profile']
    saved = store.save_version('a', {'module': {'block': 'A2', 'layer': 'M'},
                                     'roof': {'layer': 'R'}}, expected_version=1)
    read = CustomerProfileStore(profile, read_only=True)
    journal_read = ConfirmationReceiptStore(journal, read_only=True)
    before = (profile.read_bytes(), journal.read_bytes())
    preview = preview_change(read, customer_key='a', profile_id=saved['profile_id'],
                             before_version=1, after_version=2)
    checked = audit_receipts(read, journal_read, customer_key='a')
    common = ['--database', str(profile), '--customer', 'a']
    commands = [(['x07_profile_change_impact.py', *common, '--profile-id',
                  saved['profile_id'], '--before', '1', '--after', '2'], preview),
                (['x07_receipt_history_audit.py', *common, '--journal', str(journal)], checked)]
    for argv, expected in commands:
        child = subprocess.run([sys.executable, str(root/'tools/xbench'/argv[0]),
                                *argv[1:]], capture_output=True, text=True, timeout=10)
        assert child.returncode == 0 and child.stderr == ''
        assert json.loads(child.stdout) == expected
    assert (profile.read_bytes(), journal.read_bytes()) == before
    assert all(read.application_record('old-'+key, key) == pin for key, pin in pins.items())
    assert read.latest('a')['version'] == 2
    assert read.application_record('old-a', 'a')['version'] == 1
    print(json.dumps({'added': preview['added'], 'changed_roles': sorted(preview['changed']),
        'retaining': preview['retaining_different_roles'], 'receipt_issues': checked['issues'],
        'pins_preserved': True}, sort_keys=True))
```

The deterministic projection printed by the script is exactly:

```json
{"added": {"roof": {"layer": "R"}}, "changed_roles": ["module"], "pins_preserved": true, "receipt_issues": {"inconsistent_references": 0, "invalid_receipts": 0, "missing_references": 0}, "retaining": [{"different_roles": ["module", "roof"], "project_key": "old-a", "version": 1}]}
```

Full preview JSON also contains the explicit customer/profile IDs, before/after
versions, role values and schema `leaf.x07.profile-change-impact.v1`. Generated
profile IDs and timestamps are not part of the deterministic projection.
Preview exits 0 on success and 2 when scoped versions or safe input are
unavailable. Receipt JSON uses `leaf.x07.receipt-history-audit.v1`, explicit
customer, receipt count, `healthy` and the three issue counts above. Its exits
are 0 for clean, 1 for findings and 2 for unavailable/budget/SQLite refusal.
Operational receipt refusal is exactly
`{"error": "AUDIT_UNAVAILABLE", "schema": "leaf.x07.receipt-history-audit.v1"}`
as a parsed JSON object. It never includes a partial `healthy` result.

Receipt budgets are 262144 UTF-8 bytes per payload, 32 container levels
(root container is level one), and 10000 containers/values excluding member
keys. A malformed or nonfinite receipt within those budgets is a finding;
budget exhaustion refuses the whole audit. Foreign customer payloads are not
decoded or charged to the target's row/payload budget. Audit rows cap at10000.

Both tools read snapshots and leave old project pins unchanged. Profile and
receipt databases have separate snapshots. Supply settled stores; concurrent
writes do not have a distributed atomic-snapshot guarantee. Receipt audit
checks stored version/pin/resolved-role references. It does not replay geometry,
verify request digests or establish drawing identity or authorization.

On a lock or interrupt, preserve the stores, release the owned lock or remove
the owned handler, then retry explicitly with fresh read-only connections.
For malformed payloads, invalid references or budgets, retain the finding and
have the data owner supply corrected evidence. These tools never repair it.
Never retry confirmation to repair an audit result or move an old project pin.
