# X07 visible role card recipes

Parent is Studio X-bench X07 drawing role discovery. These recipes run the actual JSX with synthetic clients. Run them from the checkout's `web` directory with its installed test dependencies, one suite at a time.

First choice without a user action and bounded competitor pages

```powershell
npm run test:unit -- --maxWorkers=1 --no-file-parallelism src/solar/DrawingRoleProposal.test.jsx
```

The first case displays Synthetic Panel on MODULES, count 7 and bounds 0.00, 0.00, 6.00, 12.00. The large response has 203 sets. Each page mounts at most ten data rows and keeps Panel 0 as first choice while pages change locally. Previous and Next are native keyboard-focusable buttons. Malformed rows produce a short reading refusal with Retry reading roles. Recovery uses one pending request and preserves generic feedback. Client replacement, authority retirement and StrictMode cases abort old reads and exclude their labels.

Workspace catalog failure through the real scoped project client

```powershell
npm run test:unit -- --maxWorkers=1 --no-file-parallelism src/solar/DrawingRoleProposalWorkspace.test.jsx
```

The first-choice sentence and count 7 remain visible with a null tool catalog. There is one context GET and no command button. Switching projects excludes the old context. Invalid or retired scopes issue no read.

One explicit confirmation through injected callbacks

```powershell
npm run test:unit -- --maxWorkers=1 --no-file-parallelism src/solar/X07DrawingRoleReview.test.jsx src/solar/X07RoleConfirmation.test.jsx
```

Review Panel on MODULES has three inserts and bounds 0, 0, 2, 2. Click Confirm roles once. The synthetic callback receives the selected row, captured authority, drawing binding and cancellation signal. A second submit while pending makes no second callback. Successful settlement displays Roles confirmed. Retirement aborts pending reads and confirmation signals and excludes late success.

The exported review component requires a host-supplied confirmation callback. Studio's current workspace mounts the read-only role card. Its authenticated customer-profile save callback and connected review mount remain to be built. These recipes establish the injected component contract. Staging behavior, customer counts, cross-project profile reuse and frozen seven-step acceptance still require their own receipts.

For browser proof, render the actual workspace and scoped client with the same disposable synthetic context at 1440 by 900. Wait for Drawing roles, verify the block, layer, count and bounds fit, then close the owned browser and support server. Preserve source identity, screenshot, capture manifest and cleanup receipt beside the recipe. Recording is unnecessary for these checks.
