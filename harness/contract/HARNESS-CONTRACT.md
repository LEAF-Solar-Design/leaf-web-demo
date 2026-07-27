# HARNESS-CONTRACT — tenant design-time author loop (Agent SDK)

Net-new service under `C:/tmp/leaf-web-demo/harness/`. It is the **Build lane
engine** of the Leaf web-CAD platform: **NL prompt → an Agent-SDK session
proposes source and metadata through a structured tool → the trusted harness
writes and validates the exact tool package → the broker executes it in the
configured tool sandbox → the harness registers it.** Registered tools then run
with **ZERO LLM**. Since the
agent-spine wave (`server/CONTRACT-ADDENDUM.md` §18, 2026-07-20) the same process
also hosts **ConverseLoop**, the conversational runtime surface, as a sibling to
AuthorLoop — the harness remains the platform's **sole Anthropic egress** (§1
below for the `/converse/*` surface; §4 for the superseding invariant v2).

Nothing outside `harness/` is modified. The demo `server/`, `engine/`, `da/` are
READ-ONLY references. This document is the harness's own contract; it honors — and
does not re-freeze — `contract/CONTRACT.md` (§1–§6), `server/CONTRACT-ADDENDUM.md`
(§7–§10), and `contract/AUTH.md`.

> **STATUS: FROZEN (census #13, NL-build lane, 2026-07-22).** The contract
> surfaces this document defines — the HTTP API (§1, including the F5
> `X-Harness-Secret` caller-auth gate), the four ports (§2), the routing rule
> (§3), the runtime/LLM separation invariant v2 (§4), the forbidden legacy paths
> (§5), the two-concern separation (§6), and the tool-package layout (§7) — are
> change-controlled: a breaking change is stop-the-line and needs an operator
> ruling plus a versioned supersession note (the §4 v1→v2 pattern), never a
> silent in-place edit. Additive absent-safe fields follow the ADDENDUM §10
> additive rule. The PARKED `/converse/*` spec in §1 stays parked, not frozen —
> the live conversational surface is `POST /turn`. Sibling freeze: ADDENDUM
> §15/§16/§17 (same date); ADDENDUM §18 FROZEN 2026-07-23 (census #12 chip 5):
> the `ConverseTurnInput` field set (no packet field), the `HarnessTurnEvent`
> union, `StopReason`, and the parked ContextPacket schema are pinned by
> `server/tests/test_contract_freeze.py`.

> **v3 supersession (2026-07-26, structured authored-source boundary).**
> This note supersedes the old introduction and the section 3 and section 8
> wording that let the model write repository files and described
> `validateTool` as a validation-only call. The model now receives exactly
> three tools: read-only tenant repository inspection, structured source and
> metadata submission, and broker test execution. Only the trusted harness
> writes the exact `tool.py` and `tool.json` bytes. It returns a
> `leaf.tool-source.v1` receipt. In production, the broker must execute those
> bytes in E2B and return a bound `leaf.tool-execution.v1` receipt before the
> candidate can be committed. The tenant Claude grant remains on the harness
> host. The E2B credential remains in the broker. This change strengthens the
> frozen security boundary and does not change the HTTP routes or the
> registered-tool zero-LLM invariant.

---

## 1. HTTP API

Tenant id arrives via the `X-Tenant-Id` header stub (default `demo-tenant`),
matching the backbone. Concern 1 (Auth0 platform identity, `contract/AUTH.md`) is
resolved upstream; this harness does not verify the platform JWT.

Every route below except `GET /health` sits behind the F5 caller-auth gate when it
is enabled (`LEAF_HARNESS_AUTH` + `X-Harness-Secret` from `LEAF_HARNESS_SECRET`;
timing-safe compare; FAIL-CLOSED when enabled with no secret configured).

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/health` | — | `{ ok: true, service }` (no secret required) |
| POST | `/author` | `{ description, mode?, tenant_id? }` | **build** (default): `200 { tool, code, preview, telemetry? }` (CONTRACT §4; `telemetry` additive + absent-safe). **one-off** (`mode:"one-off"`): `200 { tool, code, preview, run, telemetry? }`. Missing grant → `401 { grant_required: true, error }` |
| POST | `/run-registered` | `{ tool, params?, dwg?, aps_live?, tenant_id? }` | `200` CONTRACT §3 result envelope; never constructs the SDK |
| POST | `/turn` | `ConverseTurnInput` (`ports/converse.ts`, FROZEN) | `200 application/x-ndjson`, one `HarnessTurnEvent` per line, always terminated by `turn_complete` or `error`; pre-stream grant failure → non-stream `401 { grant_required: true }`; no runner wired → `501` |
| PUT | `/grants/{tenantId}` | `{ token, kind?, label? }` (§17: kind auto-detected when absent) | Adds and activates an account. Returns token-free status and account inventory. |
| PATCH | `/grants/{tenantId}` | `{ account_id }` | Selects one of the tenant's linked accounts as active. |
| GET | `/grants/{tenantId}` | none | Returns legacy active fields plus `{active_account_id, accounts[]}`. Never returns a token. |
| DELETE | `/grants/{tenantId}?account_id={id}` | none | Removes one tenant account. Without `account_id`, removes all accounts for legacy clients. Returns post-remove status. |

- `POST /author` build response is `{ tool, code, preview }` plus the ADDITIVE,
  absent-safe `telemetry?` (A1 — present only when the runner metered the build;
  see the route table above) where `tool` validates against **CONTRACT §2** (name
  kebab-case, version, description, kind, engine_op, params JSON-Schema, returns,
  capabilities, provenance) and also carries the hot-script **SPEC §7.1**
  `tool.json` fields (`entry`, `timeout_ms`, `idempotent`, `review`).
- Errors are JSON `{ error: { message, diagnostics? } }` with a sane HTTP status
  (400 bad request, 404 unknown tool, 422 tool failed validation, 500 internal).

### `/converse/*` — conversational spine surface (PARKED — not served)

> **PARKED at the 2026-07-21 merge resolution (spine × sessions-wire).** The harness
> does NOT register these routes — they return 404. The live conversational surface
> is the §2.1 sessions wire: the app's `/api/sessions*` routes drive the harness via
> `POST /turn` (NDJSON; see the ConverseRunner port section below). This spec is
> retained verbatim for the spine-unification follow-up; do not build against it.

ConverseLoop's mirror routes, all behind the same shared-secret gate as the rest of
the harness HTTP surface (`X-Harness-Secret`; timing-safe compare, fail-closed when
the gate is enabled with no secret configured):

| Method | Path | Behaviour |
|---|---|---|
| POST | `/converse/sessions` | `{tenantId, drawingId}` → 200 `{sessionId, status, createdAt}`; idempotent per (tenantId, drawingId) |
| POST | `/converse/sessions/{sessionId}/messages` | `{tenantId, text?/confirm?, contextPacket, classifierHint?}` (exactly one of text/confirm) → 202 `{turnId}` \| 409 `turn_in_progress` \| 401 `grant_required` |
| GET | `/converse/sessions/{sessionId}/stream?afterSeq=N` | SSE: replays persisted events with seq > N from `sessions.db`, then live events |
| GET | `/converse/sessions/{sessionId}/transcript?limit=N` | 200 `{events:[…]}` (most recent N, ascending seq) |
| DELETE | `/converse/sessions/{sessionId}` | 200 `{archived:true}` |

A `tenantId` mismatch on any route returns 404 `{error:"session_not_found"}` — no
existence oracle. Event vocabulary, error codes, the ContextPacket, and the
harness→app back-edge dispatch contract (`X-Dispatch-Secret`) are pinned in
`server/CONTRACT-ADDENDUM.md` §18; design rationale: `docs/AGENT-SPINE-DESIGN.md`.

---

## 2. The four ports (typed interfaces, each with a fake AND a real-impl stub)

Defined in `src/ports/index.ts`. Fakes in `src/ports/fakes/`, real stubs in
`src/ports/impl/`. The hermetic gate runs entirely on the fakes; the real stubs
**compile now** and their live path is **operator-gated**.

| Port | Supplies | Fake | Real stub (operator-gated) | Sibling lane |
|---|---|---|---|---|
| `OAuthGrantProvider` | the tenant's Agent SDK grant (Concern 2) | `fakeOAuthGrant.ts` | `impl/oauthGrantProvider.ts` | `hosted-oauth-spike` |
| `TenantRepoProvider` | checkout of the tenant mushy-codebase git repo + `commit()` | `fakeTenantRepo.ts` | `impl/tenantRepoProvider.ts` | `project-job-schema` |
| `BrokerApsClient` | run/test-run a tool on APS via the broker (never raw creds) | `fakeBrokerApsClient.ts` | `impl/brokerApsClient.ts` | `async-broker-catalog-envelopes` |
| `AgentRunner` | the Agent SDK loop boundary (real = SDK, fake = scripted) | `fakeAgentRunner.ts` | `impl/agentSdkRunner.ts` | `agentsdk-usage-visibility` |

Two later waves added live OPTIONAL ports on `HarnessPorts` (absent → their routes
answer 501/404, everything else unchanged); they are part of the frozen surface:

| Port | Supplies | Fake | Real impl | Wave |
|---|---|---|---|---|
| `grantAdmin: TenantGrantAdminStore` | `put/status/remove` backing `/grants/{tenantId}` (wire `{linked, linked_at, kind}`, never the token) | temp-dir store in tests | `FileTenantGrantStore` (same instance as the read side; F18 seam via `createTenantGrantStore`) | §16 wave 4 |
| `converseRunner: ConverseRunner` | one converse turn as an async event stream backing `POST /turn` | `fakeTurnRunner.ts` | `impl/agentSdkTurnRunner.ts` (lazy-loaded) | §18 sessions wire |

`BrokerApsClient` maps to **POST `{BROKER_URL}/broker/run`** with the snake_case
body `{ tenant_id, tool, params, dwg, aps_live }` and returns the extended §3/§10
envelope (`ADDENDUM §8`). `BROKER_URL` defaults to `http://127.0.0.1:8140`; a
per-tenant kill-switch denial surfaces as `TENANT_DISABLED`, a down broker as
`BROKER_UNREACHABLE`.

---

## 3. Routing rule — run / one-off / build

`src/routing.ts`:

- **run** — dispatch an EXISTING registered tool. **Cheapest**: no author session;
  the Agent SDK is never constructed. Addressed only by `POST /run-registered`
  (never inferred from a prompt).
- **one-off** — author a tool + run it once, then **discard** (no register, no
  commit).
- **build** — author a tool + **register** it (append to `registry.json` + exactly
  one harness commit). Default for `POST /author`.

one-off and build share the author mechanism (`AgentRunner` + the three tools);
**only build persists** via `registry/registerTool.ts`.

The design-time author session is granted **exactly three tools** (hot-script
SPEC §10, with no shell and no arbitrary network access):

1. `fs_tenant_repo` provides read, list, and exists operations scoped to the
   checkout directory. It has no write operation and rejects path escapes.
2. `validate_tool` accepts the proposed source and manifest metadata. The
   trusted harness validates them, writes only `tools/<name>/tool.py` and
   `tools/<name>/tool.json`, and returns a `leaf.tool-source.v1` receipt with
   exact paths, byte counts, and SHA-256 digests. A retry can replace only the
   same uncommitted package when it presents the exact prior receipt.
3. `aps_test_run` delegates the current validated candidate to
   `BrokerApsClient` with `aps_live:false`. When
   `LEAF_TOOL_SANDBOX_PROVIDER=e2b`, success requires a
   `leaf.tool-execution.v1` receipt bound to the submitted source digest and
   tenant hash.

The model cannot write arbitrary files, choose a destination path, register a
tool, or commit a repository. `AuthorLoop` re-reads the package, checks the
source receipt, execution receipt, tenant binding, exact two-file Git diff, and
registry state before it can register and commit.

---

## 4. Runtime/LLM separation invariant — v2 (load-bearing)

> **v2 (2026-07-20, agent spine) supersedes the v1 "design-time-ONLY" wording.**
> The conversational spine is a *runtime* LLM surface, so the invariant is restated
> more precisely rather than silently weakened. Every v1 author-loop guarantee
> below is kept intact.

**Registered-tool EXECUTION never touches the Agent SDK.** The only code path that
runs a registered tool is the deterministic chain `POST /api/run → jobs → broker →
tool_loader.run_tool_dynamic` (or `AuthorLoop.run → broker`), and no frame of that
chain may construct, import, or await an AgentRunner/SDK session. The conversational
session is a metered, grant-scoped runtime surface that may PLAN, EXPLAIN, and
DISPATCH deterministic execution — but the dispatch boundary is an opaque HTTP job
submission whose result the tool computes with ZERO LLM.

Corollaries: (1) an agent tool result may never BE a drawing mutation — only a job
id or a proposal; (2) the deterministic classifier and every registered tool keep
working, at full fidelity, with the harness process stopped.

**Author-loop guarantees (v1, unchanged):** the design-time author session is still
spawned **per author/one-off/build request and torn down after**. The run path
(`AuthorLoop.run` → `BrokerApsClient.runTool`) **never references `AgentRunner` or
constructs the Agent SDK**. `test/designTimeOnly.test.ts` enforces this with a spy
on `AgentRunner.run()` that must not fire on `POST /run-registered` (plus a
positive control proving the spy fires on `POST /author`). Its superseding test,
`test/converseRuntimeSeparation.test.ts`, keeps that assertion and adds the converse
side: the only side-effecting port a ConverseLoop turn touches is its `AppRunClient`
dispatch port (never code, never a drawing delta, never a direct broker call), with
a positive control proving the runner IS invoked on the converse turn.

The LLM **authors tools at design time and converses at runtime; it is never in the
execution path of a registered tool**.

---

## 5. Explicitly-forbidden legacy paths

The harness reaches Anthropic **only** through the Agent SDK, inside
`src/ports/impl/agentSdkRunner.ts`, behind the `AgentRunner` port. It must NOT be:

- the fleet's `claude -p` CLI chokepoint,
- the `claude-max-api-proxy` token-forwarding shape (banned Feb 2026),
- the legacy `agent_loop.py`.

Enforced by the gate: `grep -rEn "claude -p|claude-max-api-proxy|agent_loop\.py"
harness/src harness/test` → **zero matches**. (These strings appear only in this
contract doc, which is outside `src/` and `test/`.)

The harness **never holds raw creds**: the Claude grant is injected explicitly into
a scrubbed child env by `agentSdkRunner.ts` (mirroring the `hosted-oauth-spike`
env discipline — scrubbed `ANTHROPIC_*`/`CLAUDE_CODE_*`, token set explicitly,
never logged), and APS is reached **only** through the broker (the broker process
is the sole holder of the APS credential).

---

## 6. Corrected OAuth reality (supersedes the brief's "monthly credit" framing)

Per `research/agentsdk-usage-visibility.md` (2026-07-17, live-sourced):

- **The June-2026 monthly *dollar-credit* program is PAUSED — it never took
  effect.** Agent SDK usage (and `claude -p`, and third-party-app usage) still
  **draws from the user's existing subscription usage limits** (the 5-hour +
  weekly rate windows), **not** a dedicated per-user dollar credit.
- **There is NO balance API** for a subscription-OAuth third-party app. Metering is
  **self-computed** from each response's `usage` object (`input_tokens`,
  `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`) priced
  at published rates, optionally cross-checked against `anthropic-ratelimit-*`
  headers.
- **Subscription OAuth tokens are INDIVIDUAL-USE — one per end user, NEVER shared
  or pooled.** A hosted app must do **per-tenant** OAuth (each user authorizes their
  OWN Claude subscription). Running many tenants through one operator token is the
  anti-bridging violation Anthropic actively blocks (as of April 2026).
- Token issuance: `claude setup-token` → a **1-year** `CLAUDE_CODE_OAUTH_TOKEN`
  consumed by `@anthropic-ai/claude-agent-sdk`. Enterprise lane = BYO API key.
- Mid-session exhaustion surfaces as **HTTP 429 `rate_limit_error` + `retry-after`**
  — the SAME type as an ordinary rate limit, so disambiguate by reset horizon, not
  error type. The OAuth token persists across a window reset (no re-auth).

**Two-concern separation (`contract/AUTH.md` §0):** the `OAuthGrantProvider`
(Concern 2 — "whose Anthropic credit") is fully independent of the Auth0 tenant
JWT (Concern 1 — "which workspace"). They have different cardinalities (one Auth0
tenant → many users; one Claude OAuth → one user) and **must never mingle** in a
single claim or token store.

---

## 7. Tool package layout in the tenant repo

Each built tool becomes a directory under `tools/` in the tenant repo:

```
tools/<name>/
  tool.json     (hot-script SPEC §7.1 manifest; entry is package-relative "tool.py")
  tool.py       (entry script: def run(intake, params) -> (result, overlay) — zero-LLM)
registry.json   (repo root; { "tools": [ <tool package> ] }; entry is repo-root-relative)
```

The registry entry is the CONTRACT §2 tool package (+ SPEC §7 fields). Its `entry`
is repo-root-relative (`tools/<name>/tool.py`) so the demo's dynamic loader
(`server/tool_loader.py`, "the FILE is the tool") can resolve it if this repo is
mounted there. The per-package `tool.json` uses the SPEC's package-relative `entry`
(`tool.py`).

---

## 8. Validation oracle

`src/registry/toolPackageSchema.ts` is a **faithful TypeScript port of CONTRACT
§2**. The trusted `validate_tool` handler applies it before writing a candidate,
and `AuthorLoop` applies it again before registration. The model does not invoke
the validator directly against repository paths. Chosen over shelling out to
`engine/selfcheck.py` because the gate must be hermetic and Windows-safe, with
one language and one process. `selfcheck.py` is the §3 **envelope** and
effective-registry checker, not a §2 tool-**package** schema checker. It remains
the §3 oracle on the Python run side.

---

## 9. Verification (all hermetic — no network, no real Anthropic/APS creds)

```
cd C:/tmp/leaf-web-demo/harness && npm ci && npm test      # vitest: author.e2e + designTimeOnly
cd C:/tmp/leaf-web-demo/harness && npx tsc --noEmit        # four ports + real stubs compile
grep -rEn "claude -p|claude-max-api-proxy|agent_loop\.py" harness/src harness/test   # zero
```

## 10. Live smoke (DEFERRED — operator-gated)

A real SDK + real per-tenant grant smoke stays deferred until: the app is
registered for "sign in with Claude", `@anthropic-ai/claude-agent-sdk` is
installed, a broker is running on `BROKER_URL`, and the per-tenant grant store +
repo locator exist. The sibling `C:/tmp/hosted-oauth-spike/` is READY-FOR-CLICK and
demonstrates the clean-env single-turn pattern this harness's `agentSdkRunner.ts`
mirrors. Rollback = delete `harness/`.

The CONTAINERIZED smoke is NOT deferred: `scripts/harness-container-smoke.py`
boots the real compose stack (broker + harness + app, `LEAF_AGENT_MOCK=1` so no
Anthropic egress) and proves the authed app→harness hop, durable grant/tenant
volumes across a container restart, the §16.H shared-volume catalog fold, and
secret-free logs. Opt-in gate entry: `LEAF_CONTAINER_SMOKE=1` in
`scripts/run-all-gates.py`.

## 11. Manifest-v1 adoption (tool-package intake; census #13)

The cross-host CAD tool-package contract ("manifest v1":
`LEAF-Solar-Design/cad-tool-package` → `contract/CONTRACT.md` +
`contract/tool.schema.json`) is FROZEN as of 2026-07-22, with a recorded
round-trip gate PASS on both legs (C# in-process host + hosted Linux runner).
This lane adopts it:

- The §7 layout above (`tool.json` SPEC §7.1 + `registry.json` CONTRACT §2
  entry) is the platform's OWN registry shape and is unchanged by this adoption.
- Any NL-build-lane container that accepts EXTERNALLY-authored tool packages as
  intake (a future surface — today the harness only authors packages itself)
  MUST reuse the manifest-v1 Linux runner (`runner-linux/runner.py`: validate +
  introspect + re-emit) rather than re-implement package validation, and MUST
  keep its report honesty floor: `cad_execution: not-attempted` — a validation
  container never fakes CAD execution.
- Any change to the package format re-runs the manifest-v1 gate
  (`gate/run_gate.py --all` in that repo) on both legs before merge; a breaking
  change to a frozen field is stop-the-line.
