# X07 offline schema compatibility matrix

Parent: Studio X-bench X07 injected customer profiles and confirmation journals.
Both paths are explicit local files. These rules neither authorize a customer
nor connect live persistence. Schema adoption preserves logical records; a
header upgrade can change file bytes.

## Per-store decisions

Apply each row independently to the profile and journal. An adapter needs both
stores to open successfully. Opening two files is not one atomic transaction:
the first store may initialize before the second refuses. Do not infer that a
refused journal undid an earlier profile initialization or confirmation.

| Profile input | Writable | Read-only | Record invariant and evidence |
|---|---|---|---|
| Missing or empty version 0 | Initialize exact schema 1 | Refuse, do not create a missing file | No customer/version/pin rows are invented. [Initialization](../../server/tests/test_x07_profile_schema_evolution.py), [read-only missing path](../../server/tests/test_x07_profile_readonly_store.py). |
| Known exact legacy shape, version 0 | Adopt header 1 | Refuse without adoption | Existing profile, roles and pins unchanged. [Legacy writable adoption](../../server/tests/test_x07_profile_schema_evolution.py), [read-only legacy refusal followed by adoption](../../server/tests/test_x07_profile_readonly_store.py). |
| Exact supported version 1 | Open | Open for reads and existing pinned reuse | Old pins retain their original version; new writes refuse in RO. [Read-only store](../../server/tests/test_x07_profile_readonly_store.py), [actual JSX journey](tests/x07_readonly_store_journey_dom.cjs). |
| Unknown version-0 shape or damaged version-1 shape | Refuse, no repair | Refuse, no repair | Do not clobber rows or restore missing triggers silently. [Schema evolution](../../server/tests/test_x07_profile_schema_evolution.py), [semantic tampering](../../server/tests/test_x07_schema_semantic_tampering.py). |
| Future version, tested with 99 | Refuse | Same version guard refuses | Rejected store bytes unchanged. Writable future-version readback is exercised by [schema evolution](../../server/tests/test_x07_profile_schema_evolution.py); the RO path shares the same pre-adoption guard in `CustomerProfileStore.__init__`. |

| Journal input | Writable | Read-only | Record invariant and evidence |
|---|---|---|---|
| Missing or empty version 0 | Initialize exact confirmations table and header 1 | Refuse, do not create a missing file | No receipt success fabricated. [Initialization races](../../server/tests/test_x07_receipt_schema_init_races.py), [RO missing path](../../server/tests/test_x07_receipt_readonly_replay.py). |
| Known exact legacy confirmations table, version 0 | Adopt header 1 | Replay known request while retaining header 0 | Scope, digest and full cached result must validate. [Legacy RO replay](../../server/tests/test_x07_receipt_readonly_replay.py) proves unchanged legacy bytes; writable header adoption follows the constructor's explicit `version == 0 and not read_only` branch. This is a journal-only exception, not profile RO adoption. |
| Exact supported version 1 | Produce or replay | Replay existing request; refuse unseen request | RO refusal precedes profile production. [Read-only replay/recovery](../../server/tests/test_x07_receipt_readonly_replay.py), [actual JSX journey](tests/x07_readonly_store_journey_dom.cjs). |
| Unknown table/DDL, extra objects or added triggers | Refuse, no repair | Refuse, no repair | Guard checked at construction and under every run transaction. [Trigger tampering](../../server/tests/test_x07_receipt_trigger_tampering.py), [journal corruption](../../server/tests/test_x07_receipt_journal_corruption.py). |
| Future version, tested with 99 | Refuse | Same version guard refuses | Rejected journal bytes unchanged. [Future schema](../../server/tests/test_x07_receipt_journal_corruption.py); RO shares the same version guard in `ConfirmationReceiptStore.__init__`. |

The matrix distinguishes executable fixture coverage from shared source-guard
reasoning. It does not claim every Cartesian combination has a separate test.
For example, profile 1 plus journal legacy 0 can be read-only, while profile
legacy 0 plus journal 1 cannot: the profile refuses before any profile adoption.
Both legacy stores may adopt independently in writable mode; that is not a
cross-file all-or-nothing migration guarantee.

## Backup generations and committed records

| Restored generations | Decision | Unchanged records / executable evidence |
|---|---|---|
| Receipt references a version and project pin present in restored profile store | Scoped replay succeeds | Result must equal pinned roles and recounted selection. [Matching generation and newer latest version](../../server/tests/test_x07_receipt_restore_divergence.py). |
| Receipt exists but referenced profile version/pin is absent | Refuse replay, no silent repair | Neither file rewritten. [Independent mismatched backups](../../server/tests/test_x07_receipt_restore_divergence.py). |
| Restored project belongs to another customer | Refuse without foreign payload disclosure | Foreign pin and both files retained. [Foreign restored generation](../../server/tests/test_x07_receipt_restore_divergence.py). |
| IDs match but cached roles/counts/snapshot metadata are inconsistent | Refuse cached success | Valid IDs alone do not validate a receipt. [Same-scope tampering](../../server/tests/test_x07_receipt_same_scope_tampering.py). |
| Profile and pin committed, receipt insert/COMMIT failed | Inspect and apply existing pin, do not reconfirm | A rollback in the journal does not roll back the profile. [Commit gap](../../server/tests/test_x07_receipt_commit_gap.py), [COMMIT denial](../../server/tests/test_x07_receipt_commit_authorizer.py), [length limit](../../server/tests/test_x07_sqlite_length_limits.py). |
| Both settled stores backed up and restored to new explicit paths | Fresh scoped editor load and same-request replay | Original stores, old pins and foreign scope retained. [Actual restored-store JSX](tests/x07_restored_store_journey.cjs). |

SQLite online backups, WAL snapshots and checkpoint recovery are exercised by
[active-transaction backup](../../server/tests/test_x07_backup_active_transaction.py),
[receipt WAL snapshots](../../server/tests/test_x07_receipt_wal_snapshot.py), and
the bounded [journal WAL growth tool](x07_journal_wal_growth.py). Its smoke mode
measures actual WAL bytes, receipt rows and pinned versions before/after a real
checkpoint. It is synthetic local evidence, not a deployment durability claim.

## Replay

Run the linked Python files through the existing offhost pytest rail. Run these
standalone checks separately from the repository root:

```text
node tools/xbench/tests/x07_status_announcements_dom.cjs --deps C:/tmp/x07-web-check-20261115/node_modules
node tools/xbench/tests/x07_bridge_working_directory.cjs --deps C:/tmp/x07-web-check-20261115/node_modules
python tools/xbench/x07_journal_wal_growth.py --smoke
```

Use disposable stores and new output paths. Do not rewrite older receipts to
claim a later schema or source. No production, credential change or deployment
is part of this matrix.
