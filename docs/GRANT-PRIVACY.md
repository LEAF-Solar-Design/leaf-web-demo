# How the platform handles your Claude credential

Status: DRAFT for enterprise review (census #13, NL-build lane, 2026-07-22).
Audience: enterprise security reviewers. Every claim below names the code or
test that enforces it. Contract references: `server/CONTRACT-ADDENDUM.md`
sections 15 to 17 (FROZEN) and `harness/contract/HARNESS-CONTRACT.md` (FROZEN).

## What we store

One credential per tenant, and nothing else:

- either a "sign in with Claude" OAuth token (web lane), or an API key your
  company brings (enterprise lane, the recommended path);
- a one-word record of which kind it is (`oauth` or `api_key`);
- the file's own modification time, which we report as `linked_at`.

We store no other Anthropic account data. We never derive, cache, or copy the
credential anywhere else.

## Where it lives

The credential lives in one file per tenant (`<tenant>.token`, mode 0600) plus
a kind sidecar (`<tenant>.kind`) under `LEAF_GRANTS_DIR`, inside the harness
container only (`harness/src/ports/impl/oauthGrantProvider.ts`,
`FileTenantGrantStore`). In the container stack this directory is the
`leaf-grants` volume. Only the harness mounts it. The app, web, and broker
containers cannot read it. The harness runs as a dedicated non-root user
(uid 10002, `deploy/Dockerfile.harness`).

The file store is the default backend. Production deployments can select a
sealed secret store (vault or DPAPI) through the `LEAF_GRANT_STORE` seam. If an
operator requests `vault` before it is wired, the harness refuses to start; it
never falls back to disk silently (`createTenantGrantStore`, pinned by
`harness/test/grantStore.test.ts`).

## How it moves

You submit the credential once, to the app (`POST /api/tenant/claude-grant`).
The app forwards it in that one request to the harness over an authenticated
internal hop (`X-Harness-Secret`, shared secret `LEAF_HARNESS_SECRET`,
fail-closed) and then forgets it. The app never persists, logs, or echoes the
credential (`server/routers/tenant.py`, asserted by `server/tests/test_wave4.py`).

Every status or link response carries only `{linked, linked_at, kind}` and
never the credential (`grantStore.test.ts`, `grantAdmin.e2e.test.ts`, and the
containerized smoke `scripts/harness-container-smoke.py`).

## When we use it

The credential is used only for your own tenant's work: authoring a tool at
design time, or driving your conversational session. For each such request the
harness injects it into a scrubbed child environment that carries only that
one variable (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`), and the only
network egress on that path is Anthropic, through the official Agent SDK
(`agentSdkRunner.ts`). Registered tools run with zero LLM involvement, so
normal tool execution never touches the credential at all (HARNESS-CONTRACT
section 4, enforced by `converseRuntimeSeparation.test.ts`).

Credentials are individual-use. One credential serves one tenant. We never
pool one credential across tenants, and the store is keyed by a validated
tenant id that cannot escape its directory (`tenant_id_validator.py` mirrored
in the TS store).

## What we log

Nothing that contains the credential. The harness never prints a token; the
serve path additionally redacts any token-shaped string from everything it
emits (`serve.ts`). The internal hop secret is env-only and never logged. The
containerized smoke asserts both: after a full link, author, restart, and
unlink cycle, neither the credential nor the hop secret appears anywhere in
the container logs.

We do keep usage telemetry (token counts and estimated cost from each SDK
response). Telemetry never includes credentials.

## How you remove it

`DELETE /api/tenant/claude-grant` deletes the token file and its kind sidecar
immediately. There are no other copies, so deletion is complete. Destroying
the `leaf-grants` volume (`docker compose down -v`) removes every stored
credential at once. We make no backups of the grant volume; if your operators
add volume backups, those backups inherit this document's obligations.

## Open policy question (not a code gap)

Whether Anthropic's consumer terms permit a hosted, stranger-facing service to
author tools on end users' own subscription OAuth tokens is a POLICY question
that remains open. It gates a stranger-facing launch only. The enterprise
lane does not depend on it: bring your own API key, which your agreement with
Anthropic already covers.
