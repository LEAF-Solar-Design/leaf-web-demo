# Studio service admin browser login — stage A

This source slice implements the server and the protected Windows approval CLI.
It does **not** establish a working Studio application browser session: stage B
must consume and remove `service_session` from the return URL, keep the public
binding in per-tab sessionStorage, probe `/api/auth/agent/session`, and reconcile
all existing JWT consumers. No frontend file is changed in stage A. Do not count
this slice as admin activation, C01 admission, or an original18 workflow pass.

## Provisioning boundary

The operator owns deployment, canonical migrations, credentials and live proof.
Run migration `0101_service_browser_sessions.sql` through the normal migration
runner; both lists in `platform/authority-inventory.json` include it. It adds
two independent custody tables without modifying existing identities or grants.
Missing tables, unavailable PostgreSQL, invalid configuration or failed crypto
deny access. There is no sqlite/in-memory authority fallback or startup DDL.

Enable only with all of:

- `LEAF_SERVICE_BROWSER_LOGIN_ENABLED=1` (unset is disabled).
- `LEAF_AUTH_LIVE=1` and `LEAF_RUNTIME_ENV` exactly `staging` or `production`.
- `LEAF_SERVICE_BROWSER_SESSION_KEY`, an independently provisioned Fernet key.

Do not reuse a dispatch secret, JWT key or other application secret. This code
never provisions a key. Rotating the key makes outstanding encrypted custody
unreadable and fails closed; disable the flag for immediate feature rollback.
Rolling the image back leaves additive tables available to the older image.
Expiry is enforced in queries, not by a cleanup daemon. Root should choose a
retention/deletion policy for expired challenges and sessions separately.

Fixed HTTPS origins are `https://platform-staging.leafdesign.ai` and
`https://platform.leafdesign.ai`. The ingress must preserve that exact Host.
Forwarded host headers grant nothing. Provided Origin must match exactly;
browser poll and logout require the matching Origin header. No CORS permission,
provider session, operator signin, password or browser-token injection is added.

## Flow and protocol

1. Visit `/api/auth/agent/login` on the target host. A visible service admin
   page displays only a public UUID and origin, with a nonce-based CSP and
   bounded polling. A separate 120-second nonce is stored in a Secure HttpOnly
   SameSite=Strict host-only `__Host-leaf-agent-pending` cookie, Path=/.
   PostgreSQL stores its SHA256 digest only.
2. Approve the UUID from the matching Windows automation account:

   ```text
   python scripts/studio_agent_login.py approve --environment staging --challenge-id UUID --expect-source SOURCE40
   ```

   `SOURCE40` must be the exact canonical released source SHA expected by root.
   The CLI fetches fresh `/api/health` and requires its `release_source_sha` and
   `source_identity` (`built` or `adopted`), then sanitized challenge metadata,
   before reading credentials. It rechecks source immediately before approval.
   This is HTTPS source evidence, not a digest-attested live release proof.
3. `/approve` accepts only a genuine Auth0 RS256 machine-admin Bearer. Existing
   `deps.require_tenant` and admin allowlist/current binding checks apply;
   machine sub/azp, namespaced service marker and admin tier must also match.
   No roles, tenant grants, operator authority or project state are written.
   The real token is Fernet-sealed before storage. Atomic approval fails 409
   if missing, expired, foreign, approved or consumed.
4. The original browser's POST `/challenge/UUID/poll` must prove its nonce.
   A conditional UPDATE and session INSERT share one PG transaction. Only an
   approved/unconsumed/unexpired challenge with the exact nonce hash can win.
   Session authority is a new random 256-bit secret; only SHA256 is stored.
   The server sets `__Host-leaf-agent-session` with the same cookie protections
   and returns `/?service_session=PUBLIC_BINDING_UUID`, clearing pending custody.
   The database deadline is the earlier of 900 seconds and token expiry. Cookie
   max-age is at most 900 seconds; it cannot extend database/token validity.
