# Parity receipts

One file per capability and fixture, at `receipts/<capability>/<fixture-id>.json`.
The directory name is the capability, and a receipt that declares a different
capability than the folder it sits in is a hard error, not a miss.

A receipt is a comparator's word that the Branch2025 plugin and Leaf Automation
Studio produced the same result for one fixture. `scripts/solar_parity_status.py`
reads them and will not accept anything it cannot fully parse.

```json
{
  "schema": "leaf.solar-parity-receipt.v1",
  "capability": "draw-array",
  "capability_version": "1",
  "fixture": { "id": "fx-flat-roof-01", "sha256": "<64 hex>" },
  "plugin": { "build": "2026.9.1", "state": "committed", "receipt_sha256": "<64 hex>" },
  "studio": { "capability_version": "1", "engine": "browser" },
  "comparator": { "name": "geometry", "version": "1", "verdict": "pass", "diffs": [] },
  "synthetic_fields": [],
  "fallback_fields": [],
  "synthetic_flagged": false,
  "survived_reopen": true,
  "produced_at": "2026-09-17T00:00:00Z"
}
```

Every field is required and every one is checked:

- `capability_version` must match the ledger row's version, and `studio.capability_version`
  must match the receipt's. A version bump stales every receipt written before it.
- `plugin.state` must be `committed`. A receipt from a dirty or uncommitted plugin
  tree proves nothing anyone can reproduce.
- `survived_reopen` must be true: the result has to survive a save and reopen, not
  just look right in the session that made it.
- `synthetic_fields` or `fallback_fields` may be non-empty only when
  `synthetic_flagged` is true. Substituted data that is not declared is the failure
  this rule exists to catch.
- `comparator.verdict` is `pass` or `fail`. `diffs` carries the field names that
  differed, and is empty on a pass.

One clean passing receipt settles a capability, however many ledger rows share it.
A failing receipt beside a clean one is kept: it is history, not a blocker.

Receipts are committed, never generated at check time, and the tree is bounded at
20000 files with 2 MB per receipt.
