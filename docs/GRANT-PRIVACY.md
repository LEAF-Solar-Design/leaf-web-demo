# How Leaf handles Claude credentials

Status: Staging commercial lane review, 2026-07-27.

Audience: enterprise security reviewers. The controls below are implemented in
`server/routers/tenant.py`, `harness/src/ports/impl/oauthGrantProvider.ts`,
`harness/src/agent/spineTurnAdapter.ts`, and their focused tests.

## Terms boundary

This lane accepts Claude Team or Enterprise setup tokens under Anthropic's
Commercial Terms and Claude for Work terms. It also accepts customer-owned
Anthropic API keys. It does not accept consumer Free, Pro, or Max credentials
for automatic routing.

The tenant owner must attest the Team or Enterprise plan when mounting an OAuth
credential. This is a product control, not proof of the customer's contract.
Leaf must keep the current terms and its own Anthropic agreement under review.

Primary terms reviewed on 2026-07-27:

- https://www.anthropic.com/legal/commercial-terms
- https://www.anthropic.com/legal/service-specific-terms
- https://code.claude.com/docs/en/legal-and-compliance

## What Leaf stores

Leaf stores one private atomic v3 JSON record per tenant. It contains:

- one or more Team or Enterprise OAuth setup tokens, or customer API keys;
- an opaque account id, owner-supplied label, credential kind, plan attestation,
  and link time;
- token-free routing state: settled token usage, selection count, last use,
  short leases, and quota cooldown.

The app and browser never store the credential. Status responses contain only
the token-free account inventory.

## Who can manage mounts

Only the active platform tenant owner can list, add, select, diagnose, or remove
mounts. The app resolves the current server-owned identity binding and reads the
role again before it contacts the harness. Revoked, moved, stale, and non-owner
bindings fail closed.

Auth-off local and hermetic test paths keep the legacy behavior. Staging and
production use live authorization.

## Where credentials live

The record lives under `LEAF_GRANTS_DIR` inside the harness container. New
writes use a mode-0600 temporary file, file fsync, atomic rename, and directory
fsync where the operating system supports it. Only the harness mounts this
directory. The app, web, and broker containers cannot read it.

The file store is the staging backend. `LEAF_GRANT_STORE=vault` is a fail-closed
production seam. The harness refuses to start if that backend is requested
before it is implemented.

## How a credential moves

The browser sends a credential once to
`POST /api/tenant/claude-grant`. The app forwards it once to the harness over
the authenticated internal hop and then forgets it. The app never logs,
persists, or echoes the credential.

The harness injects exactly one selected credential into a scrubbed Agent SDK
child environment. Known ambient Anthropic identities and credential-like
environment keys are removed first. Registered CAD tools run without LLM
involvement, so normal tool execution never receives the credential.

## Automatic routing

For each live conversational turn, the harness considers only eligible mounts
from that tenant's record. It selects the lowest settled token usage, then uses
selection count and stable account fields as deterministic tie breakers.

The harness creates a short lease before the turn. That lease reserves estimated
capacity so simultaneous turns in one staging harness do not choose the same
mount. When the turn ends, Leaf removes the lease and settles the actual
cost-relevant token count.

A long-horizon quota response places only the selected mount in cooldown. Leaf
does not retry a completed or partly visible turn. It does not pool credentials
across tenants and does not use rotation to bypass provider limits. Ephemeral
per-turn credentials never enter the tenant pool or its usage records.

## Logs and telemetry

No designed log path contains a credential. Harness error and status logs use
the shared redactor in `harness/src/redact.ts`. Errors that cross the durable
transcript boundary are scrubbed by value before they leave the runner.

Leaf records token counts, estimated cost, selected account id, and cooldown
state. It never records the credential value in telemetry.

## Removal

`DELETE /api/tenant/claude-grant?account_id=...` removes one mount. Deleting
the last mount removes the tenant record. The response reports the actual
token-free post-delete state.

The local demo tenant can have a documented environment fallback for backwards
compatibility. Commercial staging tenants do not use that fallback.
