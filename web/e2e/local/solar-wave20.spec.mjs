import { expect, test } from '@playwright/test'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { SEED_RESULT_SCHEMA, runBody, seedParams } from '../solarGraphCommitProof.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const DRAWING = join(process.cwd(), 'e2e', 'fixtures', 'distinctive-panel.dxf')
const LANDXML = join(process.cwd(), '..', 'docs', 'parity', 'evidence', 'probes',
  'demo-probes-20260923', 'leaflandxml_fixed.xml')
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')
const REQUEST_TIMEOUT = 15_000
const allThreeFlagsOn = ['VITE_SOLAR_FLOW_RAIL', 'VITE_CAD_EDIT', 'VITE_SOLAR_SETTINGS_FORM']
  .every((flag) => process.env[flag] === '1')

async function prepareDrawing(page, seeded) {
  const api = page.request
  const observed = []
  const note = (method, path, response) => {
    observed.push(`${method} ${path.replace(/\/api\/drawings\/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?=\/|$)/, '/api/drawings/{id}')} ${response.status()}`)
    return response
  }
  // Include the readiness helper's calls as well as setup and served UI calls.
  await requireLocalReady({
    get: async (url, options) => note('GET', new URL(url).pathname, await api.get(url, options)),
  }, test, API_BASE)
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname
    if (path.startsWith('/api/')) note(response.request().method(), path, response)
  })
  const getJson = async (path) => {
    const response = note('GET', path, await api.get(`${API_BASE}${path}`, {
      headers: TENANT_HEADERS, timeout: REQUEST_TIMEOUT,
    }))
    expect(response.status(), `GET ${path}`).toBe(200)
    return response.json()
  }
  const health = await getJson('/api/health')
  expect(health.source_sha).toMatch(/^[0-9a-f]{40}$/)
  const upload = note('POST', '/api/drawings/upload', await api.post(`${API_BASE}/api/drawings/upload`, {
    headers: TENANT_HEADERS,
    multipart: {
      file: { name: 'distinctive-panel.dxf', mimeType: 'application/dxf', buffer: readFileSync(DRAWING) },
    },
    timeout: REQUEST_TIMEOUT,
  }))
  expect(upload.status()).toBe(202)
  const uploadedReceipt = await upload.json()
  expect(uploadedReceipt).toMatchObject({ tenant_id: 'demo-tenant', tenant_kind: 'account' })
  const drawingId = uploadedReceipt.drawing_id
  expect(drawingId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  await expect.poll(async () => (await getJson(`/api/drawings/${drawingId}/upload-status`)).status, {
    timeout: 30_000, message: 'upload extraction settles',
  }).toMatch(/^(ready|failed)$/)
  expect((await getJson(`/api/drawings/${drawingId}/upload-status`)).status).toBe('ready')
  const versionsOf = () => getJson(`/api/drawings/${drawingId}/versions`)
  const uploaded = await versionsOf()
  expect(uploaded).toMatchObject({ drawing_id: drawingId, head: 1, latest: 1 })
  expect(uploaded.versions).toHaveLength(1)
  const intake = (await getJson(`/api/drawings/${drawingId}/intake`)).intake
  expect(intake.solar_design_graph ?? null).toBeNull()

  if (seeded) {
    const tools = (await getJson('/api/tools')).tools
    const settings = tools.find((tool) => tool.name === 'solar-settings')
    expect(settings, 'solar-settings is in the served catalog').toBeTruthy()
    const checkoutPath = `/api/drawings/${drawingId}/checkout`
    const checkout = note('POST', checkoutPath, await api.post(`${API_BASE}${checkoutPath}`, {
      headers: TENANT_HEADERS, data: { holder: 'drafter' }, timeout: REQUEST_TIMEOUT,
    }))
    expect(checkout.status()).toBe(200)
    const lease = await checkout.json()
    expect(lease).toMatchObject({ acquired: true, holder: 'drafter' })
    const capability = lease.checkout_capability
    expect(typeof capability === 'string' && capability.length > 0).toBe(true)
    const seed = note('POST', '/api/run', await api.post(`${API_BASE}/api/run?wait=1`, {
      headers: { ...TENANT_HEADERS, 'Content-Type': 'application/json', 'X-Checkout-Capability': capability },
      data: runBody({ tool: 'solar-settings', drawingId, params: seedParams(uploaded.versions[0]),
        catalogDigest: settings.catalog_digest }),
      timeout: 45_000,
    }))
    expect(seed.status()).toBe(200)
    expect(await seed.json()).toMatchObject({ ok: true, result: {
      schema_version: SEED_RESULT_SCHEMA, new_version: { drawing_id: drawingId, version: 2 },
    } })
    const release = note('DELETE', checkoutPath, await api.delete(`${API_BASE}${checkoutPath}`, {
      headers: { ...TENANT_HEADERS, 'X-Checkout-Capability': capability }, timeout: REQUEST_TIMEOUT,
    }))
    expect(release.status()).toBe(200)
    expect(await versionsOf()).toMatchObject({ head: 2, latest: 2 })
  }
  return { drawingId, health, observed, versionsOf }
}

