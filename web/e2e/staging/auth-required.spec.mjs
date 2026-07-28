import { expect, test } from '@playwright/test'
import { captureStagingIdentity } from './stagingIdentity.mjs'
import {
  allowedStagingOrigins,
  assertPageOnAllowedOrigin,
  assertResponseOnAllowedOrigin,
  collectBearerLeaks,
  stagingProofPath,
} from './stagingConfig.mjs'
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

async function primeAuthenticatedPage(page) {
  // Full ORIGIN, not hostname: a page served from an allowed hostname on a
  // nonstandard port is a different origin and must never receive the JWT.
  await page.addInitScript(({ token, allowedOrigins }) => {
    if (allowedOrigins.includes(location.origin.toLowerCase())) {
      window.localStorage.setItem('leaf.jwt', token)
    }
  }, { token: STAGING_JWT, allowedOrigins: [...allowedStagingOrigins()] })
}

test('ID-01 + CA-01: an authenticated staging session reaches the operator surface and a real tool catalog', async ({ page, request, baseURL }) => {
  test.skip(!STAGING_JWT, 'LEAF_E2E_STAGING_JWT is not set; skipping the authenticated ID-01/CA-01 sub-cases today')
  const identity = await captureStagingIdentity(request)
  await primeAuthenticatedPage(page)
  const bearerLeaks = collectBearerLeaks(page)
  const observedEndpoints = [identity.endpoint]
  const stagingOrigin = new URL(baseURL).origin
  page.on('response', (response) => {
    const url = new URL(response.url())
    if (url.origin === stagingOrigin) observedEndpoints.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
  })
  // Registered BEFORE navigation: the browser's OWN authenticated catalog
  // fetch is the signal that the controller's live reload completed, and the
  // stale-mock-card hole (createCatalogController.js ~257) closes only once
  // that response has landed -- a shared tool name alone can match a stale
  // mock card while the live load is still in flight.
  const browserCatalogLoaded = page.waitForResponse(
    (response) => new URL(response.url()).pathname === '/api/tools' && response.ok(),
    { timeout: 30_000 },
  )
  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toHaveCount(0)
  const catalogTab = page.locator('#workspace-tab-catalog')
  await expect(catalogTab).toBeEnabled()

  // CA-01 at API level: the app contract is exactly { tools: [...] }, never
  // a bare array. This is a non-mutating GET against the deployed endpoint,
  // and the RESOLVED response URL must still be the allowed origin (a
  // redirect off-host must fail the test, not become evidence).
  const toolsResponse = await request.get('/api/tools', {
    headers: { Authorization: `Bearer ${STAGING_JWT}` },
  })
  assertResponseOnAllowedOrigin(toolsResponse)
  expect(toolsResponse.status()).toBe(200)
  const toolsBody = await toolsResponse.json()
  expect(Array.isArray(toolsBody?.tools)).toBe(true)
  const tools = toolsBody.tools
  expect(tools.length).toBeGreaterThan(0)
  const realName = tools.find((tool) => typeof tool?.name === 'string' && tool.name)?.name
  expect(realName).toBeTruthy()
  observedEndpoints.push(`GET /api/tools ${toolsResponse.status()}`)
  await catalogTab.click()
  const browserCatalogResponse = await browserCatalogLoaded
  assertResponseOnAllowedOrigin(browserCatalogResponse)
  await expect(page.locator('.tool-card').filter({ hasText: realName })).toBeVisible({ timeout: 15_000 })
  // No browser request carried the bearer off the allowed origin.
  expect(bearerLeaks).toEqual([])

  const screenshotPath = stagingProofPath('auth-required', 'id-01-ca-01.png')
  await page.screenshot({ path: screenshotPath, fullPage: true })

  const common = {
    evidence_tier: 'staging',
    route: '/try',
    runtime: 'deployed staging origin, authenticated real browser session and real API bearer token',
    source_commit: identity.source_revision,
    api_endpoints: [...new Set(observedEndpoints)],
    artifacts: [screenshotPath],
    result: { verdict: 'pass', observed_source_revision: identity.source_revision, real_tool_count: tools.length },
  }
  writeProofReceipt(stagingProofPath('auth-required', 'id-01-receipt.json'), {
    ...common,
    capability_ids: ['ID-01'],
    sub_cases: { proven: ['authenticated'], not_proven: ['signed-out', 'expired-session recovery'] },
    assertions: [
      'the signed-out gate did not render for a primed authenticated session',
      'the Catalog tab became enabled once the session was active',
    ],
    limitations: ['This uses a pre-provisioned staging JWT, not a live Auth0 interactive login.'],
  })
  writeProofReceipt(stagingProofPath('auth-required', 'ca-01-receipt.json'), {
    ...common,
    capability_ids: ['CA-01'],
    sub_cases: { proven: ['load'], not_proven: ['family filter', 'tool detail', 'empty', 'retry'] },
    assertions: [
      'GET /api/tools with the real bearer token returned exactly { tools: [...] } with at least one tool',
      'the enabled Catalog tab rendered at least one real tool card',
    ],
    limitations: ['This test has not yet run with real credentials; treat its first real receipt as unreviewed until checked against this file.'],
  })
})

test('VR-01: the version drawer opens with real, non-empty history for an authenticated staging session', async ({ page, request, baseURL }) => {
  test.skip(!STAGING_JWT, 'LEAF_E2E_STAGING_JWT is not set; VR-01 requires an active session on the deployed surface')
  const identity = await captureStagingIdentity(request)
  await primeAuthenticatedPage(page)
  const bearerLeaks = collectBearerLeaks(page)
  const observedEndpoints = [identity.endpoint]
  const stagingOrigin = new URL(baseURL).origin
  page.on('response', (response) => {
    const url = new URL(response.url())
    if (url.origin === stagingOrigin) observedEndpoints.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
  })
  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
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
  expect(bearerLeaks).toEqual([])

  const screenshotPath = stagingProofPath('auth-required', 'vr-01.png')
  await page.screenshot({ path: screenshotPath, fullPage: true })

  writeProofReceipt(stagingProofPath('auth-required', 'vr-01-receipt.json'), {
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
      artifacts: [screenshotPath],
      sub_cases: { proven: ['list'], not_proven: ['preview', 'return to head', 'parent relation'] },
      result: { verdict: 'pass', observed_source_revision: identity.source_revision, version_entry_count: versionCount },
      limitations: [
        'Only listing is proven, not preview, return-to-head, or parent-relation navigation.',
        'This test has not yet run with real credentials; treat its first real receipt as unreviewed until checked against this file.',
      ],
  })
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
test('authoring entry point renders for an authenticated staging session (no AU-01 claim; staging/publishing is out of scope for a read-only suite)', async ({ page, request }) => {
  test.skip(!STAGING_JWT, 'LEAF_E2E_STAGING_JWT is not set; skipping today')
  await primeAuthenticatedPage(page)
  const bearerLeaks = collectBearerLeaks(page)
  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  const authorTab = page.locator('#workspace-tab-author')
  await expect(authorTab).toBeEnabled()
  await authorTab.click()
  await expect(page.getByLabel('What should the tool do?')).toBeVisible({ timeout: 15_000 })
  expect(bearerLeaks).toEqual([])
})
