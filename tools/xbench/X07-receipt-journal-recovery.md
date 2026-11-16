# X07 offline confirmation recovery

This service supports the X-bench X07 customer drawing profile workflow. The host injects both local database paths and an authorized customer, project and drawing context. It has no shared route or default journal path.

Profile versions, project pins and confirmation receipts have separate commits. A reply is not a transaction boundary.

| Durable state at restart | Decision | Expected records for the target customer/project |
| --- | --- | --- |
| Neither profile nor pin committed | Inspect again before explicit confirmation | No new version or pin |
| Profile committed, pin absent | Inspect and apply the compatible saved version | One saved version, then one pin |
| Profile and pin committed, journal absent | Inspect and apply the existing pin; confirmation refuses | One version and one pin, no new receipt |
| Journal committed, reply lost | Retry the identical request against the same drawing snapshot | Same receipt, one version and one pin |
| Journal committed, retry payload changed | Refuse the conflict | All earlier records retained |
| Journal schema or stored receipt invalid | Refuse without repair or foreign payload disclosure | Existing database bytes retained |

The journal serializes independent connections with a bounded SQLite busy timeout. Scope and request digest are checked before decoding stored results. A replay must match its injected context and current project pin. A caller cannot use a different customer or drawing as a recovery shortcut.

For synthetic reproduction from the repository root:

```powershell
python -P -m pytest server/tests/test_x07_receipt_commit_gap.py server/tests/test_x07_receipt_retry_races.py server/tests/test_x07_receipt_journal_corruption.py server/tests/test_x07_confirmation_receipt_loss.py -q -p no:cacheprovider
node tools/xbench/tests/x07_bridge_duplicate_json_keys.cjs --deps C:/tmp/x07-web-check-20261115/node_modules
node tools/xbench/tests/x07_scope_identity_collisions_dom.cjs --deps C:/tmp/x07-web-check-20261115/node_modules
```

The crash fixture waits for the profile/pin commit, then terminates its own child before journal insertion. Restart proves inspect/apply recovery and retains an unrelated old pin. Race fixtures use separate adapters and journal connections. Malformed and foreign receipts use disposable stores. DOM fixtures compile the actual JSX and use local Python stdin/stdout only. All fixture children and databases close before cleanup.

These checks prove bounded offline recovery. They do not establish distributed exactly-once delivery, live authorization, shared persistence or staging acceptance.
