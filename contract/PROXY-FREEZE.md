# Console proxy freeze: header allowlist + error vocabulary (wave 1 lane B2)

Status: FROZEN. Scope: the AUTHENTICATED console proxy at leaf_website
`app/api/app/[...path]/route.ts` only (the seam every signed-in console API
call rides). The signed-out guest seam (`/api/guest`, CONTRACT §19, with its
own `X-Guest-Session` and validated `X-Tenant-Id` headers) is a separate
proxy with its own freeze, documented with the §19 guest-upload work; it is
NOT covered by the lists below. Changing either list below requires the
operator-promotion ritual and a matching update in both repos the same day.
Date: 2026-07-22.

## 1. Request header allowlist (route.ts:91-101)

The ONLY client request headers ever forwarded upstream (everything else is
dropped, named or not):

1. `content-type`
2. `accept`
3. `accept-language`
4. `user-agent`
5. `x-request-id`
6. `x-org-id` (validated upstream against the resolved tenant; carries no auth)
7. `x-project-id` (same)

Plus the server-minted headers, never copied from the client (route.ts:52-57):

- `Authorization: Bearer <token>`, attached after entitlement passes.
- `X-Ops-Secret`, attached only on internal ops requests after the
  internal-only gate (route.ts:366; env sourcing and refusal at :335-338).

Note: census #3 called this an "8-header allowlist". Measured today the
allowlist is 7 entries plus the server-minted headers above. This document
freezes the measured list; the census figure counted Authorization.

Defense in depth (also frozen):

- Hop-by-hop headers never forwarded (route.ts:106-117): connection,
  keep-alive, te, trailer, trailers, transfer-encoding, upgrade,
  proxy-authenticate, proxy-authorization, proxy-connection.
- Response headers stripped before reaching the client (route.ts:120-126):
  content-encoding, content-length, connection, transfer-encoding,
  set-cookie (upstream is not the session's cookie owner).

## 2. Proxy error vocabulary (route.ts:269-383)

The proxy emits exactly these machine-readable error tokens; the console
(`console/api.js`) may branch only on these plus the CONTRACT.md §10 enum
passed through from upstream:

| token | status | meaning |
|---|---|---|
| `proxy_misconfigured` | 500 | LEAF_PLATFORM_API_URL unset (route.ts:269) |
| `unauthorized` | 401 | no session (route.ts:278) |
| `entitlement_unavailable` | 503, `retryable: true` | entitlement source cannot be trusted to answer; deny, never allow (route.ts:293, fail-closed per :211) |
| `console_entitlement_required` | 403 | signed in but not entitled; carries `reason` (route.ts:301) |
| `ops_forbidden` | 403 | ops path without internal role (route.ts:316) |
| `reauth_required` | 401 | token mint failed or expired session (route.ts:327,330) |
| `ops_unavailable` | 503 | ops secret path unavailable (route.ts:340) |
| `upstream_unreachable` | 502 | backend did not answer (route.ts:383) |

Upstream bodies pass through untouched: the CONTRACT.md §10 envelope
(`error.error_code`, `degraded_mode`) is the backend's voice and the proxy
never rewrites it.

## 3. Freeze scope

- Adding a forwarded header, a new proxy error token, or a rewrite of
  upstream envelopes = contract change: operator promotion + same-day update
  of this doc and route.ts, plus the console consumer.
- Removing entries fail-closes silently and is equally a contract change.
- The entitlement cache behavior (TTL 5 min, max 500 entries,
  route.ts:132-134) is an implementation detail, not contract, and may tune
  freely.
