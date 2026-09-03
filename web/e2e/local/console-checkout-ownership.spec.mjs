import { expect, test } from '@playwright/test'
import { join, relative } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'
import { setRail } from './railFlag.mjs'

// The /app TWIN of checkout-ownership.spec.mjs (W2c).
//
// That spec proves the STAGE's checkout ownership against the shared
// controller. The console ran a hand-rolled twin of the whole single-writer
// block until this wave and had no equivalent walk at all, so the two surfaces
// are now held to one behaviour by two suites rather than one surface by one.
//
// Every assertion is a PRESERVATION proof: it must pass before and after the
// adoption, against the same real stack, the same real manifest lock, and the
// same real 403/409 authority the store enforces.
const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const DRAWING_ID = 'cat-panels'
const OTHER_HOLDER = 'other-console-editor'
const WRITE_TOOL = 'delete-marked-panel'
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

// The console's catalog renders one collapsed Section per family. Open them all
// rather than naming a family label, so the walk does not break when the server
// regroups its capabilities.
async function openEveryCatalogFamily(page) {
  // W4c-V1 / W4d Slice D: on drafting surfaces under the studio the tool
  // rail hides behind the band - the catalog sections exist only once it
  // expands. The walk expands it first through the band's own affordance
  // (a no-op rail-OFF, where the band does not render). Never `.spine-expand`
  // alone: the job monitor's spine carries that class too.
  const expand = page.locator('[data-tool="rail-expand"], aside.nav .spine-expand').first()
  if (await expand.count()) await expand.click()
  await page.locator('aside.nav[data-spine]').waitFor({ state: 'detached', timeout: 10_000 }).catch(() => {})
  const heads = page.locator('.section-head')
  const count = await heads.count()
  for (let i = 0; i < count; i += 1) {
    const head = heads.nth(i)
    if ((await head.getAttribute('aria-expanded')) === 'false') await head.click()
  }
}

