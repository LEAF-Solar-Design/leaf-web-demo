# Unified surface end-to-end execution plan

Date: 2026-07-24

Status: active. The cat flow is one deterministic proof case. It is not proof
that the unified product works end to end.

Independent review: Claude Opus 5 Extra High, profile `opus-critic`, run
`20260724-175500-3934d73d`, verdict `REVISE`. The worker completed with exit 0
and proved `actualModels: claude-opus-5`. Root accepted the stricter proof
ladder, controller extraction, motion fixes, and six-wave sequence.

## Objective

Put every supported Leaf operator capability into one standards-based surface,
remove duplicate frontend state machines, and prove each capability through the
browser. If a required capability has no working backend seam, mark it missing,
add the seam, and keep it open until its proof passes.

## Evidence levels

Each capability advances through these states. A capability cannot skip a state.

1. `FOUND`: implementation and source-of-truth path identified.
2. `INTEGRATED`: the unified surface calls that production controller or API.
3. `CONTRACT_PROVEN`: deterministic browser test verifies the UI contract.
4. `LOCAL_E2E_PROVEN`: the browser uses the real local web and server stack.
5. `STAGING_PROVEN`: the same flow passes against an authorized production-like
   staging tenant and records immutable source and runtime identity.

`MISSING` means the product or API seam does not exist. Mock data may prove a UI
contract, but it cannot advance an item to `LOCAL_E2E_PROVEN`.

## Product rules

- `/try` becomes the unified product shell. `/app` may remain as a temporary
  compatibility route, but both routes must mount the same controllers.
- One controller owns each state domain. No cat-only copy of jobs, versions,
  conversation, selection, or run state survives integration.
- One persistent three-column grid holds projects and tools, the drawing and
  command bar, and events and jobs. Details overlays the right rail without
  changing the grid.
- The command bar is the only command composer and the only ingest drop target.
- The UI uses one green accent. Amber and red represent real states only.
- Every chevron resolves to a real view. Every destructive action uses the
  defined confirmation level. Loading, empty, error, retry, keyboard, reduced
  motion, and responsive states require browser coverage.

## Capability traceability ledger

The status column records the evidence available at plan creation. `APP ONLY`
means the capability exists in the legacy `/app` composition but is absent from
the unified scene. `PARTIAL` means the unified scene contains a deterministic
fixture path but not the production controller.

