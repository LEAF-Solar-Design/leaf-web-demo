# Agent SDK "Sign in with Claude" — runtime usage/balance visibility, developer requirements, exhaustion behavior

**Research doc.** Read-only web research, executed 2026-07-17. No code, no API calls, no OAuth. Every load-bearing claim is tagged `confirmed (source: URL)` or `inferred (would confirm by: X)`. All sources were fetched **live this session** (the program post-dates common training data; nothing below is from model memory).

---

## Headline correction to the platform's working assumption (read this first)

The `MATRIX.md` amendment block and this task's framing both say the **2026-06-15 Agent SDK credit program** is live/"reinstated" and that "Anthropic enforces the cap." The live docs refine that materially:

1. **The separate monthly *dollar credit* was PAUSED on June 15, 2026 — it never took effect.** As of the current Help Center article: *"Update June 15: We're pausing the changes to Claude Agent SDK usage described below. For now, nothing has changed: Claude Agent SDK, `claude -p`, and third-party app usage still draw from your subscription's usage limits."* — `confirmed (source: https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)`.
2. **What IS true and shippable:** a third-party app built on the Agent SDK **can** authenticate with a user's Claude subscription via OAuth and draw from that user's subscription — but it draws from the subscription's **existing usage limits** (5-hour + weekly rate windows), **not** a dedicated per-user dollar credit. — `confirmed (source: support.claude.com/.../15036540)`.
3. **The catch that MATRIX's optimism glosses:** the subscription OAuth token is **licensed for individual use**; shipping a **multi-user** app on subscription OAuth "likely violates Anthropic's terms," and "as of April 2026 Anthropic actively blocks third-party harnesses that try to bridge subscription auth into other tools." — `confirmed (source: https://dev.to/aviv_shaked/how-to-use-your-claude-promax-subscription-with-the-agent-sdk-python-typescript-4emi)`. Each user must authorize **their own** subscription (one OAuth token per end user); Leaf may not run many tenants through one operator's token.

Net: the web lane is still doable, but "Anthropic enforces the per-tenant cap for us via a clean credit meter" is **not** the current reality. There is **no dedicated dollar-credit cap today**, and **no API that hands a third-party app the user's remaining balance.** Leaf must self-meter from token usage and treat per-tenant capping as its own responsibility.

## Summary — decisions this unblocks