5. Stage B clients must send `Authorization: LeafServiceSession PUBLIC_BINDING_UUID`
   plus the cookie. The public binding alone and the cookie alone do not
   authenticate. The ASGI middleware translates this scheme only on `/api/`
   requests, after DB binding/environment/origin/expiry/revocation checks and
   authenticated decryption. It strips competing demo/guest/dispatch headers so
   the unchanged canonical JWT gates always run. Ordinary Bearer requests are
   untouched, including with a stale service cookie. `/internal` is excluded.
6. GET `/session` requires joint proof and normal machine admin authorization;
   it returns only authenticated status, sanitized subject/tenant/type, deadline
   and public binding. POST `/logout` additionally requires exact Origin, revokes
   only the matching binding+secret, and clears the cookie. Stage B must clear
   tab readiness and invoke existing beforeLogout cleanup immediately even if
   logout fails, preserving other tabs' human credentials and Auth0 callbacks.

The host cookie is shared by tabs. Starting a second service login replaces it;
older selectors then fail joint proof. Tabs must never infer identity from a
selector, cookie presence or a synthetic JWT. No JWT appears in any successful
endpoint response, redirect, HTML, public status or browser JavaScript object.

## Windows credential package contract

The CLI reads only `%LOCALAPPDATA%/LeafAutomation/StudioAgentAdmin/{environment}.dpapi`.
Root provisions raw DPAPI CurrentUser ciphertext (no entropy) whose UTF-8 JSON is:

```json
{
  "schema": "leaf.studio-agent-admin.v1",
  "environment": "staging",
  "domain": "leafautomation.us.auth0.com",
  "client_name": "StudioAgentAdmin",
  "client_id": "OPERATOR_PROVISIONED_CLIENT_ID",
  "client_secret": "OPERATOR_PROVISIONED_SECRET",
  "audience": "https://api-staging.leafdesign.ai"
}
```

Production uses environment `production` and audience `https://api.leafdesign.ai`.
The purpose-named sealed package pins the selected client's ID. No real client
ID or credential package was read or confirmed during implementation. Root must
confirm this package contract against the actual credential provisioner before
live use. The minted token's sub must be that exact ID plus `@clients`, and azp
must be that exact ID; the Action's service-admin marker and admin tier must be
present. The server's existing admin subject allowlist is a separate authority.

DPAPI runs in an isolated, noninteractive, profile-free PowerShell child with a
minimal environment. Ciphertext enters stdin; cleartext is captured in-process.
Neither cleartext nor ciphertext enters argv, logs, inherited environment or
temporary files. Stdout/stderr are captured; failures print only a generic JSON
receipt. Fixed issuer `https://leafautomation.us.auth0.com/`, JWKS and HTTPS
origins cannot be overridden by CLI/environment. Redirects are refused before
any follow-up request. Proxies and ambient cookie/credential stores are unused.
Tokens are locally checked with RS256/JWKS, issuer, audience, expiry and exact
service identity before approval. Secrets exist transiently in process memory;
Python does not guarantee zeroization of immutable strings.

## Evidence and remaining proof

Planner-managed source verification:

```text
python -P -m pytest -o pythonpath=server server/test_auth.py -q
```

The new tests are explicitly unit evidence: signed test JWTs, isolated routing,
crypto, scripted database cursor decisions/SQL predicates and fake CLI I/O. They
do not prove a live PG transaction race, run DPAPI, read real credentials or
contact Auth0. Fake transaction contexts cover rollback on tampered custody or
failed session insertion; CLI fakes cover source/metadata checks before credential
access and the fresh source check before bearer approval. These remain unit
evidence, not database or network proofs. The visible page aborts pending poll
requests at its 120-second deadline; malformed public lookups return sanitized
nonexistence without database access. Remaining root-owned proof includes canonical migration/rollback
compatibility, real PG parallel approval/consume and revocation, independent key
provisioning, Windows DPAPI credential compatibility, exact source deployment,
stage B per-tab/human compatibility tests, supported browser login and runtime
tenant/allowlist revocation. The broader contract/telemetry/consumer rows remain
unresolved until root supplies their required evidence references.