| ID | Capability | Source of truth | Unified target | Initial status | Required browser proof |
|---|---|---|---|---|---|
| ID-01 | Sign in, sign out, session expiry | `web/src/auth.js`, `getSession` | shell trust chrome | APP ONLY | authenticated, signed-out, expired-session recovery |
| ID-02 | Organization, project, workspace selection | project and org APIs, `ProjectSwitcher` | left rail and trust chrome | APP ONLY | create, switch, empty, denied, refresh persistence |
| ID-03 | Drawing open and intake | project open and intake APIs | center stage | APP ONLY | open drawing, loading, failure, retry, canonical version |
| ID-04 | Manifest or drawing upload | no frontend API seam | command-bar drop catcher | MISSING | real upload, validation failure, sandbox notice, drawing opens |
| CA-01 | Tool catalog and families | tools and capabilities APIs, `ToolsPanel` | left rail | APP ONLY | load, family filter, tool detail, empty, retry |
| CA-02 | Natural-language routing | `nlPrompt`, `RoutePanel` | command bar and proposal strip | APP ONLY | route, alternatives, no match, service failure |
| CV-01 | Conversation session and turns | `web/src/converse.js`, `ConversePanel` | center ledger | PARTIAL | create, reattach, stream, poll fallback, refresh recovery |
| CV-02 | Read and write approval | converse approval API | proposal strip and ledger | PARTIAL | approve, deny, stale approval, exact arguments |
| RN-01 | Run, build, solve dispatch | run and job APIs | command bar and ledger | PARTIAL | read run, write run, build, solve, failure, retry |
| AU-01 | Author, stage, publish, use tool | author APIs, `AuthorPanel` | guided center flow | APP ONLY | stage, independent decision, publish, catalog refresh, use |
| JB-01 | Job list and status | jobs APIs, `JobRail` | right rail | PARTIAL | queued, running, complete, failed, selected detail |
| JB-02 | SSE, poll fallback, reattach | converse stream and job attach APIs | ledger and right rail | PARTIAL | stream loss, fallback, reload reattach, duplicate suppression |
| VW-01 | Drawing viewer and visible layers | `Viewer`, `Legend` | center stage | PARTIAL | layer visibility, resize, empty, render failure |
| VW-02 | Entity selection and readout | `SelectionReadout` | stage and details | APP ONLY | select, clear, hidden entity, version change |
| VR-01 | Versions and immutable history | versions APIs, `VersionHistory` | right rail and details | PARTIAL | list, preview, return to head, parent relation |
| VR-02 | Checkout ownership | checkout APIs, `CheckoutControls` | trust chrome | APP ONLY | take, conflict, release, expired lock |
| VR-03 | Undo and redo | undo and redo APIs | stage controls and command path | PARTIAL | undo, redo, unavailable state, refresh truth |
| AC-01 | Claude account grant | grant APIs, `ClaudeAccountPanel` | account or trust surface | APP ONLY | link, unlink, required, denied, expired |
| EN-01 | Entitlements | entitlements API, `EntitlementGate` | contextual gate | APP ONLY | allowed, blocked, refresh after change |
| EN-02 | Quota and spend | usage API, `QuotaCard` | details or account surface | APP ONLY | normal, near limit, exhausted, unavailable |
| HL-01 | Health and degraded mode | health API, `DegradedBanner` | shell banner | APP ONLY | healthy, degraded, retry, recovery |
| OP-01 | Tenant operations | ops APIs, `OpsDrawer` | standalone ops entry or drawer | APP ONLY | list, select, disable with confirmation, denied role |
| NT-01 | Notices, errors, retry, details | toast, banners, strips, drawers | whole shell | PARTIAL | completed toast, ongoing banner, failure strip, details |
| KB-01 | Keyboard traversal and Escape | shared interaction rules | whole shell | UNPROVEN | tab order, focus return, Escape, named back link |
| HP-01 | Hints, keycaps, and coach | design-system micro standards | whole shell | UNPROVEN | hover/focus hints, shortcut labels, first-run coach |
| RS-01 | Responsive layouts | design-system breakpoints | shell | UNPROVEN | desktop, narrow desktop, tablet, no hidden primary action |
| AX-01 | Accessibility | semantic UI and focus rules | whole shell | UNPROVEN | axe scan, accessible names, live status, contrast |
| MO-01 | Motion and reduced motion | motion standard | stage and overlays | PARTIAL | allowed transitions, stable grid, zero duration preference |
| DS-01 | Visual tokens and calm rules | design-system docs and CSS | whole shell | PARTIAL | screenshot assertions for accent, status, typography, density |
| RS-02 | Resolvers and details overlay | chevrons, `DetailsDrawer` | right overlay | APP ONLY | every resolver opens a destination, no grid reflow |
| DC-01 | Dock projects, events, and keys | Dock Console standard | dock or ops domain | MISSING DECISION | product-owner mapping, then browser proof for accepted scope |

### Granular behavior pins found by independent review

These rows prevent broad capability rows from hiding important failure paths.

| ID | Behavior | Initial status | Required proof |
|---|---|---|---|
| ID-04A | Auth callback and all legacy boot parameters | APP ONLY | one browser boot test per callback and parameter |
| ID-04B | 401 gate and bounded demo fallback | APP ONLY | local 401, poll-stop, and recovery proof |
| CA-03 | Flat catalog fallback and retry ladder | APP ONLY | deterministic empty/error plus local recovery |
| RN-02 | Staged run intent, confirmation, and idempotency binding | APP ONLY | unit behavior pin plus local double-submit negative |
| JB-03 | Page-hide close beacon | APP ONLY | local browser beacon assertion |
| JB-04 | Escape interrupt and sequence detach | APP ONLY | keyboard proof while a job keeps running |
| JB-05 | Inflight persistence after browser reload | APP ONLY | reload mid-run and reattach once |
| VR-04 | Undo and redo at arbitrary depth | PARTIAL | three-version chain, two undos, two redos |
| NT-02 | Single-fire R ladder and fall-through | APP ONLY | behavior pin for every priority target |
| AX-02 | Run and result live-region announcements | APP ONLY | accessibility tree and announcement proof |
| MO-02 | No fill-mode snap under reduced motion | CONTRACT_PROVEN | forced reduced-motion result pane remains painted |
| MO-03 | Micro entrances use scale, never translation | CONTRACT_PROVEN | CSS pin plus visual proof |
| DM-01 | Guided tour drives production handlers | APP ONLY | deterministic tour without copied actions |
| FT-01 | Fetch budget and timeout seam | FOUND | unit pin and slow-response browser proof |

