import { expect, test, request as pwRequest } from '@playwright/test'
import {
  assertAllowedStagingHost,
  assertResponseOnAllowedOrigin,
  resolveStagingBaseURL,
} from './staging/stagingConfig.mjs'
import { captureStagingIdentity } from './staging/stagingIdentity.mjs'

// Card B-U8 acceptance oracle:
//   Two real tenants: A creates/clones/exports; B sees nothing of A's;
//   revoked B-member loses access next read. Runs against staging in the
//   acceptance shape; doubles as the lifecycle_ui flip-time proof.
//
// The lifecycle_ui React surface (web/src/projects/*, cards B-U1..B-U7) is
// not yet mounted to any route — there is no matching entry in
// web/src/site/router.js today, so no real browser has anything to click
// yet. This harness instead drives the exact HTTP contract that surface
// calls through web/src/projects/api.js (platform/api.py's live
// /api/projects* routes), which IS deployed on staging today. Once
// lifecycle_ui is wired to a route, that wiring change can layer page-level
// assertions on top of this file without touching the tenant-isolation
// assertions below, which is why this file lives outside e2e/staging/ and
// resolves its own staging base URL rather than depending on
// playwright.staging.config.mjs's testDir scoping.
//
// Deliberately does NOT use Playwright's config-scoped `request`/`page`
// fixtures for the tenant calls below: this file may be discovered by
// playwright.config.mjs (testDir './e2e', baseURL 127.0.0.1:5185) as well as
// playwright.staging.config.mjs, so every HTTP call here opens its own
// APIRequestContext pinned to the real, host-allowlisted staging origin.
//
// Real, never-run-yet credentials (mirrors e2e/staging/auth-required.spec.mjs
// precedent: this file is skip-gated on real secrets rather than fabricating
// a pass). Three live staging identities are required:
//   LEAF_E2E_STAGING_JWT_TENANT_A        — tenant A's owner bearer token
//   LEAF_E2E_STAGING_JWT_TENANT_B        — tenant B's owner bearer token
//   LEAF_E2E_STAGING_JWT_TENANT_B_MEMBER — a second real user's bearer token,
//                                           already provisioned inside B's org
//   LEAF_E2E_STAGING_TENANT_B_MEMBER_BINDING_ID — that second user's
//                                           platform binding_id (UUID), which
//                                           B's owner needs to invite them by
//                                           (platform/api.py has no
//                                           binding-by-email lookup route)
const TENANT_A_JWT = process.env.LEAF_E2E_STAGING_JWT_TENANT_A || ''
const TENANT_B_JWT = process.env.LEAF_E2E_STAGING_JWT_TENANT_B || ''
const TENANT_B_MEMBER_JWT = process.env.LEAF_E2E_STAGING_JWT_TENANT_B_MEMBER || ''
const TENANT_B_MEMBER_BINDING_ID = process.env.LEAF_E2E_STAGING_TENANT_B_MEMBER_BINDING_ID || ''
const HAVE_CREDENTIALS = Boolean(
  TENANT_A_JWT && TENANT_B_JWT && TENANT_B_MEMBER_JWT && TENANT_B_MEMBER_BINDING_ID,
)

function bearerHeader(jwt) {
  return { Authorization: `Bearer ${jwt}` }
}

// Fresh per call, like web/src/projects/api.js's own idempotencyKey() — a
// retried assertion must never replay a prior mutation's receipt.
function idempotencyKey(label) {
  const uuid = globalThis.crypto?.randomUUID?.()
  return `b-u8-${label}-${uuid || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`
}

