# leaf-tenant-author-harness

The **design-time author loop** for the Leaf web-CAD platform, built on the
Anthropic Agent SDK. An NL prompt drives an agent that edits the tenant's
mushy-codebase git repo, authors + validates a **deterministic** tool package, and
registers it. **Registered tools then run with ZERO LLM.**

Net-new, parallel-safe: everything lives under `harness/`. Nothing outside is
modified. Full contract: [`contract/HARNESS-CONTRACT.md`](contract/HARNESS-CONTRACT.md).

## Quick start (hermetic — no network to Anthropic/APS, no real creds)

```bash
cd C:/tmp/leaf-web-demo/harness
npm ci
npm test          # vitest: test/author.e2e.test.ts + test/designTimeOnly.test.ts
npx tsc --noEmit  # four ports + fakes + real-impl stubs all compile
```

## Shape

- `src/server.ts` — HTTP shell over `AuthorLoop` (ports injected).
  `GET /health`, `POST /author`, `POST /run-registered`.
- `src/agent/authorLoop.ts` — build / one-off / run orchestration.
- `src/agent/tools/` — the three tools the author session is granted:
  `fsTenantRepo`, `validateTool`, `apsTestRun`.
- `src/ports/index.ts` — the four typed ports.
  `src/ports/fakes/` (hermetic) + `src/ports/impl/` (real, operator-gated).
- `src/registry/` — CONTRACT §2 schema oracle + `registerTool`.

## Design-time-only invariant

The Agent SDK is spawned per author request and torn down after. The run path
(`POST /run-registered`) dispatches a registered tool through the credential broker
and **never** touches the Agent SDK — enforced by a spy in
`test/designTimeOnly.test.ts`.

## Ports and the fakes/real split

| Port | Fake (tested now) | Real (operator-gated) |
|---|---|---|
| `OAuthGrantProvider` | scripted grant | per-tenant "sign in with Claude" OAuth / BYO key |
| `TenantRepoProvider` | real git temp clone of the fixture | clone the tenant's mushy repo |
| `BrokerApsClient` | deterministic §3 envelope | `POST {BROKER_URL}/broker/run` |
| `AgentRunner` | scripted authoring | `@anthropic-ai/claude-agent-sdk` (scrubbed env, explicit grant) |

## OAuth reality (corrected)

The June-2026 monthly *dollar-credit* program is **paused**; Agent SDK usage draws
from the user's subscription rate windows. There is **no balance API** — self-meter
from per-response `usage`. Subscription OAuth is **individual-use, one token per
user, never pooled** → the web lane must do **per-tenant** OAuth. Details in
`contract/HARNESS-CONTRACT.md` §6 and `research/agentsdk-usage-visibility.md`.

Rollback: delete `harness/`.