Review fact: the name `arrange-panels-as-cat` is present in tests and fixtures,
but the independent review did not find a registered runnable tool. The cat row
therefore stays at contract proof until a real catalog and run proves otherwise.

### Ingest contract found after review

The server already exposes a real DWG and DXF upload lane:

- `GET /api/site/guest-upload-policy`
- `POST /api/drawings/upload` with multipart field `file`
- `GET /api/drawings/{drawing_id}/upload-status`
- `GET /api/drawings/{drawing_id}/intake` after status becomes `ready`

The current “Drop manifest to ingest” copy is wrong. No user manifest-upload
contract exists. The unified surface must say DWG or DXF and derive accepted
types, size, and retention from the policy response. Guest identity uses the
server-issued `X-Guest-Session`; authenticated uploads use the active server
tenant. Project association, cancel/delete, and numeric extraction progress are
missing backend capabilities and remain separate rows.

Focused backend evidence at investigation time: guest upload, guest auth,
fail-closed, and broker resolver suites passed, 83 tests in 10.22 seconds.

### Local real-stack profile

Use isolated ports and stores per run: web `5275`, app `8230`, broker `8240`,
harness `8250`. Start with `scripts/start-leaf.py --with-harness` and these
explicit substitutions:

- `APS_LIVE=0`: local pure-Python engine, not Autodesk APS.
- `LEAF_AGENT_MOCK=1`: harness fake agent, not Claude.
- `LEAF_AUTH_LIVE=0`: local tenant header, not Auth0.
- SQLite stores and in-process job pools, not PostgreSQL or the canonical worker.

The local runner must use a new temporary store directory, require strict broker,
harness, app health, app readiness, and web readiness, forbid requests to
`leaf-proof.invalid`, and forbid Playwright API fulfillment. Its first flows are
real drawing plus catalog, real read run plus job SSE and reload reattach, and a
real write plus version persistence, undo, and redo. Receipts must name every
substitution and runtime mode.

## Test matrix

Every ledger row gets stable test IDs and evidence in
`artifacts/unified-surface-proof/<capability-id>/`.

| Tier | Runtime | External substitutions allowed | Claim |
|---|---|---|---|
| Contract | Vite plus deterministic API | all remote dependencies | UI behavior only |
| Local E2E | real local web, app, server, stores, worker | Claude and APS only when labelled | application integration |
| Staging | deployed immutable revision and test tenant | none for the claimed path | production-like proof |

Each proof writes a receipt with the capability ID, URL, source commit, runtime
mode, API endpoints called, assertions, screenshots, video, and terminal result.
The aggregate walk is useful for review, but the receipts and assertions are the
acceptance evidence.

## Architecture

Create one `WorkspaceProvider` with domain controllers rather than moving the
legacy component into a new visual shell in one large edit:

- `identityWorkspace`: auth, org, project, drawing intake, checkout.
- `catalogRouting`: tools, families, NL route, alternatives, entitlements.
- `conversationExecution`: session, turns, approvals, run intent, authoring.
- `jobs`: dispatch, SSE, polling, reattach, retry, detail selection.
- `drawingState`: viewer data, selection, versions, preview, undo, redo.
- `operations`: health, quota, spend, grant, tenant operations, notices.

Both `/app` and `/try` consume these controllers during migration. Once all
ledger rows are integrated, `/app` redirects to the unified shell, not the
reverse. Deterministic fixtures replace controller transports, not controllers.

