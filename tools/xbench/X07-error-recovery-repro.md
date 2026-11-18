# X07 offline error recovery

Parent: Studio X-bench X07 profile workflow. These source-only reproductions use
synthetic drawings and disposable SQLite. No shared route, live authorization,
customer data, network or staging acceptance is exercised.

Run from repository root, one suite at a time:

```powershell
python -P -m pytest server/tests/test_x07_adapter_payload_failures.py server/tests/test_x07_adapter_snapshot_isolation.py server/tests/test_x07_workflow_recovery.py -q
node tools/xbench/tests/x07_adapter_jsx_journey.cjs --deps web/node_modules
python tools/xbench/x07_adapter_init_perf.py --smoke
```

The JSX journey runs the actual composed React component. Its callbacks launch
Python with local JSON on stdin, a temporary database, and the real adapter.
Python errors return type, message and optional error code over stdout JSON.
The script rejects network access and removes only its own disposable database.
It requires React, Testing Library, jsdom and esbuild in the injected dependency
directory, plus Python on PATH. No browser is launched.

| Error | Reproduction and preserved state | Safe retry |
|---|---|---|
| Invalid copy window | `AdapterPayloadTests.test_null_scalar_and_malformed_choices_refuse_deterministically` submits a window outside injected candidates. Customer rows, all versions and pins stay equal to their pre-call snapshot. | Reload scoped copy candidates and choose one exact window. Never expand a window silently. |
| Oracle mismatch | `test_oracle_mismatch_and_invalid_candidate_shape_refuse_before_write` injects a wrong independent count. Adapter construction refuses before any profile write. | Correct the independent fixture/window evidence, then construct a new adapter. Do not replace the oracle with the product count. |
| Stale editor version | `AdapterSnapshotTests.test_stale_editor_has_code_and_keeps_records_then_explicit_reload_recovers` saves version2, then retries expected_version1. It raises `STALE_VERSION`, retaining all records. | Keep edits, explicitly reload latest version, review, then save using that version. Editing makes a new immutable version and never rewrites old project pins. |
| Saved version, failed pin | `AdapterRecoveryTests.test_pin_failure_leaves_saved_version_then_inspect_apply_recovers` injects a pin error after a version save. Saved version1 survives; no pin exists. Stale confirmation refuses. | Inspect again, observe compatible version1, then apply its selected copy. Do not confirm again and create a duplicate version. |

`load_profile(customer_key)` and `save_profile(customer_key, roles,
expected_version=...)` are bound editor callbacks. They refuse a different
customer and a project already owned by another customer. Optional roof and
obstruction roles must exist in the injected drawing; an explicit edit may
confirm a current anonymous block literal. Later drawings still need aliases.
The future host must authorize context before adapter construction. The adapter
does not authenticate a browser's customer claim.

Snapshot tests mutate caller intake, aliases, copies and returned plans. The
adapter retains its injected snapshot. Reopening a pinned project uses the old
canonical map and resolved aliases, even after editing the latest customer map.
Malformed versions, including booleans, refuse without writes.

The source replay is separate from the seven frozen LIVE steps. Those still
need canonical delivery and source-bound staging evidence. Performance output
is an observation, not a live responsiveness threshold.
