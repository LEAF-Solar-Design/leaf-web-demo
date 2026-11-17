# X07 confirmation acknowledgment recipes

Parent is Studio X-bench X07 drawing role discovery. These recipes exercise actual JSX with injected scope and callbacks, using synthetic data and disposable SQLite stores.

From the checkout's `web` directory, run the acknowledgment and pagination controls

```powershell
npm run test:unit -- --maxWorkers=1 --no-file-parallelism src/solar/X07DrawingRoleReview.test.jsx src/solar/X07RoleConfirmation.test.jsx
```

Null, undefined, scalar, missing-ok, nonboolean-ok and rejected acknowledgments leave the selected choice available for retry. Only an object carrying `ok: true` confirms. A callback receives the selected block, layer, count and design_window, followed by options containing signal, authorityKey and a copy of solarBinding. A rejected attempt does not display Roles confirmed.

With 23 synthetic choices, at most ten radio controls mount per page. Select Choice 4, move to the second page and confirm. The callback still receives Choice 4. Page changes make no context request. Duplicate block/layer identities with conflicting counts or bounds refuse before the form offers confirmation. Distinct identities remain selectable.

A callback replacement leaves the pending operation bound to the callback captured at submit. After a failed acknowledgment, only a new explicit click invokes its replacement. Removing the callback before submit disables confirmation. Authority retirement aborts the pending signal and prevents late success from populating a new scope.

From the checkout root, run the actual review-to-adapter journey

```powershell
node tools/xbench/tests/x07_role_review_adapter_journey.cjs --deps web/node_modules
```

The journey reads a role proposal through the existing local JSON bridge, presses Confirm roles once and submits to the real Python WorkflowAdapter. Its injected intake contains three Panel inserts on MODULES in window 0, 0, 2, 0. An independent count checks the returned selection. The callback checks captured authority and project/drawing scope. Direct SQLite queries require exactly one customer version, one project pin and one confirmation journal row. Mutation negative controls require the immutable-version and pinned-project triggers to refuse updates. All workers, JSX state and owned temporary stores settle in finally.

The journey is also a test in X07DrawingRoleReview.test.jsx, so the first command executes it. Python and the installed web dependency directory must be available. The callback acknowledgment proves this injected component contract. Authenticated customer-profile persistence and mounting the review in shared Studio remain pending. Live drawing counts and frozen seven-step acceptance require separate source-bound staging receipts.