Root decision after Opus review: the routes must share one live session because
the requested surface keeps all action in one persistent scene. One provider
therefore sits above route presentation and owns one viewer, one job stream, one
conversation session, and one version head. The alternative `<App shell>` mount
would preserve behavior faster but would retain duplicated runtime ownership,
so it is rejected for this product requirement.

The smallest safe extraction is the conversation session controller. Drawing
versions and jobs follow serially because both touch central `App.jsx` state.
Catalog, workspace, platform status, Claude grant, checkout, and operator chrome
can then move into separate modules with disjoint ownership.

## Execution waves

### Wave 0: proof ladder, inventory, and behavior pins

Owner: root.

- Freeze this ledger and map every current `/app` action and standard.
- Keep the existing intercepted tests under an explicit fixture tier.
- Add a local tier with a real server and no `page.route` interception.
- Add a production-like tier driven by one deployed base URL.
- Add route smoke tests that fail if `/try` navigates to `/app`.
- Add a proof receipt schema and per-capability artifact folders.
- Pin Escape interruption, the single-fire R ladder, quota freshness,
  poll-stop-on-401, tour cancellation, and inflight reattach.
- Record existing failing gates separately from new regressions.

Gate: every capability has one owner, target, evidence level, and proof test.

### Wave 1: behavior-neutral shared-controller extraction

This wave is serial because `App.jsx` is the single source that owns the current
behavior. Land unused controller modules first, then convert `App.jsx` in one
revertible integration change. Preserve all sequence refs, freshness checks,
DOM classes, accessible names, and handler order.

Gate: `/app` passes the unchanged fixture suite and all behavior pins. `/app`
and `/try` then use the same controller instances and API transports. The cat
fixture works only by swapping the transport layer.

Progress:

- The conversation controller is extracted and provided above both `/app` and
  `/try`; stale-session retry is browser-proven.
- `/try` now uses the shared job lifecycle with SSE plus poll fallback, durable
  pointer, reattach, beacon, and sequence guards instead of its 30-poll loop.
- `/try` now uses the shared drawing-version controller and derives Undo and
  Redo availability from `head` and `latest` instead of literal version guards.
- `/app` now uses the same job lifecycle controller as `/try`. The unchanged
  fixture suite proves submit, attach, durable reattach, Escape detach, job rail,
  result seating, and negative terminal states.
- `/app` now uses the same drawing-version controller as `/try`. The `/app` cat
  proof creates version 2, seats it in the viewer, undoes to version 1, and makes
  redo available. Both surfaces use controller-owned `canUndo` and `canRedo`.
- Catalog, workspace, and platform-trust controllers are extracted behind stable
  interfaces and now own the corresponding `/app` state. Their 22 focused tests,
  the 10-test fixture suite, the managed real-stack smoke, production build,
  behavior pins, customization checks, and all four staging-fix checks pass.
- `/try` now mounts the registered catalog, immutable run-intent review, shared
  job rail, version history and read-only preview, backend health, Claude grant
  kind, usage cap, and entitlement gate in the persistent three-column scene.
  The aggregate contract walk covers a catalog read run followed by proposal,
  approval, cat write, version preview, trust refresh, undo, and redo. The
  10-test fixture suite, focused trust lifecycle tests, production build,
  behavior pins, customization checks, and all staging-fix checks pass.
- The shared platform-trust adapter is now safe under React Strict Mode's
  setup-cleanup-setup lifecycle. This fixed a real state-loss defect found by
  the aggregate browser walk.
- `/try` now mounts the real project resolver and workspace hydration summary.
  A selected canonical drawing version is bound into the immutable run intent,
  project runs carry both scope headers, and terminal runs refresh the project
  job ledger. Org and project creation use inline named forms, not native
  prompts or no-argument controller calls.
- `/try` now mounts the protected authoring lifecycle. The contract walk stages
  a non-runnable tool, proves pending independent review creates no register
  request, publishes only the exact receipt-bound decision, refreshes the
  catalog, expands schema defaults into the immutable intent, confirms the
  authored run, and completes it through the shared job controller.
- `/try` now mounts the production viewer layer and selection controls. The
  contract walk proves a visible canvas change when PANELS is hidden and proves
  real pointer picking of panel P1696 through the shared selection readout.