- **`anthropic-agentsdk-integration`:** Wire **per-user** OAuth (each tenant authorizes their own Claude subscription; the app receives a token via the claude.ai redirect / `claude setup-token` produces a 1-year `CLAUDE_CODE_OAUTH_TOKEN`). Do **not** design a single shared subscription token serving all tenants — that is the individual-use / anti-bridging violation. `confirmed`.
- **`billing`:** There is **no balance-read API** for a subscription-OAuth third-party app. Metering must be **self-computed** from each response's `usage` object (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`) priced at published API rates, optionally cross-checked against the `anthropic-ratelimit-unified-5h-*` response headers. `confirmed` for the token fields; `inferred` for the unified-header cross-check.
- **Keystone credential-broker (MATRIX §keystone):** It **can** enforce a spend cap, but only because the broker sees every call's token `usage` and can stop routing when a Leaf-side running total crosses a threshold. It **cannot** rely on Anthropic to return the tenant's remaining subscription balance. `confirmed` (negative on balance API) + `inferred` (broker self-metering design).
- **Degraded-mode (MATRIX #5):** At exhaustion, the SDK/API returns **HTTP 429 `rate_limit_error`** with a `retry-after` header — the **same** error type as an ordinary per-minute rate limit, so the harness must disambiguate by window/reset, not by error type. `confirmed`.
- **Contrast (enterprise/API-key lane):** Only the **Admin** Usage & Cost API (`/v1/organizations/usage_report/messages`, `/v1/organizations/cost_report`) returns dollars-and-tokens — and it needs an org **Admin API key** (`sk-ant-admin-*`), which a subscriber's OAuth token is not. `confirmed`.

---

## Q1 — Can the app see a user's remaining credit / usage?

**Two vantages, kept separate:**

**(a) End-user on claude.com/console:** Yes — a user sees their own usage and limits in the claude.ai `/usage` view, the Console `/usage` page, and `/settings/limits`. `confirmed (source: https://platform.claude.com/docs/en/api/rate-limits — "You can monitor your rate limit usage on the Usage page of the Claude Console"; "see your organization's tier and current limits on the Limits page")`. This is a human-facing surface, not something the developer app reads.

**(b) Developer app via the SDK (the vantage that matters):** **No documented mechanism returns a subscriber's remaining dollar credit/balance to a third-party app.** — `confirmed-negative`. Evidence:
- The Help Center article for using the Agent SDK with a Claude plan describes **no** balance/usage read for third-party apps. `confirmed (source: support.claude.com/.../15036540)`.
- The official rate-limits doc lists the response headers an app *can* read: `anthropic-ratelimit-requests-limit/remaining/reset`, `anthropic-ratelimit-tokens-limit/remaining/reset`, `anthropic-ratelimit-input-tokens-*`, `anthropic-ratelimit-output-tokens-*`, plus `retry-after`. **These are per-window request/token RATE limits, expressed in requests and tokens — not a dollar balance.** `confirmed (source: platform.claude.com/docs/en/api/rate-limits, "Response headers" table)`.
- The closest thing to "remaining subscription usage" for a **subscription** (Pro/Max) session is the **`anthropic-ratelimit-unified-5h-status` / `-remaining` / `-reset`** family of response headers — "the same 5-hour-window usage that `/usage` and claude.ai show." The `remaining` value is a **proportion of the window (e.g. `0.58`), not dollars.** `confirmed-that-headers-exist (source: https://github.com/anthropics/claude-code/issues/55333)`; note this is an Anthropic **GitHub issue / feature request**, not formal API reference — treat the header *semantics* as `inferred (would confirm by: an official docs page documenting anthropic-ratelimit-unified-* for subscription auth, which was not found)`.

**Answer:** A third-party developer app **cannot** read the user's remaining dollar credit. It can, at best, read **rate-window remaining** (proportion/tokens/requests) via response headers and infer headroom — never a spend balance.

## Q2 — Is there a usage or balance API / callback?

- **Per-response token usage — YES (this is the self-metering primitive).** Every Messages response carries a `usage` object with `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens`. `confirmed (source: platform.claude.com/docs/en/api/rate-limits — the "Cache-aware ITPM" section enumerates input_tokens / cache_creation_input_tokens / cache_read_input_tokens and total_input_tokens = cache_read + cache_creation + input_tokens)`. An app can price these against published rates to compute spend itself.
- **Rate Limits API — exists, but reads configured limits, not a balance.** "read the configured limits programmatically with the Rate Limits API" (`/docs/en/manage-claude/rate-limits-api`). This returns org/workspace RPM/ITPM/OTPM configuration, not remaining dollars. `confirmed (source: platform.claude.com/docs/en/api/rate-limits, "read your current organization and workspace rate limits programmatically, use the Rate Limits API")`.
- **Admin Usage & Cost API — returns dollars + tokens, but org-Admin-key only (NOT reachable by a subscriber-OAuth third-party app).** `/v1/organizations/usage_report/messages` (token consumption by model/workspace/service tier) and `/v1/organizations/cost_report` (USD, "reported as decimal strings in lowest units (cents)"), authenticated with an **Admin API key `sk-ant-admin-*`**. `confirmed (source: https://platform.claude.com/docs/en/manage-claude/usage-cost-api via WebSearch result summary + endpoint reference platform.claude.com/docs/en/api/admin/cost_report)`. This is the **enterprise/API-key** path, not the subscription path — one-line contrast per scope.
- **Balance webhook / credit-state callback — NONE found.** `confirmed-negative` (no such callback in the Help Center article, the rate-limits doc, or the errors doc; searches on "credit"/"balance"/"webhook" surfaced none).

**Answer:** No balance API and no credit callback for the subscription-app vantage. The only runtime signal an app owns is the **per-response `usage` token counts** (+ rate-limit headers). Dollar-level usage exists only via the **Admin** API, which requires an org admin key Leaf would have to hold itself (i.e., BYO-key/enterprise lane), not the user's subscription.

## Q3 — Developer-side program requirements (registration, approval, terms, quotas)

- **OAuth model:** "When you connect a third-party app to Claude, the app redirects you to claude.ai for authorization … you sign in, grant the app permission, and the app receives a token it uses to make requests on your behalf." `confirmed (source: support.claude.com/.../15036540, as summarized from the live article via WebSearch of the same URL)`.
- **Token issuance for SDK/CLI:** `claude setup-token` "opens the same browser authorization flow as `/login`" and prints a **one-year OAuth token**; set it as `CLAUDE_CODE_OAUTH_TOKEN`. "This token authenticates with your Claude subscription and requires a Pro, Max, Team, or Enterprise plan. It can only make model requests." The env vars `apiKeyHelper`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` "apply to the CLI and the surfaces that wrap it, **including the Agent SDK**." `confirmed (source: https://code.claude.com/docs/en/authentication)`.
- **Individual-use / no-multi-user restriction (the binding constraint):** "the OAuth token is licensed for **individual use** through Claude Code and the Agent SDK. **Do not** ship a multi-user app on it" — because "you'd run everyone's traffic through one person's quota," and "it likely violates Anthropic's terms. Pro/Max plans are for individual use, and **as of April 2026 Anthropic actively blocks third-party harnesses that try to bridge subscription auth into other tools.**" `confirmed (source: dev.to/aviv_shaked/...)`. Implication: Leaf's web lane must do **per-tenant OAuth** (each user authorizes their own subscription), never a shared token.
- **Org login restriction levers (relevant to enterprise tenants):** admins can pin sessions to an org with `forceLoginMethod` / `forceLoginOrgUUID`. `confirmed (source: code.claude.com/docs/en/authentication)`.
- **No public self-serve "register + get approved as a third-party OAuth app" flow was found in docs.** Whether Anthropic operates a formal registration/allowlist/approval program for third-party OAuth clients (client IDs, redirect-URI registration, review), and any per-app quota schedule, is **not publicly documented** and is almost certainly **login-gated** in console/developer settings. `confirmed-negative on public docs` → see **Open questions / login-gated gaps**.

**Answer:** The runtime mechanism (per-user browser OAuth → token → SDK env var) is documented and clear. The **program/registration** side (formal app registration, approval, per-app quotas) is **not** in public docs. The one hard, documented **rule** is: subscription OAuth is **individual-use**; multi-user apps on one subscription token violate terms and are actively blocked.

## Q4 — What happens when a user's credit is exhausted mid-session?

**Current reality (credit split paused → usage draws from subscription limits):**
- When a subscription usage window is exhausted, requests fail with **HTTP 429, error `type: "rate_limit_error"`** ("429 - `rate_limit_error`: Your account has hit a rate limit"), accompanied by a **`retry-after`** header (seconds to wait). `confirmed (source: https://platform.claude.com/docs/en/api/errors and platform.claude.com/docs/en/api/rate-limits)`.
- **Distinguishable from an ordinary per-minute rate limit? Not by error type** — both are `429 rate_limit_error`. The disambiguation must come from **which window is exhausted and its reset horizon**: a per-minute burst has a short `retry-after` / near-term `*-reset`, whereas a subscription 5-hour or weekly window shows a long reset in the `anthropic-ratelimit-unified-5h-*` / `-7d-*` headers. `confirmed` that both share the type/status; `inferred (would confirm by: observing the unified-5h/7d reset horizon on a live 429 — not done, read-only)` for the window-based disambiguation.
- **Distinct billing-exhaustion signal exists only on the API-key path:** `402 billing_error` ("There's an issue with your billing or payment information"), and for API-key orgs a spend-cap hit means "API usage pauses until the next month." These apply to **Console/API-key** accounts, **not** to subscription OAuth (which surfaces exhaustion as 429). `confirmed (source: platform.claude.com/docs/en/api/errors; platform.claude.com/docs/en/api/rate-limits "Spend limits")`.

**The paused would-be credit design (documented, but NOT currently in effect — tag accordingly):**
- "When your monthly credit runs out, additional Agent SDK usage flows to usage credits at standard API rates — but only if you've enabled usage credits. If usage credits aren't enabled, Agent SDK requests **stop until your credit refreshes**." `confirmed-as-paused-design (source: support.claude.com/.../15036540)`.
- The proposed credit was **per-user, refreshes monthly with the billing cycle, and NON-rollover** (unused credit does not carry over), billed at standard API rates. `confirmed-as-paused-design (source: multiple 2026-06 write-ups of the announcement; e.g. https://www.digitalapplied.com/blog/anthropic-claude-credit-overhaul-june-15-2026 and https://thenewstack.io/anthropic-agent-sdk-credits/)`. Since the program is paused, treat these as the **shape Anthropic will likely resume**, not current law.

**Resume after reset:** The OAuth token is long-lived (1-year `setup-token`; `/login` credentials refresh), so no re-auth is needed across a window reset. Once the exhausted window replenishes (token-bucket continuous replenishment for rate windows; monthly refresh for the paused credit), the **same session can resume making requests**. `inferred (would confirm by: a live exhaust-then-wait-then-retry test — not performed, read-only)`. The Agent SDK session state is app-side; Anthropic does not "hold" a session across exhaustion — the harness must retry after `retry-after`/reset. `inferred`.

**Answer:** Mid-session exhaustion today = `429 rate_limit_error` + `retry-after`, **indistinguishable by type** from a normal rate limit (disambiguate via reset horizon). Non-rollover monthly reset is the paused-credit design, not current behavior. A session resumes after the window/credit resets without re-auth, because the OAuth token persists.

---

## UX / architecture implications (mapped onto the platform)

### (i) How the web UI credit indicator can be populated

Given Q1/Q2 (**no dollar-balance read for subscription apps**), the indicator must be a **self-metered running total**, not a direct balance:

1. **Primary — self-metered spend.** The credential-broker sums each response's `usage` (`input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens` + `output_tokens`), prices them at the model's published API rate, and accumulates per tenant per billing period. Display as "≈ $X used this cycle" and, if the tenant tells Leaf their plan, "≈ $X of $20/$100/$200." `confirmed` inputs exist; the plan cap number is **user-supplied or paused-program-derived**, so label it an **estimate**. `inferred` for the cap value.
2. **Secondary — rate-window headroom.** Surface `anthropic-ratelimit-unified-5h-remaining` / `-reset` (proportion + reset time) as a "usage window" gauge, mirroring what claude.ai `/usage` shows. This tells the user "you're near your 5-hour/weekly cap," which is the **actually-enforced** limit today. `inferred` (community-documented header).
3. **Honest fallback copy.** If Leaf chooses not to self-meter, the only honest statement is **"no live credit balance is available from Anthropic for subscription sign-in; usage is estimated from tokens and reconciled post-hoc."** Do not render a fake "$ remaining" — there is no source for it. `confirmed-negative` basis.

### (ii) Zero-credit / exhaustion degraded-mode — as the shared error envelope (MATRIX #5)

On a 429 whose reset horizon indicates the subscription window (not a transient per-minute burst), the broker emits:

```json
{
  "error_code": "llm_quota_exhausted",
  "message": "Your Claude plan's usage limit is reached. Design tools that need the model are paused until your limit resets (see reset time). Already-built deterministic tools keep working.",
  "retryable": true,
  "degraded_mode": true
}
```

- **What the user sees:** a calm banner — "AI authoring paused until <reset time from `retry-after`/`unified-*-reset>`"; the CAD viewport, deterministic (zero-LLM) registered tools, and APS read/solve ops **stay live** because they do not consume LLM. This is exactly the platform's factory-not-runtime split: **only the design-time tool factory degrades; the runtime does not.** `confirmed` architecture fit (factory/runtime split per MATRIX + MISSION).
- **What the harness does:** stop routing LLM calls for that tenant; do **not** hard-fail the session; persist the in-flight mushy-branch state; schedule/allow retry after `retry-after`. Distinguish `llm_quota_exhausted` (long reset, hours/days/until-refresh) from `llm_rate_limited` (short `retry-after`, auto-retry with backoff). `confirmed` that both are 429 (so the broker must branch on reset magnitude), `inferred` for the exact threshold.
- **Resume next month/cycle:** yes — the OAuth token persists, so once the window (or the paused-design monthly credit) resets, the same session resumes with no re-auth. Non-rollover means unused headroom does **not** bank; Leaf should not promise carry-over. `inferred` (resume) + `confirmed-as-paused-design` (non-rollover).

### (iii) Can the credential-broker (MATRIX §keystone) enforce a spend cap from what the SDK exposes?

**Yes, but only by self-metering out-of-band — not by reading an Anthropic balance.** The broker is the one component that sees every call's `usage`, so it can: (a) accumulate per-tenant token spend, (b) refuse to route once a Leaf-configured cap is crossed (kill-switch), and (c) emit the `llm_quota_exhausted` degraded envelope proactively **before** Anthropic's own 429. This closes the MATRIX risks "usage attribution; quota cutoff; spend-cap kill switch" **provided Leaf tracks usage itself.** It does **not** get a free cap from Anthropic on the subscription path. `confirmed` (token usage is readable) → `inferred` (broker enforcement design; no code this task). Corollary: for **hard** liability protection (tenant can't exceed Leaf-fronted spend), the BYO-**platform-API-key** path is stronger because it also exposes the Admin cost API and 402 billing signals — reinforcing MATRIX's "swap subscription → API key" recommendation for any lane where Leaf carries the bill.

---

## Open questions / login-gated gaps (for the operator — NOT guessed)

1. **Is there a formal third-party OAuth *app registration/approval* program?** Public docs describe the *user* OAuth flow and `claude setup-token`, but no self-serve developer flow to register a third-party OAuth **client** (client ID, redirect-URI allowlist, review/approval). If this exists, it is behind console/developer login. Operator: check console.anthropic.com / platform.claude.com developer settings and claude.com developer/OAuth app settings.
2. **Exact current per-plan usage limits (5h + weekly) for Pro/Max/Team/Enterprise** that subscription Agent SDK usage draws from — the numeric caps are shown in the logged-in claude.ai `/usage` view, not in the fetched public docs.
3. **The precise Terms/Usage-Policy clause governing a *hosted third-party app* authenticating many end-users each via their own subscription OAuth** — i.e., is Leaf's "each tenant signs in with their own Claude" model explicitly permitted, versus the blocked "bridge one subscription into a harness" pattern? The dev.to source says multi-user-on-one-token violates terms; whether per-user-OAuth-in-a-hosted-app is sanctioned needs the actual Commercial Terms / Usage Policy text (login/legal page).
4. **Whether/when Anthropic resumes the paused monthly-credit program**, and its final exhaustion semantics (usage-credits-enabled overflow, non-rollover reset date). The paused design is documented; the resumption timing is not announced. `confirmed-negative` per the Help Center pause note.
5. **Per-app or per-user quotas specific to the (paused) Agent SDK credit** beyond the headline $20/$100/$200 — any rate/throughput caps distinct from normal subscription limits are not in public docs.

*Executor did NOT log in, create a developer account, accept terms, call the Anthropic API, or run any OAuth flow. The above are listed precisely for the operator to retrieve.*

---

## Sources (all fetched live this session, 2026-07-17)

1. **https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan** — Authoritative Help Center article. Confirms the **June 15 pause** ("nothing has changed: Agent SDK, `claude -p`, and third-party app usage still draw from your subscription's usage limits") and the paused exhaustion design ("when your monthly credit runs out … requests stop until your credit refreshes"). *Primary source for Q4 + headline correction.*
2. **https://platform.claude.com/docs/en/api/rate-limits** — Official rate-limits reference. Confirms the full `anthropic-ratelimit-*` response-header table (requests/tokens/input/output limit·remaining·reset, `retry-after`), that they are **rate** limits (tokens/requests, not dollars), the `usage` token fields (`input_tokens`/`cache_creation_input_tokens`/`cache_read_input_tokens`), spend caps, and the Rate Limits API pointer. *Primary source for Q1(b) + Q2 self-metering.*
3. **https://code.claude.com/docs/en/authentication** — Official Claude Code auth doc. Confirms subscription OAuth via `/login`, `claude setup-token` → 1-year `CLAUDE_CODE_OAUTH_TOKEN`, that these env creds apply to **the Agent SDK**, credential storage, and org login pinning. *Primary source for Q3 mechanism.*
4. **https://platform.claude.com/docs/en/api/errors** — Official error reference. Confirms **429 `rate_limit_error`** (same type for any rate limit) and the distinct **402 `billing_error`** (API-key/billing path only), plus the `{type,message,request_id}` error shape. *Primary source for Q4 error semantics.*
5. **https://dev.to/aviv_shaked/how-to-use-your-claude-promax-subscription-with-the-agent-sdk-python-typescript-4emi** — Practitioner walkthrough. Confirms the OAuth-token-into-Agent-SDK flow and, load-bearingly, the **individual-use restriction**: "the OAuth token is licensed for individual use … Do not ship a multi-user app on it … likely violates Anthropic's terms … as of April 2026 Anthropic actively blocks third-party harnesses that try to bridge subscription auth." *Primary source for the Q3 "catch."* (Secondary/community source — corroborated by the Help Center's per-user framing.)
6. **https://github.com/anthropics/claude-code/issues/55333** — Anthropic GitHub issue. Confirms the existence of `anthropic-ratelimit-unified-5h-status`/`-remaining`/`-reset` headers reflecting "the same 5-hour-window usage that `/usage` and claude.ai show," with `remaining` a **proportion (0.58), not dollars**. *Source for Q1(b) subscription-window headroom.* (Feature-request thread, not formal API ref — header semantics tagged inferred.)
7. **https://platform.claude.com/docs/en/manage-claude/usage-cost-api** (+ endpoint ref **platform.claude.com/docs/en/api/admin/cost_report**) — Admin Usage & Cost API. Confirms dollar+token reporting via `/v1/organizations/usage_report/messages` and `/v1/organizations/cost_report`, authenticated with an **Admin API key `sk-ant-admin-*`** (org-level, not subscriber-OAuth). *Source for Q2 contrast + the enterprise/API-key lane.* (Reached via live WebSearch this session.)
8. **https://www.digitalapplied.com/blog/anthropic-claude-credit-overhaul-june-15-2026** and **https://thenewstack.io/anthropic-agent-sdk-credits/** — 2026-06 write-ups of the announcement; corroborate the paused credit's shape ($20 Pro / $100 Max5x / $200 Max20x, per-user, monthly, **non-rollover**, API-rate-billed). *Supporting sources for the paused-design details in Q4.* (Secondary; the pause itself is confirmed by source #1.)
