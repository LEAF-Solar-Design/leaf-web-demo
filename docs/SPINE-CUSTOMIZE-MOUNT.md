# Mounting `customize_platform` on the agent spine (R7 → conversational)

**Status: IN FLIGHT 2026-08-03, session 89d2209d (operator-directed).** This doc is
the implementation spec; if the implementing session dies, a revive executes it
top-to-bottom. Branch: `feat/spine-customize-platform-89d2209d` off `fcaff59`.

## Goal

"Change the background to lightmode" said to the in-product assistant produces a
gated, approved, audited self-edit proposal through the existing R7 lane —
instead of today's refusal ("scoped to CAD drawing tasks"). The drawer stops
being the only door.

## What exists (all verified at fcaff59)

- R7 API: `POST /api/platform/customize` (propose {title, edits[{path,content,delete}]}),
  `GET /api/platform/customize/{change_id}` (status),
  `POST /api/platform/customize/{change_id}/land` ({commit_sha} — the fresh
  per-invocation ack). Server-side admission `_gate`: live auth + valid tenant +
  `platform_customize` entitlement (admin tier or platform_admin role path) +
  R7 rollout flag (`customization_flags.enabled(7, tenant)`).
  [server/routers/platform_customize.py]
- Catalog action `customize_platform`: rung 7, `always_confirm`,
  `tenant_tightenable: false`, `enabled: False`, `dispatch.routes: []`.
  [server/agent_policy.py ~:239, server/agent_policy.json]
- The agent gate is catalog-GENERIC (`POST /internal/agent/gate` →
  agent_gate.gate). Tier authority: the message route snapshots the verified
  JWT tier onto the active turn; the gate resolves the turn tier (admin for the
  operator), falling back to broker-trusted `backedge_tenant` for non-session
  callers. No per-action gate code needed. [server/routers/agent.py :95-150]
- Approval flow is split-turn with args-bound TTL'd approvals; UI renders
  server-truth `proposed_run` chips. [docs/AGENT-SPINE-DESIGN.md §5.3-5.4]
- Harness spine tools live in `harness/src/agent/converseLoop.ts` (execution
  cases ~:700), `harness/src/ports/impl/converseSdkRunner.ts` (tool schemas
  ~:151), `harness/src/agent/spineSystemPrompt.ts` (prompt), ports in
  `harness/src/ports/index.ts` (+ fakes). Dispatch to the app rides the
  back-edge (X-Dispatch-Secret + X-Tenant-Id), route allowlist
  `deps._dispatch_backedge_route` (:499) mirrored in CONTRACT-ADDENDUM §0.

## Decisions taken

1. **Dispatch = back-edge** (the architecture the catalog's `app_api` kind
   anticipates). Add EXACTLY: `POST /api/platform/customize`,
   `GET /api/platform/customize/{change_id}`,
   `POST /api/platform/customize/{change_id}/land` to §0 + `_dispatch_backedge_route`.
   NEVER the `/internal/platform-customize/*` co-sign routes (their docstring
   pins them off the back-edge; the harness must never hold co-sign authority).
2. **Two chips, not one**: `propose` and `land` are separate always-confirm
   gate calls (land additionally re-names the exact commit_sha server-side).
   No auto-land after propose.
3. **Server R7 gate is re-checked at dispatch** with the back-edge tenant whose
   tier comes from the broker-trusted record. Deployment seed (below) makes the
   internal tenant's broker record carry `admin`; if absent the dispatch denies
   — fail closed, correct.
4. Args schema (catalog + gate validation):
   `{op: "propose"|"status"|"land", title?, edits?[{path, content?, delete?}],
     change_id?, commit_sha?}` — validated per-op in the harness case before
   the gate call; the gate binds the approval to the exact args.

## Work list (dependency order)

1. `server/agent_policy.py` + `server/agent_policy.json`: enable
   `customize_platform`, fill `dispatch.routes` (the three routes), real
   `args_schema` (above), keep rung 7 / always-confirm / not-tightenable.
2. `server/deps.py` `_dispatch_backedge_route`: add the three routes
   (POST propose, GET status w/ path prefix match, POST land w/ suffix match).
3. `server/CONTRACT-ADDENDUM.md` §0: contract revision listing the routes,
   with the co-sign exclusion restated.
4. Server tests: extend `test_agent_policy.py` (enabled, schema, routes),
   `test_agent_gate.py` (customize_platform now gates as always_confirm with
   entitlement `platform_customize`), deps back-edge tests (route allowed +
   co-sign routes still refused).
5. Harness: `converseLoop.ts` case `customize_platform` (per-op arg checks,
   call new appRun port methods, emit an event per op), `converseSdkRunner.ts`
   tool schema, `ports/index.ts` port surface + `fakes/fakeGateClient.ts` /
   fake appRun additions, `spineSystemPrompt.ts` paragraph (what the tool is,
   propose-then-land, never claim a change is live — landing pushes a BRANCH).
6. Harness tests: converseLoop.test.ts — gate-deny path, propose happy path,
   land requires change_id+commit_sha, approval-chip flow unchanged.
7. PR via autopilot (sol-critic; this is a security boundary — expect rounds).

## Deployment (after merge — each its own verified step)

- Staging first: five-service train rules apply (memory
  r7-self-edit-activation-state: alias→deactivate→deploy→activate traps).
- Broker record seed: internal tenant `1b60bef6-b488-4e18-ac45-4e4afecc30ff`
  broker-trusted tier must resolve `admin` for the back-edge dispatch re-check
  (staging + production `/data/state/broker_tenants.json` — verify actual
  schema in broker.py before editing; use ecs execute-command, exec is enabled
  on production `leaf-platform`, staging app service has exec=false).
- Production: monolith deploy drops to zero (~2-4 min 503 window).
- Honest limit to state everywhere: a LANDED change pushes branch
  `admin-customize/<id>` to leaf-web-demo. It does NOT change the running
  product until that branch merges + deploys (+ console changes need the
  leaf_website port + promotion). The assistant's prompt paragraph must say
  this so the model never overclaims.

## Session context for a revive

- Production live as of 2026-08-03 ~20:30Z: website `646bc247` on
  `dpl_mwjDa9kwQZevwHUh1Li8gdpE3LqR`; platform `9579e84`; operator identity
  seeded in production RDS (org 1b60bef6 + owner binding). Memory:
  `r7-production-handoff-7379201a` UPDATE blocks.
- Operator wants: assistant-driven UI edits ("background → lightmode", CAD
  background as full-page background). The second also needs an actual design
  change; find/port the design lane separately.