- `/try` now mounts checkout take and release against an extracted controller.
  The controller ignores stale drawing reads, enforces mutation single-flight,
  refreshes authoritative state after each mutation, and blocks write approval
  when another holder owns the lock. Its four focused tests pass.
- `/try` now mounts Claude account unlink and link against the shared platform
  controller. The contract walk proves destructive confirmation, a write-only
  token field, token-field removal after success, and the expected OAuth grant
  request without recording a real credential.
- The aggregate one-scene walk passes with production Playwright video capture.
  Its receipt covers 24 capability IDs and records every observed API method-path.
  The 14-scenario fixture gate, 22 controller tests, production build, behavior
  pins, customization checks, and all staging checks pass.
- `/try` now mounts the shared result panel, completion toast, provenance drawer,
  historical job selection, running status, polite live region, and result
  overlays on the resident viewer. Structured terminal failures remain result
  envelopes instead of being flattened into transport errors.
- `/try` now uses the server's existing DWG/DXF upload contract. Both the
  accessible picker and command-bar drop target call one controller that reads
  public policy, validates type and size, posts multipart bytes, polls honest
  extraction state, loads intake with guest identity, and seats the drawing.
  Three focused tests cover success, preflight rejection, and late-result
  suppression after cancel. The aggregate video proves both entry paths.
- `/try` now binds identity from the actual session `{tenant, org, tier}`
  response, keeps Auth0 callbacks on `/try`, renders a calm 401 gate, disables
  execution while sign-in is required, and exposes account details separately
  from the Claude grant. Two focused browser contracts cover the 401 gate and
  callback route ownership.
- `/try` now has explicit desktop, narrow-desktop, tablet, phone, and
  short-height layouts. The proof checks both rails, the command input, and Run
  remain visible without page overflow, and that phone input text stays at
  16 px. Ctrl or Command K focuses the real command bar, both tablists use a
  roving tab stop with arrow keys, visible input focus is restored, and errors
  use alert semantics. Five viewport screenshots and two browser tests pass.
- `/try?ops=1` now keeps the unified scene and mounts the existing internal
  tenant drawer. Browser proof covers authorized list and disable, cancel before
  mutation, exact secret-header scoping, no secret leakage, calm 403 denial,
  hidden tenant data on denial, and zero ops requests without the flag.
- `/try` now exposes structured failure code, message, and retryability in the
  result and details surfaces. Retry stages a fresh immutable proposal and
  cannot submit until the operator approves again. Browser proof checks a new
  idempotency key, successful recovery, unchanged drawing state on failure, and
  the absence of Retry for a nonretryable failure.
- `/try` now uses one latched platform-session controller. Any observed API or
  conversation 401 clears the stored token, dismisses mutable proposals, stops
  the conversation and upload work, returns both rails to the session gate, and
  disables execution. Browser proof covers initial 401, mid-session checkout
  expiry, stopped job polling, sign-out without Auth0 build flags, token removal,
  and callback route ownership. Two controller tests cover latch and sign-out
  ordering.
- Drawing state now lives in the persistent workspace provider instead of the
  scene cast, and the provider is no longer keyed by route. Browser proof creates
  cat version 2, recasts `/try` to the site and back, then verifies the same
  controller instance, version head, single submission, unchanged session-read
  count, and resident viewer.
- A successful drawing write now remains a successful receipt when its head
  refresh fails. `/try` keeps the previous drawing visible, shows one alert with
  Retry, and uses the controller's head-read recovery without approving or
  submitting the write again. The focused browser proof injects one 503, then
  recovers version 2 with one submission.
- Submit transport failures now use the same fresh-review recovery path as
  structured terminal failures. Browser proof aborts the first run request,
  verifies Retry submits nothing, then confirms a second proposal with a new
  idempotency key and a successful result.
- The unified scene now exposes named main and complementary landmarks, a named
  live status region, and visible keyboard focus. Proposal Escape dismisses the
  proposal before scene navigation. Details and ops dialogs trap focus, consume
  Escape before the scene ladder, and restore their opener. Four focused browser
  contracts cover these semantics and the combined ops proof caught and fixed a
  real double-Escape navigation defect.
