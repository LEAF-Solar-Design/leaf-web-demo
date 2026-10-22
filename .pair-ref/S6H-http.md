# S6H: Scoped integration HTTP services

Parent: Phase 1 of the shared Ops/Studio/Mushy platform. Expose the verified application service through a finite `/api/v1/projects/{projectId}/integrations` surface. This is a host API, not a parallel policy engine.

Worktree: C:/tmp/wt/ops-dashboard/S6-http. Exclusive ownership:
- src/lib/integrations/platform/http.ts (new)
- src/lib/integrations/platform/routes.ts (new, edge-safe pure route matcher)
- src/lib/integrations/platform/__tests__/http.test.ts (new)
- src/lib/integrations/platform/__tests__/discovery.test.ts (new)
- src/app/api/v1/projects/[projectId]/integrations/[...resource]/route.ts (new)
- src/lib/integrations/platform/application-service.ts
- src/lib/api/workspace-api.ts
- src/lib/api/__tests__/workspace-api.test.ts
- src/middleware.ts
- src/lib/api/__tests__/workspace-api-middleware.test.ts

You are not alone. No edits outside ownership, shell, tests, provider calls, secrets, commits or delegation. Root runs verification. Do not alter database/vault/transport implementation.

## Finite route surface

Use one explicit route table with matching method, path segments and access. No arbitrary operation name dispatch. Slug/version validation uses canonical contracts. UUID project matches the existing scope route rules.

GET connectors: bounded page of authorized draft/published metadata.
POST connectors/import: `{document, options:{id,title,origin?}}`.
GET connectors/{id}: draft metadata.
GET connectors/{id}/versions/{version}: published definition, immutable reference and typed operation references.
POST connectors/{id}/publish: `{version, expectedRevision, readOperationIds}`.
GET connections: bounded connection summaries.
GET connections/{id}: summary with revision, auth kind, configured credential purposes, state, grants, health and cleanup status. Never secret values or encrypted envelopes.
POST connections: current createConnection input.
PUT connections/{id}/credential: `{purpose, value, expectedRevision}`. Value is transient host-controlled input, never a log or output. Bound to 16 KiB UTF-8, create Buffer once and fill in finally. Do not promise complete JavaScript heap erasure. Never reflect request validation data in errors.
DELETE connections/{id}: required numeric If-Match revision, no implicit current revision.
GET datasets: authorized bounded metadata page.
POST datasets: `{definition,mapping}`.
GET datasets/{id}/versions/{version}: metadata, optional revision query.
POST datasets/{id}/versions/{version}/sync: empty strict object, synchronous bounded result, propagate request.signal.
POST datasets/{id}/versions/{version}/query: canonical DatasetQueryV1.
GET elements: bounded page.
POST elements: canonical DatasetElementV1, create only.
GET elements/{id}: optional revision query.
PUT elements/{id}: canonical element plus required numeric If-Match revision; body id must match path.

List query allows only limit (1..100) and opaque after cursor. Reject duplicate query keys, unknown keys, malformed or out-of-range versions/revisions. No unbounded bulk route.

Use handleWorkspaceApi for authentication, CORS/CSRF, bounded body parsing, correlation IDs and private/no-store headers on every application path. Unsupported routes return safe 404, unsupported methods 405 with Allow, without touching resources. OPTIONS advertises only methods supported by that concrete path and performs no resource calls.

Instantiate IntegrationApplicationService from canonical scope after authorization. Required revalidateScope calls resolveWorkspaceScope with the same request/project and access, never an identity echo or client scope. Trusted test factory injection is not selected by query, environment bypass or headers.

Extend WorkspaceApiError with fixed integration categories/statuses: invalid input400, not found404, forbidden403, conflict409, unsupported operation422, unavailable503, cleanup required503. Preserve WorkspaceError authority/configuration categories. No raw error.message, SQL, URLs, credential references or provider response text. Handler output remains data, not an arbitrary Response escape from common headers.

Extend application service with get/list connector and connection discovery under fresh authority and existing isolated store. Owners/editors may inspect configuration, other project roles receive safe health/title summaries. Connector definitions and operation references contain no credentials and are safe for project readers. Compute operation digests by existing integrationDigest, never new hashing rules. Connection summaries omit credential reference IDs and cleanup pending IDs for all roles. Recheck read access before returning. Respect revoked state in discovery, showing health rather than hiding recovery information.

Fix the application saveElement axis check to accept number/count X as well as date/instant, matching the already verified shared renderer. Do not loosen Y semantic checks.

Middleware exemption imports the pure matcher and exempts ONLY the exact concrete registered integration routes for a valid UUID. Reject case changes, percent-encoded separators, trailing path extensions and unknown resource routes. Every exempt method reaches the canonical local authority helper. Do not exempt `/api/v1`, the whole projects prefix or unrelated legacy routes. Preserve existing scope route behavior.

## Acceptance

Test route dispatch with real authority and common request boundary, fake service only for wiring. Check invalid bearer never falls back, body/query cannot supply tenant/role, viewer mutations denied, cookie mutation missing/wrong Origin denied, valid bearer allowed, preflight never calls service, unsupported paths/methods, duplicate/oversize/invalid inputs, all error categories sanitized, body secret absent from responses and Buffer scrubbed on both success/failure, immutable IDs and If-Match binding.

Discovery tests use real service/store/migrations under restricted PGlite, two colliding organizations and role-reduced metadata. Reuse test fixture patterns, no mocked tenant filtering. Test numeric X acceptance and connection summaries containing no opaque credential IDs.

Verify: `npx vitest run src/lib/integrations/platform/__tests__/http.test.ts src/lib/integrations/platform/__tests__/discovery.test.ts src/lib/api/__tests__/workspace-api.test.ts src/lib/api/__tests__/workspace-api-middleware.test.ts`

High effort for request boundary, medium after frozen routes. 25-minute budget. Return actual artifacts and unmet criteria. Public hosted access, machines, SDK generation, UI and live provider proof follow separately.
