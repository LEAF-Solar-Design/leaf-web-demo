# HARNESS-CONTRACT — tenant design-time author loop (Agent SDK)

Net-new service under `C:/tmp/leaf-web-demo/harness/`. It is the **Build lane
engine** of the Leaf web-CAD platform: **NL prompt → an Agent-SDK session edits
the tenant's mushy-codebase git repo → authors + validates a deterministic tool
package → registers it.** Registered tools then run with **ZERO LLM**. Since the
agent-spine wave (`server/CONTRACT-ADDENDUM.md` §18, 2026-07-20) the same process
also hosts **ConverseLoop**, the conversational runtime surface, as a sibling to
AuthorLoop — the harness remains the platform's **sole Anthropic egress** (§1
below for the `/converse/*` surface; §4 for the superseding invariant v2).

Nothing outside `harness/` is modified. The demo `server/`, `engine/`, `da/` are
READ-ONLY references. This document is the harness's own contract; it honors — and
does not re-freeze — `contract/CONTRACT.md` (§1–§6), `server/CONTRACT-ADDENDUM.md`
(§7–§10), and `contract/AUTH.md`.

---

## 1. HTTP API

Tenant id arrives via the `X-Tenant-Id` header stub (default `demo-tenant`),
matching the backbone. Concern 1 (Auth0 platform identity, `contract/AUTH.md`) is
resolved upstream; this harness does not verify the platform JWT.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/health` | — | `{ ok: true, service }` |
| POST | `/author` | `{ description, mode? }` | **build** (default): `200 { tool, code, preview }` (CONTRACT §4). **one-off** (`mode:"one-off"`): `200 { tool, code, preview, run }` |
| POST | `/run-registered` | `{ tool, params?, dwg?, aps_live? }` | `200` CONTRACT §3 result envelope |

- `POST /author` build response is **exactly** `{ tool, code, preview }` where
  `tool` validates against **CONTRACT §2** (name kebab-case, version, description,
  kind, engine_op, params JSON-Schema, returns, capabilities, provenance) and also
  carries the hot-script **SPEC §7.1** `tool.json` fields (`entry`, `timeout_ms`,
  `idempotent`, `review`).
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

The design-time author session is granted **exactly three tools** (hot-script SPEC
§10 — no shell, no arbitrary net):

1. `fsTenantRepo` — read/write scoped to the checkout dir; rejects path escapes.
2. `validateTool` — runs the CONTRACT §2 oracle; returns pass/fail + diagnostics.
3. `apsTestRun` — delegates to `BrokerApsClient` (broker only, `aps_live:false`).

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
§2**, used by the `validateTool` tool and re-run by the harness before register
(defense in depth). Chosen over shelling out to `engine/selfcheck.py` because the
gate must be hermetic and Windows-safe, one language / one process — and
`selfcheck.py` is actually the §3 **envelope** + effective-registry checker, not a
§2 tool-**package** schema checker. `selfcheck.py` remains the §3 oracle on the
Python/run side.

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