- Ordinary language in `/try` now passes through the shared catalog router
  before it can reach Claude. High-confidence catalog matches stage immutable
  review, low-confidence and build requests carry the server classifier hint
  into Claude, transport loss falls back visibly to local catalog matching, and
  no match plus Claude quota loss remains a non-executing resolver. Four focused
  browser contracts prove request order, recovery, and zero preapproval runs.
- The shared quota and degraded-result notices now render in `/try` execution,
  preserve the result beneath them, and link quota state to freshly polled usage.
  Trust now shows a persistent degraded-backend notice with an explicit refresh.
  Three browser contracts cover daily quota, local solver fallback, details,
  health refresh, and zero job creation after preflight rejection.
- The resident tool viewer is now a named region with pan and zoom enabled,
  exposes the existing imperative Fit action in the View rail, and keeps the
  same canvas node when Fit restores drawing bounds. WebGL loss now leaves a 2D
  drawing visible with an operator-facing Retry action. Two browser contracts
  prove geometry restoration, canvas identity, fallback visibility, and retry.
- `/try?demo=tour` now opens a seven-step walkthrough on the unified scene
  instead of redirecting into the legacy console. It types the real cat request,
  routes before Claude, waits for explicit write approval and version 2, then
  opens version history and trust before exiting in place. The automated walk
  passes with a production Playwright video and a persistent Restart action.
- Both rail tablists now expose stable tab, panel, and label relationships with
  visible panel focus. Route resolver rows are focusable options with a roving
  tab stop, and arrow movement transfers focus to the active choice. The
  expanded accessibility, routing, responsive, and keyboard gates pass.
- The expanded serial fixture gate now passes 37 of 37 tests in 9.1 minutes.
  This aggregate run includes the full standards walk, guided cat video, all
  negative and recovery paths, session and ops gates, responsive and motion
  checks, viewer recovery, and the session controller contracts.
- The managed local proof now selects `count-by-layer` from the real catalog,
  stages the proposal without dispatch, confirms it once, receives a real 202
  job id, renders the completed result, reloads `/try`, and restores the
  completed job from the durable API and Jobs rail. The isolated run passed in
  14.0 seconds. APS, Claude, Auth0, PostgreSQL, mid-run reattach, Escape detach,
  and staging proof remain open.
- A second managed local browser test uploads a distinctive DXF, waits for real
  extraction, proves the seeded drawing was replaced by a one-panel scene,
  reviews `count-by-layer`, and confirms that the submitted run remains bound
  to the uploaded drawing id and account tenant. The two-test managed gate
  passes. Fourteen live-auth guest-session tests and five upload isolation tests
  also pass, including tampered and expired session denial, bearer precedence,
  cross-tenant denial, and refusal to bootstrap unknown guest drawings.
- The managed live-auth guest browser gate now passes. A signed-out operator can
  upload and inspect a real DXF, load version 1 through the signed guest drawing
  read path, and see the uploaded drawing id in the unified scene. Run stays
  disabled, the browser emits no run request, and a direct run attempt with the
  valid guest credential fails closed with 403. Guest authority is stored only
  for the server allowlisted uploaded-drawing reads and never reaches run, job,
  solve, author, or conversation calls.
- The managed local job gate now uses the guarded QA latency hook to hold a real
  broker job in the running state. The unified scene reattaches to its durable
  pointer, renders progress and the terminal result, and submits no duplicate
  run. Escape now detaches only the UI, clears the pointer, emits no close reap
  beacon, and leaves the same durable job to complete exactly once. Actual page
  close still owns the separate close-beacon path.
- The real page-hide gate now navigates away during a guarded slow job, observes
  one close beacon, and verifies that the orphan reaper fails the abandoned job
  once with the session-closed reason. The proof caught duplicate beacons from
  `visibilitychange` plus `pagehide`; the shared controller now deduplicates them
  by job id.
- The managed local version-depth gate now confirms the tracked
  `delete-marked-panel` write tool twice, creating a real `demo` chain from
  version 1 to 2 to 3. The unified surface then sends two Undo and two Redo
  mutations, walks the authoritative head from 3 to 1 and back to 3, and shows
  all three immutable versions in history. Exactly two run, two undo, and two
  redo requests are recorded.

