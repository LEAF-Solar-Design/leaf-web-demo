# X07 source and staging acceptance matrix

Parent: Studio X-bench X07 drawing roles and reusable customer profiles.
The adapter and JSX callbacks are offline building blocks. They are not wired
to shared workspace routes or live customer authorization. No external check
is run by this matrix. Synthetic receipts cannot establish staging PASS.

| Frozen step | Adapter/service entry | Actual JSX callback | Local executable synthetic check | Required source-bound LIVE evidence |
|---|---|---|---|---|
| 1 First choice and one confirmation | `load_workflow`, `confirm_roles` | `loadWorkflow`, `confirmRoles` | `x07_adapter_jsx_journey.cjs`, proposal contract tests | Real first drawing names the correct block/layer automatically, then accepts one confirmation on the delivered source. |
| 2 Competing sets, one picked window, oracle count | Injected copy candidates and `confirm_roles`/`apply_profile` | Confirmation followed by `X07CopyPicker` | Adapter recovery tests, selection metamorphic tests, JSON journey | Real competing sets show counts/locations; one set/window pick gives the frozen597 oracle. |
| 3 Visible editable versioned customer profile | `load_profile`, `save_profile(expected_version)` | `loadProfile`, `saveProfile` in injected editor | Adapter snapshot/editor tests; JSON journey edits to version2 | Authenticated project customer binding, persisted profile id/version and rendered edits on staging. |
| 4 Same customer's next two drawings reuse roles | `load_workflow`, `apply_profile` | Compatible plan skips confirmation | Synthetic journey, bound adapter reuse and scope tests | Two new real projects reuse the same customer map with zero edits and frozen counts. |
| 5 Other customer's next drawing, one copy pick | Explicit aliases and compatible copy candidates | `applyProfile` from one pick | Alias compatibility tests and adapter reuse checks | Real anonymous block correspondence and the frozen510 count after at most one pick. |
| 6 No cross-customer applied map | Bound scope check plus scoped store read/apply | Context-keyed workflow rejects foreign receipt | Scope matrix, payload tests, fresh-process isolation | Both real cross-customer directions propose fresh and exclude the other profile id from applied records. |
| 7 Edits retain old project versions | Immutable version insert, exact project pin | Editor save leaves `applicationRecord` unchanged | Restart durability, schema/failure checks, actual JSON journey/failures | Edit on delivered source makes a new version; old real projects retain their original version. |

Run local commands from repository root, one suite at a time:

```powershell
python -P -m pytest server/tests/test_x07_process_restart_durability.py server/tests/test_x07_selection_duplicate_identity.py server/tests/test_x07_profile_schema_evolution.py -q
node tools/xbench/tests/x07_adapter_jsx_journey.cjs --deps web/node_modules
node tools/xbench/tests/x07_adapter_jsx_failures.cjs --deps web/node_modules
python tools/xbench/x07_workflow_soak.py --smoke
```

The JavaScript checks compile actual JSX and call Python through UTF-8 JSON
stdin/stdout. SQLite files are disposable. Negative fixtures inject only a save
or pin failure; positive recovery uses the real adapter/store. The failure
journey keeps editor input, reloads inspect after a saved-version pin failure,
applies without confirming again, and verifies both customers' old pins.

Schema1 is recorded in SQLite `user_version`. A new/empty database initializes
atomically. A known complete unversioned legacy shape is explicitly adopted;
version1 reopens as supported. Future versions, unknown unversioned shapes or
missing immutability triggers refuse without repairing or changing file bytes.
Initialization executes statements in one transaction; injected partial DDL
failure leaves no tables or version marker. This is not an automatic migration
framework or authorization to touch a live database.

INSERT selection counts each valid record once. A repeated nonempty handle in
the matching block/layer is ambiguous and refuses before returning a count,
including conflicting coordinates outside the picked window. Handles match
case-insensitively. Coincident records with distinct handles are distinct inserts.
Missing/empty/non-string IDs supply no identity and are counted as records;
coordinates alone never collapse them. Independent fixture oracles check these
rules. This qualifies the earlier record-count boundary for ambiguous IDs.

The soak defaults to200 bounded cycles and supports at most1000. Smoke uses20.
It alternates explicit customers, edits optional roles, reopens pins and samples
Python allocations. Cancellation here means abandoning a read-only inspect
before save/apply, not a UI interruption. Controlled-promise JSX checks prove
UI response retirement separately. Memory samples make no leak or latency claim.

Published code, selected CI, critic approval, synthetic fixtures and delivered
semantic evidence are separate states. The frozen grader alone reports the
seven-step staging verdict, bound to its observed source. The preserved valid
FAIL on source1f41fad2 identifies a missing visible proposal; later source-only
success does not turn it into PASS. Canonical delivery and a fresh real-drawing
grade remain necessary for LIVE acceptance. Real drawing bytes stay outside Git.