// Both rail values (W4c-0 debt): the OFF walk is the W2c preservation proof;
// the ON walk proves the STUDIO console holds the identical single-writer
// behaviour - same lock, same fail-closed gates, one controller - under the
// portaled ground. ACCEPTANCE: "run console-checkout-ownership.spec.mjs with
// setRail('1')", due since the pre-existing-failures chip merged (#896).
for (const rail of ['0', '1']) {
test(`the console holds the same single-writer lock the stage does [rail ${rail}]`, async ({ page, request }, testInfo) => {
  test.setTimeout(120_000)
  await requireLocalReady(request, test, API_BASE)
  await setRail(page, rail)

  // Start from a clean lock: a previous spec in this run may have left one.
  await request.delete(`${API_BASE}/api/drawings/${DRAWING_ID}/checkout`, {
    headers: { ...TENANT_HEADERS, 'X-Checkout-Force': '1' },
  }).catch(() => {})

  const seed = await request.post(`${API_BASE}/api/drawings/${DRAWING_ID}/checkout`, {
    headers: TENANT_HEADERS,
    data: { holder: OTHER_HOLDER, ttl_s: 30 },
  })
  expect(seed.status()).toBe(200)
  const seedBody = await seed.json()
  expect(seedBody).toMatchObject({ acquired: true, checkout: { holder: OTHER_HOLDER } })

  // A forged bearer proof cannot end someone else's lease. The console never
  // sees a holder query; the capability IS the authority.
  const deniedRelease = await request.delete(
    `${API_BASE}/api/drawings/${DRAWING_ID}/checkout`,
    { headers: { ...TENANT_HEADERS, 'X-Checkout-Capability': `lco1.${'b'.repeat(64)}` } },
  )
  expect(deniedRelease.status()).toBe(403)

  const observed = []
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    observed.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
  })

  // `?drawing=` boots the console on every path (the frozen route matrix) and
  // seeds DrawingIdentityProvider with the drawing the stage twin uses.
  await page.goto(`/app?drawing=${DRAWING_ID}`)
  // The walk must run under the arm the rail names, or the ON row proves
  // nothing: assert the studio shell's presence exactly matches the rail.
  await expect(page.locator('.studio-shell[data-mode="console"]')).toHaveCount(rail === '1' ? 1 : 0)
  await expect(page.getByText(`Editing locked by`)).toBeVisible({ timeout: 20_000 })

  // Runtime single-instance proof for the console (panel W2c): exactly one
  // single-writer controller stamps the page. Two instances would be two
  // bearer capabilities and two holder ids over ONE server-side lock.
  expect(await page.locator('[data-checkout-instance]').count()).toBe(1)
  const checkoutInstance = await page.locator('[data-checkout-instance]').getAttribute('data-checkout-instance')
  expect(checkoutInstance).toBeTruthy()
  await expect(page.getByText(OTHER_HOLDER)).toBeVisible()

  // The write tool is genuinely DISABLED, and no run leaves the browser.
  await openEveryCatalogFamily(page)
  const writeTool = page.locator('.tool-card').filter({ hasText: WRITE_TOOL }).first()
  await writeTool.locator('button.tool-head').click()
  await expect(writeTool.getByRole('button', { name: 'Review & run' })).toBeDisabled()
  expect(observed.filter((entry) => entry.startsWith('POST /api/run '))).toHaveLength(0)

  // Let the lease elapse and reload. Expiry never enables a write client-side:
  // it only offers a Take that the SERVER adjudicates.
  const shortenLease = await request.post(`${API_BASE}/api/drawings/${DRAWING_ID}/checkout`, {
    headers: { ...TENANT_HEADERS, 'X-Checkout-Capability': seedBody.checkout_capability },
    data: { holder: OTHER_HOLDER, ttl_s: 0.5 },
  })
  expect(shortenLease.status()).toBe(200)

  await page.waitForTimeout(600)
  await page.reload()
  const take = page.getByRole('button', { name: 'Take edit lock' })
  await expect(take).toBeVisible({ timeout: 20_000 })
  // Still exactly one controller after the reload (a fresh instance id is
  // legitimate; a second stamp is not). Checked once the reloaded page has
  // reached the same stable, rendered state the pre-reload check waited for
  // (line 70) — `.count()` has no retry of its own, so it must never run
  // against a still-hydrating DOM right after `reload()` returns.
  expect(await page.locator('[data-checkout-instance]').count()).toBe(1)

  const takeResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST' && url.pathname === `/api/drawings/${DRAWING_ID}/checkout`
  })
  await take.click()
  expect((await takeResponse).status()).toBe(200)
  // "You hold the edit lock" is only rendered once the capability is BACKED by
  // the origin-wide Web Lock: a matching holder label alone is not proof.
  await expect(page.getByText('You hold the edit lock')).toBeVisible()

  // And the lock we now hold un-gates the write tool on the same surface.
  await openEveryCatalogFamily(page)
  const unlockedTool = page.locator('.tool-card').filter({ hasText: WRITE_TOOL }).first()
  await unlockedTool.locator('button.tool-head').click()
  await expect(unlockedTool.getByRole('button', { name: 'Review & run' })).toBeEnabled()

  const releaseResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'DELETE' && url.pathname === `/api/drawings/${DRAWING_ID}/checkout`
  })
  await page.getByRole('button', { name: 'Release' }).click()
  const released = await releaseResponse
  expect(released.status()).toBe(200)
  // No holder query rides the release: the capability travels as a header.
  expect(new URL(released.url()).search).toBe('')
  await expect(page.getByRole('button', { name: 'Take edit lock' })).toBeVisible()

  const versions = await request.get(`${API_BASE}/api/drawings/${DRAWING_ID}/versions`, {
    headers: TENANT_HEADERS,
  })
  expect(versions.status()).toBe(200)
  await expect(versions.json()).resolves.toMatchObject({ drawing_id: DRAWING_ID, checkout: null })
  expect(observed.filter((entry) => entry.startsWith('POST /api/run '))).toHaveLength(0)

  writeProofReceipt(join(PROOF_DIR, `console-checkout-ownership-receipt${rail === '1' ? '-studio' : ''}.json`), {
    capability_ids: ['VR-02'],
    evidence_tier: 'local-e2e',
    route: '/app',
    runtime: `real local Vite, FastAPI, drawing manifest, and the SHARED checkout controller (one-shell rail ${rail === '1' ? 'ON: studio console' : 'OFF: old shell'})`,
    api_endpoints: observed,
    artifacts: [
      relative(join(process.cwd(), '..'), testInfo.outputPath('video.webm')).replaceAll('\\', '/'),
    ],
    assertions: [
      'a real active checkout held by another editor rendered on the console',
      'the conflicting checkout disabled the drawing.write review action and sent no run request',
      'a forged opaque capability could not release the active checkout and no public holder query was used',
      'the expired checkout became available after an authoritative refresh, never on the browser clock',
      'the operator took and released the real checkout through the console',
      'the taken lock un-gated the same write tool on the same surface',
      'the authoritative versions response ended with no checkout',
    ],
    result: {
      verdict: 'pass',
      drawing_id: DRAWING_ID,
      conflicting_holder: OTHER_HOLDER,
      non_holder_release_status: deniedRelease.status(),
      product_run_count: 0,
      checkout_after_release: null,
    },
    limitations: [
      'LEAF_DRAWING_STORE=legacy uses the local manifest lock, not PostgreSQL compare-and-swap authority.',
      'LEAF_AUTH_LIVE=0 uses the local tenant header and generated holder identity.',
      'APS_LIVE=0 substitutes the local drawing engine for Autodesk APS.',
    ],
  })
})
}
