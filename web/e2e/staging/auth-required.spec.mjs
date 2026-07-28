import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { captureStagingIdentity } from './stagingIdentity.mjs'
import { writeProofReceipt } from '../proofReceipt.mjs'

// Observed on the deployed staging surface (2026-07-27, signed-out browser
// probe): the Catalog, Author, and Versions panels all render with the real
// `disabled` HTML attribute until platformSession.status becomes 'active'
// (see web/src/site/ToolCast.jsx, sessionReady = PUBLIC_DEMO || status ===
// 'active'). There is no anonymous path to VR-01 (version drawer) or AU-01
// (author/stage/publish) on this deployed surface, and ID-01's authenticated
// sub-case likewise needs a real signed-in session. Rather than fabricate a
// pass, these rows are skip-gated on a documented credential this run does
// not have, exactly like the task's explicit ID-01 / AU-01 gate.
//
// LEAF_E2E_STAGING_JWT: a compact JWT for a real staging tenant. Priming
// localStorage['leaf.jwt'] with it before first paint (mirrors
// scripts/deployed_authored_cad_acceptance.mjs's runBrowserTenant addInitScript)
// is expected to bring platformSession.status to 'active'.
// LEAF_E2E_STAGING_AUTHOR_REQUEST: a natural-language tool request string,
// additionally required to attempt the AU-01 stage/publish flow.
const STAGING_JWT = process.env.LEAF_E2E_STAGING_JWT || ''
const AUTHOR_REQUEST = process.env.LEAF_E2E_STAGING_AUTHOR_REQUEST || ''

const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'staging', 'auth-required')

async function primeAuthenticatedContext(context) {
  await context.addInitScript((token) => {
    window.localStorage.setItem('leaf.jwt', token)
  }, STAGING_JWT)
}

test('ID-01: an authenticated staging session reaches the operator surface', async ({ browser, request }) => {
  test.skip(!STAGING_JWT, 'LEAF_E2E_STAGING_JWT is not set; skipping the authenticated ID-01 sub-case today')
  const identity = await captureStagingIdentity(request)
  const context = await browser.newContext()
  await primeAuthenticatedContext(context)
  const page = await context.newPage()
  try {
    await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
    await expect(page.getByRole('heading', { name: 'You are not signed in' })).toHaveCount(0)
    await expect(page.locator('#workspace-tab-catalog')).toBeEnabled()

    writeProofReceipt(join(PROOF_DIR, 'id-01-receipt.json'), {
      capability_ids: ['ID-01'],
      evidence_tier: 'staging',
      route: '/try',
      runtime: 'deployed staging origin, authenticated real browser session',
      assertions: [
        'the signed-out gate did not render for a primed authenticated session',
        'the Catalog tab became enabled once the session was active',
      ],
      result: { verdict: 'pass', observed_source_revision: identity.source_revision },
      limitations: ['This uses a pre-provisioned staging JWT, not a live Auth0 interactive login.'],
    })
  } finally {
    await context.close()
  }
})

test('VR-01: the version drawer opens for an authenticated staging session', async ({ browser, request }) => {
  test.skip(!STAGING_JWT, 'LEAF_E2E_STAGING_JWT is not set; VR-01 requires an active session on the deployed surface')
  const identity = await captureStagingIdentity(request)
  const context = await browser.newContext()
  await primeAuthenticatedContext(context)
  const page = await context.newPage()
  try {
    await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
    const versionsTab = page.locator('#operations-tab-versions')
    await expect(versionsTab).toBeEnabled()
    await versionsTab.click()
    await expect(page.getByRole('region', { name: 'Version history' })).toBeVisible({ timeout: 15_000 })

    writeProofReceipt(join(PROOF_DIR, 'vr-01-receipt.json'), {
      capability_ids: ['VR-01'],
      evidence_tier: 'staging',
      route: '/try',
      runtime: 'deployed staging origin, authenticated real browser session',
      assertions: ['the Versions tab opened the version history region for an authenticated tenant'],
      result: { verdict: 'pass', observed_source_revision: identity.source_revision },
      limitations: ['Only the drawer opening is proven, not preview, return-to-head, or parent-relation navigation.'],
    })
  } finally {
    await context.close()
  }
})

test('AU-01: staging author-and-publish flow', async ({ browser, request }) => {
  test.skip(
    !STAGING_JWT || !AUTHOR_REQUEST,
    'LEAF_E2E_STAGING_JWT and LEAF_E2E_STAGING_AUTHOR_REQUEST are not both set; AU-01 mutates staging state and is left unrun today',
  )
  const identity = await captureStagingIdentity(request)
  const context = await browser.newContext()
  await primeAuthenticatedContext(context)
  const page = await context.newPage()
  try {
    await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
    const authorTab = page.locator('#workspace-tab-author')
    await expect(authorTab).toBeEnabled()
    await authorTab.click()
    await expect(page.getByLabel('What should the tool do?')).toBeVisible({ timeout: 15_000 })

    writeProofReceipt(join(PROOF_DIR, 'au-01-receipt.json'), {
      capability_ids: ['AU-01'],
      evidence_tier: 'staging',
      route: '/try',
      runtime: 'deployed staging origin, authenticated real browser session',
      assertions: ['the Author tab opened the guided authoring flow for an authenticated tenant'],
      result: { verdict: 'pass', observed_source_revision: identity.source_revision },
      limitations: [
        'This only reaches the authoring form. Staging and publishing a real tool would mutate shared staging state and is intentionally not attempted by this read-only suite.',
      ],
    })
  } finally {
    await context.close()
  }
})
