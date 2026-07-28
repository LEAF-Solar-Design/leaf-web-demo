import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { captureStagingIdentity } from './stagingIdentity.mjs'
import { writeProofReceipt } from '../proofReceipt.mjs'

// Observed on the deployed staging surface (2026-07-27, signed-out browser
// probe): the Catalog, Author, and Versions panels all render with the real
// `disabled` HTML attribute until platformSession.status becomes 'active'
// (see web/src/site/ToolCast.jsx, sessionReady = PUBLIC_DEMO || status ===
// 'active'). There is no anonymous path to CA-01 (real catalog data),
// VR-01 (version drawer), or AU-01-shaped authoring on this deployed
// surface, and ID-01's authenticated sub-case likewise needs a real
// signed-in session. Rather than fabricate a pass, these rows are
// skip-gated on a documented credential this run does not have.
//
// LEAF_E2E_STAGING_JWT: a compact JWT for a real staging tenant. Priming
// localStorage['leaf.jwt'] with it before first paint (mirrors
// scripts/deployed_authored_cad_acceptance.mjs's runBrowserTenant addInitScript)
// is expected to bring platformSession.status to 'active'.
//
// IMPORTANT -- these tests have never run for real. LEAF_E2E_STAGING_JWT has
// never been set in any run of this suite to date, so every assertion below
// is skip-gated code that has only been read, not exercised. The first real
// credentialed run of this file must be reviewed line by line against its
// actual receipts and screenshots before those receipts are trusted as
// STAGING_PROVEN evidence -- do not merge a green run of this file on trust
// alone the first time real credentials are supplied.
const STAGING_JWT = process.env.LEAF_E2E_STAGING_JWT || ''

const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'staging', 'auth-required')

async function primeAuthenticatedContext(context) {
  await context.addInitScript((token) => {
    window.localStorage.setItem('leaf.jwt', token)
  }, STAGING_JWT)
}

test('ID-01 + CA-01: an authenticated staging session reaches the operator surface and a real tool catalog', async ({ browser, request, baseURL }) => {
  test.skip(!STAGING_JWT, 'LEAF_E2E_STAGING_JWT is not set; skipping the authenticated ID-01/CA-01 sub-cases today')
  const identity = await captureStagingIdentity(request)
  const context = await browser.newContext()
  await primeAuthenticatedContext(context)
  const page = await context.newPage()
  const observedEndpoints = [identity.endpoint]
  const stagingOrigin = new URL(baseURL).origin
  page.on('response', (response) => {
    const url = new URL(response.url())
    if (url.origin === stagingOrigin) observedEndpoints.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
  })
  try {
    await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
    await expect(page.getByRole('heading', { name: 'You are not signed in' })).toHaveCount(0)
    await expect(page.locator('#workspace-tab-catalog')).toBeEnabled()

    // CA-01 at API level: the authenticated bearer must reach the real
    // catalog endpoint, not a client mock, and it must return at least one
    // real tool. This is a non-mutating GET.
    const toolsResponse = await request.get('/api/tools', {
      headers: { Authorization: `Bearer ${STAGING_JWT}` },
    })
    expect(toolsResponse.status()).toBe(200)
    const toolsBody = await toolsResponse.json()
    const tools = Array.isArray(toolsBody) ? toolsBody : toolsBody?.tools
    expect(Array.isArray(tools)).toBe(true)
    expect(tools.length).toBeGreaterThan(0)
    observedEndpoints.push(`GET /api/tools ${toolsResponse.status()}`)

    const screenshotPath = join(PROOF_DIR, 'id-01-ca-01.png')
    await page.screenshot({ path: screenshotPath, fullPage: true })
    const videoPath = await page.video()?.path().catch(() => null)

    writeProofReceipt(join(PROOF_DIR, 'id-01-ca-01-receipt.json'), {
      capability_ids: ['ID-01', 'CA-01'],
      evidence_tier: 'staging',
      route: '/try',
      runtime: 'deployed staging origin, authenticated real browser session and real API bearer token',
      source_commit: identity.source_revision,
      api_endpoints: [...new Set(observedEndpoints)],
      assertions: [
        'the signed-out gate did not render for a primed authenticated session',
        'the Catalog tab became enabled once the session was active',
        'GET /api/tools with the real bearer token returned 200 and at least one real tool',
      ],
      artifacts: [screenshotPath, ...(videoPath ? [videoPath] : [])].map((p) => p.replaceAll('\\', '/')),
      result: {
        verdict: 'pass',
        observed_source_revision: identity.source_revision,
        real_tool_count: tools.length,
      },
      limitations: [
        'This uses a pre-provisioned staging JWT, not a live Auth0 interactive login, so sign-in itself and expired-session recovery are not proven.',
        'CA-01 is proven for catalog load only, not family filter, tool detail, empty, or retry.',
        'This test has not yet run with real credentials; treat its first real receipt as unreviewed until checked against this file.',
      ],
    })
  } finally {
    await context.close()
  }
})

