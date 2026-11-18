# X07 offline workflow contract

Parent: Studio X-bench X07, drawing role discovery and reusable customer profiles.
This is a source integration contract, not staging acceptance. The service and
composed UI have no shared route, default database, network client or identity
resolver. All examples and checks use synthetic inputs.

## Inputs and identity

`ProfileWorkflow(store)` receives an injected `CustomerProfileStore` using an
explicit local SQLite file. Each operation receives drawing intake plus explicit
`customer_key`, `project_key` and `drawing_key`. Keys are case-sensitive, nonempty,
trimmed application keys of at most 200 characters. `drawing_key` includes the
immutable drawing version. Intake uses the existing INSERT schema, without
replacing project, drawing, material or conversation identity.

A future live adapter must take customer scope from the authenticated project's
organisation/customer binding and authorize that binding. A browser field, CAD
label, filename, block alias or caller-provided customer claim is not identity
proof. The offline service does not provide authentication or authorization.
Anonymous aliases are explicit drawing-specific correspondences, never customer
identifiers. JSON transport must retain these values exactly.

## Service events and outputs

1. `inspect(intake, customer_key=..., project_key=..., drawing_key=...,
   block_aliases=...)` is read-only. It checks an existing project pin first,
   otherwise the latest scoped profile. It returns a compatibility decision,
   optional fresh proposal, current application record and `expected_version`.
   Missing or incompatible roles require fresh confirmation and do not create a
   pin. A foreign project's key raises a scope error.
2. `confirm(..., roles=..., window=..., expected_version=...)` is the explicit
   role-confirmation event. Roles must exist in this drawing. An explicitly
   confirmed anonymous name is allowed as the current literal name. Invalid
   roles/windows refuse before any write. The service saves a new version using
   expected-version comparison, then pins that exact version. Confirmation of an
   already pinned project refuses; use `apply` to reopen its existing pin.
3. `apply(..., window=..., block_aliases=...)` rechecks compatibility, counts the
   supplied copy window, and pins exactly the checked version. An incompatible
   drawing returns `needs_confirmation` with no applied profile. A concurrent
   profile edit refuses pinning rather than silently applying an unchecked map.

All windows use inclusive XY INSERT-origin bounds, no tolerance, and each input
record counts once. Independent `oracle_count` in copy choices is evidence for
the user, not a product count or a source of customer scope. Automatic copy
segmentation is outside this contract: the caller supplies verified copy bounds.

An applied output has schema `leaf.x07.profile-workflow.v1`, state `applied`,
the three context keys, `applied_profile` (scoped immutable project record),
`resolved_roles` (including this drawing's aliases), and `selection` (actual
count, window and INSERT indices). Saved canonical roles and resolved drawing
roles are separate. Old project pins remain at their original version after an
edit. A same-customer new project uses the latest compatible version.

Save and pin are two durable transactions. If a pin fails after a successful
save, the saved version remains. Retry by inspecting and applying that version,
not by blindly replaying confirmation. Expected-version refusal requires an
explicit reload/review. This contract does not promise atomic save-plus-pin or
cross-process cancellation of an already committed write.

## Injected UI adapter

`X07ProfileWorkflow` receives `customerKey`, `projectKey`, `drawingKey` and these
callbacks. None is implemented as a network operation here:

| Callback | Input and result |
|---|---|
| `loadWorkflow(context, {signal})` | Service inspect envelope plus `copies`, each containing block, layer, finite `design_window`, independent `oracle_count`. |
| `confirmRoles(context, choice, {signal})` | Explicit block/layer and window with `expected_version`; adapter builds a role map, calls service confirm, returns an applied envelope. |
| `applyProfile(context, choice, {signal})` | Explicit copy window; adapter calls service apply and returns an applied envelope. |
| `loadProfile(customerKey, {signal})` | Latest scoped profile for the injected editor. |
| `saveProfile(customerKey, roles, {expected_version, signal})` | New version or a rejected `STALE_VERSION`/`VERSION_CONFLICT` error. |
| `onApplied(envelope)` | Invoked only after current-context receipt validation. |

Fresh proposals use the confirmation component. More than one matching copy
then requires one explicit pick before saving. A compatible profile bypasses
role confirmation; one supplied copy applies automatically, multiple copies
require a pick. No copies leaves the picker empty rather than inventing bounds.
Applied results mount the customer profile editor when load/save callbacks exist.
The editor keeps edits on stale refusal, reloads the latest version explicitly,
and retries using that version while retaining the old project's visible pin.

Context changes remount the workflow. Cancellation, drawing switches and unmount
abort pending signals and retire responses. A producer must honor the captured
context and signal before mutating its target. Aborting cannot undo an external
write already committed. A late receipt cannot invoke `onApplied`, populate the
next drawing or change its pin. Foreign customer/project/drawing receipts refuse.

## Local checks and remaining live evidence

Run from the repository root, one suite at a time:

```powershell
python -P -m pytest server/tests/test_x07_profile_compatibility.py server/tests/test_x07_profile_json_roundtrip.py -q
node tools/xbench/tests/x07_workflow_races_dom.cjs --deps web/node_modules
python tools/xbench/x07_profile_store_perf.py --smoke
```

| Contract | Local check |
|---|---|
| Two-customer confirmation, reuse, copy counts and no intake mutation | `WorkflowTests.test_two_customers_confirm_reuse_and_choose_copy` |
| No inferred identity or unchecked pin on version drift | Workflow missing-profile, foreign-project and change-between-check-and-pin tests |
| Old pins and stale confirmations | Workflow old-project and stale-confirmation tests, existing store concurrency/failure tests |
| Unicode labels, aliases and immutable transport records | All four `test_x07_profile_json_roundtrip.py` tests |
| Three-component composition, compatible reuse and explicit copy | `happyPaths` in the workflow DOM check |
| Pending loads/saves retired on context switch, cancel and unmount | Manually resolved promises in `races`, including rejected foreign receipts and StrictMode |
| Scoped latest lookup and pinned reads at seeded sizes | Disposable profile-store performance smoke, correctness assertions per read pair |

The new Python tests belong to the dedicated `server-x07-offline` suite. The
performance command is optional and not registered in CI. DOM checks use actual
JSX in jsdom, close the DOM in `finally`, and forbid fetch. They prove no browser,
provider or staging behavior.

All seven frozen LIVE steps still require source-bound staging evidence: proposal
and confirmation on the first real drawing, competing set/window count, visible
editable scoped profile, the two same-customer reuses, the other customer's copy
pick, applied-record isolation in both directions, and retained old-project
versions after editing. Shared route/UI integration and canonical staging
delivery are still required. Synthetic examples do not replace those receipts.