test.describe('B-U8: two-tenant project lifecycle proof against staging', () => {
  test.skip(
    !HAVE_CREDENTIALS,
    'LEAF_E2E_STAGING_JWT_TENANT_A, LEAF_E2E_STAGING_JWT_TENANT_B, '
    + 'LEAF_E2E_STAGING_JWT_TENANT_B_MEMBER, and '
    + 'LEAF_E2E_STAGING_TENANT_B_MEMBER_BINDING_ID are not all set; skipping '
    + 'the two-tenant lifecycle proof today',
  )

  let baseURL
  let anonymousApi
  let apiA
  let apiB
  let apiBMember

  test.beforeAll(async () => {
    baseURL = resolveStagingBaseURL()
    assertAllowedStagingHost(baseURL)
    anonymousApi = await pwRequest.newContext({ baseURL })
    apiA = await pwRequest.newContext({ baseURL, extraHTTPHeaders: bearerHeader(TENANT_A_JWT) })
    apiB = await pwRequest.newContext({ baseURL, extraHTTPHeaders: bearerHeader(TENANT_B_JWT) })
    apiBMember = await pwRequest.newContext({
      baseURL, extraHTTPHeaders: bearerHeader(TENANT_B_MEMBER_JWT),
    })
  })

  test.afterAll(async () => {
    await anonymousApi?.dispose()
    await apiA?.dispose()
    await apiB?.dispose()
    await apiBMember?.dispose()
  })

  test('A creates/clones/exports; B sees nothing of A\'s; a revoked B member loses access on its next read', async () => {
    const identity = await captureStagingIdentity(anonymousApi)
    expect(identity.source_revision).toBeTruthy()

    // --- tenant A creates a blank project ---------------------------------
    const createA = await apiA.post('/api/projects/blank', {
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey('create-a') },
      data: { name: `B-U8 tenant A ${Date.now()}` },
    })
    assertResponseOnAllowedOrigin(createA, process.env)
    expect(createA.status()).toBe(201)
    const projectA = (await createA.json())?.project
    expect(typeof projectA?.project_id).toBe('string')
    const projectAId = projectA.project_id

    // --- tenant A clones it -------------------------------------------------
    const cloneA = await apiA.post(`/api/projects/${projectAId}/clone`, {
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey('clone-a') },
      data: { name: `B-U8 tenant A clone ${Date.now()}` },
    })
    assertResponseOnAllowedOrigin(cloneA, process.env)
    expect(cloneA.status()).toBe(201)
    const clonedA = (await cloneA.json())?.project
    expect(typeof clonedA?.project_id).toBe('string')
    expect(clonedA.project_id).not.toBe(projectAId)
    const clonedAId = clonedA.project_id

    // --- tenant A exports the original --------------------------------------
    const exportA = await apiA.post(`/api/projects/${projectAId}/export`, {
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey('export-a') },
    })
    assertResponseOnAllowedOrigin(exportA, process.env)
    expect(exportA.status()).toBe(200)
    const exportedA = await exportA.json()
    expect(exportedA?.receipt?.receipt_id).toBeTruthy()

    // --- tenant B sees NONE of tenant A's projects (list) -------------------
    const listB = await apiB.get('/api/projects')
    assertResponseOnAllowedOrigin(listB, process.env)
    expect(listB.status()).toBe(200)
    const bVisibleIds = ((await listB.json())?.projects || []).map((p) => p.project_id)
    expect(bVisibleIds).not.toContain(projectAId)
    expect(bVisibleIds).not.toContain(clonedAId)

    // --- tenant B sees NONE of tenant A's projects (direct read: 404, never
    // 403 — a cross-org read must never leak existence) ----------------------
    const readOriginalAsB = await apiB.get(`/api/projects/${projectAId}`)
    assertResponseOnAllowedOrigin(readOriginalAsB, process.env)
    expect(readOriginalAsB.status()).toBe(404)

    const readClonedAsB = await apiB.get(`/api/projects/${clonedAId}`)
    assertResponseOnAllowedOrigin(readClonedAsB, process.env)
    expect(readClonedAsB.status()).toBe(404)

    // --- tenant B creates its own project and invites a real second B member
    const createB = await apiB.post('/api/projects/blank', {
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey('create-b') },
      data: { name: `B-U8 tenant B ${Date.now()}` },
    })
    assertResponseOnAllowedOrigin(createB, process.env)
    expect(createB.status()).toBe(201)
    const projectBId = (await createB.json())?.project?.project_id
    expect(typeof projectBId).toBe('string')

    const invite = await apiB.post(`/api/projects/${projectBId}/members`, {
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey('invite-b') },
      data: { binding_id: TENANT_B_MEMBER_BINDING_ID, role: 'read_only' },
    })
    assertResponseOnAllowedOrigin(invite, process.env)
    expect(invite.status()).toBe(201)
    const membershipId = (await invite.json())?.member?.membership_id
    expect(typeof membershipId).toBe('string')

    // the invited member can read the project before revocation
    const memberReadBefore = await apiBMember.get(`/api/projects/${projectBId}/lifecycle`)
    assertResponseOnAllowedOrigin(memberReadBefore, process.env)
    expect(memberReadBefore.status()).toBe(200)

    // --- the owner revokes that member ---------------------------------------
    const revoke = await apiB.delete(`/api/projects/${projectBId}/members/${membershipId}`, {
      headers: { 'Idempotency-Key': idempotencyKey('revoke-b') },
    })
    assertResponseOnAllowedOrigin(revoke, process.env)
    expect(revoke.status()).toBe(200)

    // --- the revoked member loses access on its NEXT read (not merely a
    // stale cached state — a fresh request against the live server) --------
    const memberReadAfter = await apiBMember.get(`/api/projects/${projectBId}/lifecycle`)
    assertResponseOnAllowedOrigin(memberReadAfter, process.env)
    expect(memberReadAfter.status()).toBe(403)
  })
})