async function navigateWorkspace(page, drawingId) {
  await page.goto(`/app?surface=solar&drawing=${drawingId}`)
  // With flags off, still wait up to 60 s for the live checkout control: a pass proves
  // the app booted on the live stack and reached that control, and nothing about the Solar workspace.
  await expect(page.getByRole('button', { name: 'Take edit lock' })).toBeVisible({ timeout: 60_000 })
  const rail = page.getByTestId('solar-flow-rail')
  if (allThreeFlagsOn) await expect(rail).toBeVisible({ timeout: 60_000 })
  if (!(await rail.count())) {
    expect(allThreeFlagsOn, 'all three Solar workspace flags are on, so the served Solar flow rail must be present').toBe(false)
    test.info().annotations.push({ type: 'flags', description:
      'Solar workspace not exercised (the live checkout control was reached): VITE_SOLAR_FLOW_RAIL, VITE_CAD_EDIT and VITE_SOLAR_SETTINGS_FORM are not all enabled' })
    return false
  }
  await expect(rail).toBeVisible({ timeout: 60_000 })
  return true
}

async function chooseLandxml(page) {
  await page.getByTestId('solar-flow-select').selectOption('ground-physical')
  await page.getByRole('button', { name: 'Import LandXML terrain' }).click()
  const upload = page.getByTestId('solar-landxml-upload')
  await expect(upload.locator('input[type="file"]')).toBeFocused()
  await upload.locator('input[type="file"]').setInputFiles(LANDXML)
  await upload.getByLabel('Drawing units').selectOption('m')
  await expect(upload.getByLabel('Coordinate system')).toHaveValue('')
  return upload
}

async function takeLock(page) {
  await page.getByRole('button', { name: 'Take edit lock' }).click()
  await expect(page.getByText('You hold the edit lock')).toBeVisible()
}

function receipt(slug, context, result, artifacts, assertions, seeded) {
  writeProofReceipt(join(PROOF_DIR, `solar-wave20-${slug}-receipt.json`), {
    capability_ids: ['ID-04'], evidence_tier: 'local-e2e',
    route: `/app?surface=solar&drawing=${context.drawingId}`,
    runtime: 'real local Vite, FastAPI, upload extraction, checkout, broker, worker, terrain and version stores',
    source_commit: context.health.source_sha, api_endpoints: context.observed,
    artifacts, assertions,
    result: { verdict: 'pass', drawing_id: context.drawingId, source_sha: context.health.source_sha, ...result },
    limitations: [
      'APS_LIVE=0: this local proof does not reach Autodesk APS.',
      'LEAF_AUTH_LIVE=0: this proves the account-scoped demo tenant.',
      ...(seeded ? ['The seed run is sent through the API from the page request context, not through the Solar settings form.'] : []),
    ],
  })
}

