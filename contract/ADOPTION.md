# Platform API adoption law (contract pack, wave 1 lane B2)

Status: FROZEN on adoption. Changes go through the operator-promotion ritual
(census #3). Date: 2026-07-22.

## 1. The envelope and error taxonomy are platform law

Every client of the leaf platform API speaks the CONTRACT.md envelope and
error taxonomy. That means:

- The console frontend (leaf_website `/app` + its proxy).
- The Branch2025 plugin surfaces (dock WebView, host bridge callers).
- Every future client, first party or third party.

Authority:

- Envelope + frozen `error_code` enum: `contract/CONTRACT.md` §10
  (`UNKNOWN_TOOL, BAD_PARAMS, APS_UNAVAILABLE, BROKER_UNREACHABLE,
  WORKITEM_FAILED, TIMEOUT, TENANT_DISABLED, INTERNAL`).
- Promoted additions: `grant_required` / `GRANT_REQUIRED` mapping
  (`server/CONTRACT-ADDENDUM.md:550,570-577`) and the entitlement vocabulary
  the gate suites assert (census #3 records promotion at 9e498ce).
- Validation errors (422) are enveloped as `BAD_PARAMS` (CONTRACT.md:231).
- `degraded_mode` semantics: CONTRACT.md:226-228. A fallback run must say so.

## 2. Explicit exclusion: ops-dashboard `responses.ts`

The census merge ruling (census-backlog.md, "API envelope conflict named")
stands: the ops-dashboard `responses.ts` envelope does NOT apply to platform
APIs. It stays ops-dashboard internal. No platform endpoint, client, or test
may adopt it.

## 3. Solver naming law (operator ruling R-A, 2026-07-22)

Two implementations of string autofill exist. The ruling:

- The heuristic keeps the name `autofill-string-targets`.
- The real optimizer registers under its own name: `string-autofill-opt`.
- Both live in the `stringing` capability family.
- Every run result must disclose which solver ran (provenance line in the
  result envelope; console renders it per the solver-choice disclosure row of
  the element inventory).
- Same-name precedence resolution is FORBIDDEN. Two tools may never share a
  registry name; registration of a duplicate name is a hard error, not a
  precedence contest.

This closes the name-collision gate raised at rescue-merge time
(port-matrix.md, honesty flags).
