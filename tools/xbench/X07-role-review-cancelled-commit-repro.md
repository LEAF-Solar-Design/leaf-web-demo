# X07 cancelled commit recovery

Parent is Studio X-bench X07 drawing-role and customer-profile continuity. The fixture uses actual JSX, injected scope, the local Python WorkflowAdapter bridge and disposable profile/receipt SQLite stores.

From the checkout root with installed web dependencies and Python, run

```powershell
node tools/xbench/tests/x07_role_review_adapter_journey.cjs --deps web/node_modules
```

The same journey runs from the focused component test

```powershell
cd web
npm run test:unit -- --maxWorkers=1 --no-file-parallelism src/solar/X07DrawingRoleReview.test.jsx src/solar/X07RoleConfirmation.test.jsx
```

The fixture confirms customer A's three modules through actual role review and opens the composed profile editor. Add roof role, set its layer to ROOF and save. The store creates profile version 2 while the first project keeps version 1. Two later drawings of A auto-apply version 2 with independent counts 4 and 5 and zero repeated confirmations.

Customer B proposes fresh with no A profile identifier. After B's own confirmation, its next drawing has two copies. One explicit Use copy 2 applies B's profile and independently counts three inserts. A's profile and old pin remain unchanged.

For cancellation recovery, a fresh synthetic customer C confirms three modules. The fixture waits until the adapter has committed the profile, pin and receipt journal, then withholds the callback reply. Click Cancel while the UI still waits. The UI hides the late success and explains that saved changes may have completed. Inspect the same scoped project, verify its existing version 1 pin, and apply that profile. The confirmation callback count remains one. No second confirmation creates a version or changes an old pin.

Direct SQLite audit requires versions A1/A2/B1/C1, the six expected project pins and three confirmation receipts. It checks the immutable-version and pinned-project trigger refusals. All JSX, Python workers and fixture stores settle in finally.

Cancelling before submission has a separate Read roles again action. It rereads the same scope, makes no confirmation callback or store write on cancel, and requires a fresh explicit click. Private synchronous or rejected callback errors produce the fixed message Roles could not be saved. Try again. A failed acknowledgment retains the choice.

The 10,000-set test measures block-label reads during local paging. Candidate preparation is memoized and selection lookup uses an identity map. Each page mounts at most ten radios, retains its selected row and submits once. Proposal objects are immutable snapshots from the scoped role reader.

These are offline component and store receipts. The profile composition still requires authenticated host callbacks and a connected Studio mount. Live customer drawing counts and frozen seven-step acceptance remain pending.