async function seededWalk(page, viewport) {
  await page.setViewportSize(viewport)
  const context = await prepareDrawing(page, true)
  if (!(await navigateWorkspace(page, context.drawingId))) return
  const upload = await chooseLandxml(page)
  const submit = upload.getByRole('button', { name: 'Import terrain', exact: true })
  await expect(upload.getByTestId('solar-landxml-reason')).toHaveText('Take the drawing checkout before importing terrain')
  await expect(submit).toBeDisabled()
  await takeLock(page)
  await expect(submit).toBeEnabled()
  await submit.click()
  await expect(page.getByTestId('solar-landxml-summary')).toBeVisible({ timeout: 120_000 })
  const announcement = page.locator('.solar-workspace-announce')
  await expect(announcement).toHaveText('Terrain imported for this drawing.')
  const workspace = page.locator('section[aria-label="Solar workspace tools"]')
  await expect(workspace).toHaveAttribute('data-physical-head-index', /^\d+$/)
  const headIndex = await workspace.getAttribute('data-physical-head-index')
  expect(await context.versionsOf()).toMatchObject({ head: 2, latest: 2 })
  await submit.click()
  await expect(announcement).toHaveText('This terrain was already imported, so nothing changed.', { timeout: 120_000 })
  await expect(page.getByTestId('solar-landxml-summary')).toBeVisible()
  await expect(workspace).toHaveAttribute('data-physical-head-index', headIndex)
  const finalVersions = await context.versionsOf()
  expect(finalVersions).toMatchObject({ head: 2, latest: 2 })
  const slug = `w9-${viewport.width}x${viewport.height}`
  mkdirSync(PROOF_DIR, { recursive: true })
  const screenshot = join(PROOF_DIR, `solar-wave20-${slug}.png`)
  await page.screenshot({ path: screenshot })
  receipt(slug, context, { head_index: Number(headIndex), head: finalVersions.head }, [screenshot], [
    'the seeded drawing is version 2 and Studio focuses the LandXML file input',
    'the checkout reason disables import until Studio holds the edit lock',
    'the tracked LandXML capture imports with meters and no coordinate system',
    'the duplicate import announces nothing changed and retains the physical head index',
    'both imports leave drawing head and latest at 2',
  ], true)
}

test('W20-07a W9 seeded drawing imports LandXML terrain at 1280x800', async ({ page }) => {
  test.setTimeout(240_000)
  await seededWalk(page, { width: 1280, height: 800 })
})

test('W20-07a W9 seeded drawing imports LandXML terrain at 1600x1000', async ({ page }) => {
  test.setTimeout(240_000)
  await seededWalk(page, { width: 1600, height: 1000 })
})

test('W20-07a W10 unseeded drawing is refused and Studio shows no terrain', async ({ page }) => {
  test.setTimeout(240_000)
  const context = await prepareDrawing(page, false)
  if (!(await navigateWorkspace(page, context.drawingId))) return
  await takeLock(page)
  const upload = await chooseLandxml(page)
  let refusal = null
  // Fulfilment adds CORS headers, so check the fetched response carries the header the browser needs before fulfilling.
  await page.route((url) => url.pathname === `/api/drawings/${context.drawingId}/imports/landxml`, async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    const sent = await route.request().allHeaders()
    const pageOrigin = new URL(page.url()).origin
    if (sent.origin !== pageOrigin) {
      refusal = { status: null, originSent: sent.origin ?? null }
      return route.abort()
    }
    const response = await route.fetch()
    const allowOrigin = response.headers()['access-control-allow-origin'] ?? null
    refusal = { status: response.status(), body: await response.json(), allowOrigin, originSent: sent.origin }
    await route.fulfill({ response })
  })
  await upload.getByRole('button', { name: 'Import terrain', exact: true }).click()
  await expect.poll(() => refusal, { timeout: 120_000, message: 'the LandXML import answered' }).not.toBeNull()
  expect(refusal.originSent).toBe(new URL(page.url()).origin)
  expect([new URL(page.url()).origin, '*'],
    'the API answered the cross-origin LandXML POST with an Access-Control-Allow-Origin the browser accepts').toContain(refusal.allowOrigin)
  expect(refusal.status).toBe(409)
  expect(refusal.body.error.reason_code).toBe('LANDXML_GRAPH_REQUIRED')
  await expect(page.getByTestId('solar-landxml-refusal')).toContainText(
    'Start the solar design with Solar settings before importing terrain', { timeout: 120_000 })
  const workspace = page.locator('section[aria-label="Solar workspace tools"]')
  await expect(workspace).toBeVisible()
  expect(await workspace.getAttribute('data-physical-head-index')).toBeNull()
  await expect(page.getByTestId('solar-landxml-summary')).toHaveCount(0)
  const finalVersions = await context.versionsOf()
  expect(finalVersions).toMatchObject({ head: 1, latest: 1 })
  receipt('w10', context, { refusal_code: refusal.body.error.reason_code, refusal_status: refusal.status, refusal_allow_origin: refusal.allowOrigin, head: finalVersions.head }, [], [
    'the upload starts at version 1 with no solar design graph',
    'Studio refuses the import with the client sentence for LANDXML_GRAPH_REQUIRED',
    'no physical head index or terrain summary appears and drawing head and latest remain 1',
  ], false)
})
