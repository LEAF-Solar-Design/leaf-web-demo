# X07 offline refusal and recovery contract

Parent: Studio X-bench X07 customer role profiles. These are injected offline
services and synthetic bridge checks. They do not authorize a customer, connect
a live persistence route or establish staging acceptance.

## Unknown outcomes versus confirmed rollback

A bridge failure describes delivery. It does not establish whether a store write
committed. Keep the draft, inspect the scoped latest version and project pin,
then make an explicit recovery decision. Never turn a missing reply into a blind
confirmation or an unconditional new version. The editor sends its expected
version; a stale retry refuses, and explicit reload can retain the draft before
a later save of new edits.

`x07_editor_lost_save_reply_dom.cjs` commits version 2 and discards its reply.
A stale retry adds no version. Explicit reload keeps newer roof edits; an
explicit save then creates version 3. The old project remains pinned to 1.
`test_x07_commit_authorizer_refusal.py` and
`test_x07_store_interrupt_rollback.py` instead prove rollback with database
readback after actual SQLite failures. They do not generalize every transport
failure into a rollback receipt.

The profile store and confirmation journal are separate databases. A failed
journal insert can follow a committed profile and pin. Inspect and apply the
existing pinned version. Do not silently reconfirm. See
`test_x07_receipt_sqlite_full.py` and `test_x07_receipt_commit_gap.py`.

## Adapter and store refusals

| Signal | State and next action | Synthetic check |
|---|---|---|
| `STALE_VERSION` | Draft retained. Reload version, keep edits, then explicitly save if still wanted. | `x07_editor_lost_save_reply_dom.cjs` |
| `INTAKE_BUDGET_EXCEEDED` | Construction refuses before snapshot copying. Supply bounded intake. | `test_x07_intake_budgets.py` |
| `ProfileScopeError` | Requested scope refused. Do not substitute or disclose another customer's profile. | `test_x07_receipt_scope_namespace.py` |
| `ValueError`, unsupported confirmation journal schema | Refuse before producer or replay, including triggers added after construction. Restore a known valid disposable journal. | `test_x07_receipt_trigger_tampering.py` |
| `ValueError`, retry payload or snapshot changed | Keep existing versions and pins. Reload the drawing plan and make a new explicit choice. | `test_x07_receipt_request_identity.py` |
| `ValueError`, invalid or inconsistent receipt / pin mismatch | No replay success, write or silent repair. Reconcile the independently restored stores. | `test_x07_receipt_same_scope_tampering.py`, `test_x07_receipt_restore_divergence.py` |
| `SQLITE_INTERRUPT`, `SQLITE_AUTH` in the tested transaction | Readback proves rollback. Clear the local callback, then explicitly retry. Old pins remain. | `test_x07_store_interrupt_rollback.py`, `test_x07_commit_authorizer_refusal.py` |
| `SQLITE_FULL` during journal insert | Profile and pin may already be committed. Inspect/apply recovery, not reconfirmation. | `test_x07_receipt_sqlite_full.py` |
| `SQLITE_BUSY` before confirmation | Tested journal lock refusal occurs before profile mutation. Release owned lock, then retry. | `test_x07_receipt_lock_recovery.py` |

Adapter guards often use a plain `ValueError` with a bounded message, not a
machine enum. Do not invent a retry code from the message or expose raw cached
payloads in a diagnostic.

## Local bridge codes

| Codes | State and recovery | Synthetic checks |
|---|---|---|
| `BRIDGE_EOF`, `BRIDGE_EXIT` | Missing receipt or failed process. No success callback, even after valid reply bytes. Commit outcome needs scoped readback. | `x07_editor_lost_save_reply_dom.cjs`, `x07_bridge_reply_then_failure.cjs` |
| `BRIDGE_TIMEOUT`, `BRIDGE_STDOUT_LIMIT`, `BRIDGE_STDERR_LIMIT` | Stop and await the owned worker. Retain draft; inspect before retrying a write. | `x07_bridge_resource_limits.cjs` |
| `BRIDGE_CANCELLED` | Cancellation is explicit, but may follow a commit. Retire replies; reconcile stored state before writing again. | `x07_bridge_abort_boundaries.cjs`, `x07_editor_pending_reset_dom.cjs` |
| `BRIDGE_STDIN_FAILURE` | One delivery failure, owned child closed. The EPIPE fixture proves no producer call; other outcomes need readback. | `x07_bridge_stdin_epipe.cjs` |
| `BRIDGE_STARTUP` | The local interpreter could not start. No producer ran; retain the draft, correct the interpreter and retry explicitly. Invalid executable loader exits can instead return `BRIDGE_EXIT`. | `x07_bridge_spawn_failure.cjs` |
| `BRIDGE_UTF8`, `BRIDGE_DUPLICATE_JSON_KEY`, `BRIDGE_MALFORMED_JSON`, `BRIDGE_JSON_DEPTH` | Receipt/request invalid. No successful callback. Preserve draft and inspect if a write could have run. | `x07_bridge_json_grammar.cjs`, `x07_bridge_duplicate_json_keys.cjs`, `x07_bridge_extra_frames.cjs` |
| `BRIDGE_FIXTURE` | Invalid synthetic fixture setup. Correct the check; do not interpret it as a product rollback. | `x07_adapter_jsx_journey.cjs` |