test('VR-01: the version drawer opens with real, non-empty history for an authenticated staging session', async ({ browser, request, baseURL }) => {
  test.skip(!STAGING_JWT, 'LEAF_E2E_STAGING_JWT is not set; VR-01 requires an active session on the deployed surface')
  const identity = await captureStagingIdentity(request)
  const context = await browser.newContext()
  await primeAuthenticatedContext(context)
  const page = await context.newPage()
  const observedEndpoints = [identity.endpoint]
  const stagingOrigin = new URL(baseURL).origin
  page.on('response', (response) => {
    const url = new URL(response.url())
    if (url.origin === stagingOrigin) observedEndpoints.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
  })
  try {
    await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
    const versionsTab = page.locator('#operations-tab-versions')
    await expect(versionsTab).toBeEnabled()
    await versionsTab.click()
    const region = page.getByRole('region', { name: 'Version history' })
    await expect(region).toBeVisible({ timeout: 15_000 })

    // Must fail on empty or failed history, not merely on the region
    // mounting. A version entry must actually be present, and no error
    // state may be showing.
    await expect(page.locator('.tc-panel-error')).toHaveCount(0)
    await expect(page.locator('.tc-panel-note', { hasText: 'Loading versions' })).toHaveCount(0)
    const versionEntries = region.locator('.tc-version-list button')
    await expect(versionEntries.first()).toBeVisible({ timeout: 15_000 })
    const versionCount = await versionEntries.count()
    expect(versionCount).toBeGreaterThan(0)

    const screenshotPath = join(PROOF_DIR, 'vr-01.png')
    await page.screenshot({ path: screenshotPath, fullPage: true })
    const videoPath = await page.video()?.path().catch(() => null)

    writeProofReceipt(join(PROOF_DIR, 'vr-01-receipt.json'), {
      capability_ids: ['VR-01'],
      evidence_tier: 'staging',
      route: '/try',
      runtime: 'deployed staging origin, authenticated real browser session',
      source_commit: identity.source_revision,
      api_endpoints: [...new Set(observedEndpoints)],
      assertions: [
        'the Versions tab opened the version history region for an authenticated tenant',
        'no history error or loading state was left showing',
        'at least one real version entry rendered in the list',
      ],
      artifacts: [screenshotPath, ...(videoPath ? [videoPath] : [])].map((p) => p.replaceAll('\\', '/')),
      result: { verdict: 'pass', observed_source_revision: identity.source_revision, version_entry_count: versionCount },
      limitations: [
        'Only listing is proven, not preview, return-to-head, or parent-relation navigation.',
        'This test has not yet run with real credentials; treat its first real receipt as unreviewed until checked against this file.',
      ],
    })
  } finally {
    await context.close()
  }
})

// AU-01 ("Author, stage, publish, use tool") requires stage, an independent
// decision, publish, catalog refresh, and use. Actually exercising that
// staged-and-published a real tool on staging, mutating shared state, which
// this read-only suite does not do (see
// scripts/deployed_authored_cad_acceptance.mjs for the dedicated,
// non-read-only acceptance driver that is built for exactly that). This
// test therefore does not claim AU-01 and writes no receipt: it only checks
// that the guided authoring entry point renders for an authenticated
// tenant, which is not itself ledger evidence.
test('authoring entry point renders for an authenticated staging session (no AU-01 claim; staging/publishing is out of scope for a read-only suite)', async ({ browser, request }) => {
  test.skip(!STAGING_JWT, 'LEAF_E2E_STAGING_JWT is not set; skipping today')
  const context = await browser.newContext()
  await primeAuthenticatedContext(context)
  const page = await context.newPage()
  try {
    await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
    const authorTab = page.locator('#workspace-tab-author')
    await expect(authorTab).toBeEnabled()
    await authorTab.click()
    await expect(page.getByLabel('What should the tool do?')).toBeVisible({ timeout: 15_000 })
  } finally {
    await context.close()
  }
})