### Wave 2: unified shell, resident viewer, motion, and responsive behavior

Mount one resident viewer in the unified shell. Wire picking, layers, selection,
fit, overlays, responsive breakpoints, the short-height command-bar fold, and
the full keyboard ladder. Replace reduced-motion `animation: none` with zero
duration and delay. Replace retired translated entrances with `scale(.985)`.

Gate: the viewer never remounts during a recast, every tested viewport remains
usable, and completed panes stay painted under forced reduced motion.

### Wave 3: capability panels

Render the existing catalog, project, author, checkout, version, entitlement,
quota, health, notice, details, and job components against the shared provider.
Reuse current components with additive props and safe defaults.

Gate: the local real stack proves catalog to confirm to run to receipt, plus
stage to independent publish to refreshed catalog. `/app` remains unchanged.

### Wave 4: shared run, job, conversation, and version state

Delete `/try`'s hand-written job polling and literal version 1/2 guards. Use the
shared SSE and poll fallback, inflight persistence, reload reattach, close
beacon, Escape detach, approval failure recovery, and arbitrary-depth history.

Gate: a local real-stack run survives reload, Escape detaches without canceling
the job, and a three-version chain undoes and redoes twice without double-run.

### Wave 5: missing ingest seam, full walks, and staging promotion

- Add the real upload adapter and remove the intentional ingest placeholder.
- Complete accessibility, hints, resolver, live-region, and visual suites.
- Run one aggregate browser walk that shows the supported actions in the same
  persistent scene and records video.
- Run each capability proof independently to make failures diagnosable.
- Promote only authorized rows to staging and attach tenant, identity, source
  commit, image digest, and rollback evidence.

Gate: the completion audit has no `APP ONLY`, `PARTIAL`, `MISSING`, or
`UNPROVEN` row in accepted product scope.

## Critical path

1. Fixture, local, and production-like proof ladder with behavior pins.
2. Shared controllers and transport injection.
3. Unified shell, resident viewer, capability panels, and shared execution.
4. Missing upload seam and trusted author-publication flow.
5. Standards suites, aggregate video, and staging authority.

## Verification commands

These commands are targets. Add them as the wave supplies the required suites.

```powershell
cd web
npm run build
npm run check:customization
npm run check:staging-fixes
npm run proof:unified:contract
npm run proof:unified:local
npm run proof:unified:walk
```

Existing fact at plan creation: `npm run check:staging-fixes` fails the existing
check named `slash routes do not use the shared intent seam`. Track it as a
baseline defect until fixed. Do not hide it by changing the gate.

Observed Wave 0 results:

- Fixture tier: 10 passed with two workers. It covers the cat happy path, route
  persistence, stale session reattach, denial, stale and expired approval,
  entitlement denial, quota, spend cap, and reduced-motion completion.
- Local tier: 2 passed against an isolated real Vite, FastAPI, broker, harness,
  SQLite, and job-worker stack. It proves readiness, real drawing intake,
  catalog review, explicit confirmation, broker and worker runs, completed
  result rendering, durable job API storage, Jobs-rail recovery after page
  reload, real DXF upload and extraction, uploaded geometry replacement, and a
  catalog run bound to the uploaded drawing and tenant. A separate live-auth
  guest walk also passes for signed guest upload, extraction, viewer seating,
  allowed version read, zero dispatch, and denied run. APS, Claude, Auth0 user
  sign-in, and PostgreSQL claims remain open.
- Production-like tier: runnable and skipped because no deployed base URL was
  authorized or supplied.
- Behavior pins, customization check, production build, and whitespace check:
  passed.
- Baseline `check:staging-fixes`: still fails the pre-existing slash-route seam
  check and remains visible.

## Completion rule

The program is complete only when each accepted ledger row reaches its required
evidence level, all receipts point to reproducible artifacts, the aggregate walk
uses the same controllers as the independent tests, and the user can see the
whole flow in one persistent operator scene. A passing cat fixture alone cannot
complete this plan.