## Report codes

Report refusals emit valid JSON with seven `UNREADABLE` steps and exit 2. They
do not upgrade old receipts. Inputs remain unchanged. A requested output can
contain the new UNREADABLE report; atomic publication failures retain the prior
complete output. An output alias refuses before replacement.

| Codes | Recovery and retained evidence |
|---|---|
| `MISSING_RECEIPT`, `MALFORMED_RECEIPT`, `DUPLICATE_RECEIPT_KEY`, `NONFINITE_RECEIPT_VALUE`, `RECEIPT_INTEGER_BUDGET_EXCEEDED` | Supply fresh bounded valid receipts. Keep original bytes. |
| `INVALID_JSON_BUDGET`, `JSON_STRUCTURE_BUDGET_EXCEEDED`, `INVALID_PROOF_BUDGET`, `MISSING_OR_EXCESS_PROOFS` | Correct explicit limits or the proof list. Excess proof count refuses before receipt reads. |
| `INVALID_SOURCE`, `MISSING_SOURCE_BINDING`, `SOURCE_MISMATCH` | Use the receipt's exact source or produce a fresh receipt. Never rewrite an older source claim. |
| `UNREADABLE_SOURCE_ROOT`, `MISSING_FILE_DIGESTS`, `INCOMPLETE_SOURCE_INVENTORY`, `MALFORMED_FILE_DIGEST`, `MISSING_SOURCE_FILE`, `UNREADABLE_SOURCE_FILE` | Supply the complete measured source inventory at the explicit root. |
| `SOURCE_PATH_ESCAPE`, `SOURCE_PATH_NOT_CANONICAL`, `AMBIGUOUS_SOURCE_IDENTITY` | Use one canonical in-root path per physical file in each receipt. Do not resolve by silently choosing an alias. |
| `SOURCE_DIGEST_MISMATCH`, `SOURCE_CHANGED_DURING_AGGREGATION` | Freeze matching bytes and link targets, then generate fresh evidence. Preserve the refused receipts. |
| `RECEIPT_CHANGED_DURING_AGGREGATION` | Journey or proof bytes changed between their read and the final consistency pass. Freeze a complete receipt set and rerun; no mixed synthetic success. |
| `INVALID_SOURCE_BUDGET`, `SOURCE_READ_BUDGET_EXCEEDED` | Correct the bounded source set or explicit budget. No partial synthetic success. |
| `MALFORMED_JOURNEY`, `MALFORMED_STEPS`, `MALFORMED_ASSERTIONS`, `MISSING_OR_UNKNOWN_ASSERTIONS`, `INCONSISTENT_STEP`, `MALFORMED_PROOF` | Repair the synthetic producer/fixture and execute it again. Do not patch historical evidence. |
| `OUTPUT_ALIASES_INPUT_OR_SOURCE`, `UNREADABLE_OUTPUT_PATH`, `REPORT_PUBLICATION_FAILED` | Choose an independent writable output and retry publication. Preserve inputs and any prior complete output on failed replacement. |

Executable report checks: `test_x07_report_cli_integrity.py`,
`test_x07_report_numeric_tokens.py`, `test_x07_report_proof_budget.py`,
`test_x07_report_path_identity.py`, `test_x07_report_link_retarget.py`,
`test_x07_report_proof_order.py`, and `test_x07_report_error_disclosure.py`.

## Replay

From the repository root, run each JSX check separately:

```text
node tools/xbench/tests/x07_editor_lost_save_reply_dom.cjs --deps C:/tmp/x07-web-check-20261115/node_modules
node tools/xbench/tests/x07_bridge_reply_then_failure.cjs --deps C:/tmp/x07-web-check-20261115/node_modules
```

Run the listed Python files through the existing offhost pytest rail with
`server/tests` on the import path. All paths here name synthetic tests. These
commands require no private drawing, production, credential change or deployment.
