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
  const trigger = page.getByRole('button', { name: 'Import LandXML terrain' })
  await reachable(trigger)
  await trigger.click()
  const upload = page.getByTestId('solar-landxml-upload')
  await expect(upload.locator('input[type="file"]')).toBeFocused()
  await upload.locator('input[type="file"]').setInputFiles(LANDXML)
  await upload.getByLabel('Drawing units').selectOption('m')
  await expect(upload.getByLabel('Coordinate system')).toHaveValue('')
  return upload
}

async function takeLock(page) {
  const lock = page.getByRole('button', { name: 'Take edit lock' })
  await reachable(lock)
  await lock.click()
  await expect(page.getByText('You hold the edit lock')).toBeVisible()
}

function receipt(slug, context, result, artifacts, assertions, seeded) {
  writeProofReceipt(join(PROOF_DIR, `solar-wave20-terrain-${slug}-receipt.json`), {
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
      ...(slug === 'w2' ? ['W2 proves the client checkout gate (disabled operation buttons and no operation request); the terrain operations route requires no checkout, so W2 proves no server refusal'] : []),
    ],
  })
}

async function ownsCentre(control) {
  await expect(control).toBeVisible()
  expect(await control.evaluate((element) => {
    const r = element.getBoundingClientRect()
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
    return hit === element || element.contains(hit)
  })).toBe(true)
}

async function reachable(control) {
  await control.scrollIntoViewIfNeeded()
  await ownsCentre(control)
}

async function terrainWalk(page, viewport) {
  await page.setViewportSize(viewport)
  const context = await prepareDrawing(page, true)
  if (!(await navigateWorkspace(page, context.drawingId))) return
  await takeLock(page)
  const upload = await chooseLandxml(page)
  const submit = upload.getByRole('button', { name: 'Import terrain', exact: true })
  await expect(submit).toBeEnabled()
  await reachable(submit)
  await submit.click()
  await expect(page.getByTestId('solar-landxml-summary')).toBeVisible({ timeout: 120_000 })
  await expect(page.locator('.solar-workspace-announce')).toHaveText('Terrain imported for this drawing.')
  const workspace = page.locator('section[aria-label="Solar workspace tools"]')
  await expect(workspace).toHaveAttribute('data-physical-head-index', /^\d+$/)
  const importedHeadIndex = Number(await workspace.getAttribute('data-physical-head-index'))
  expect(await context.versionsOf()).toMatchObject({ head: 2, latest: 2 })

  const trigger = workspace.getByRole('button', { name: 'Terrain preview', exact: true })
  await reachable(trigger)
  await trigger.click()
  await expect(workspace.getByRole('heading', { name: 'Terrain preview', exact: true })).toBeFocused()
  const panel = page.getByTestId('solar-terrain-panel')
  await expect(panel.getByTestId('solar-terrain-summary')).toBeVisible()
  await expect(panel).toHaveAttribute('data-phase', 'ready')
  await expect(panel).toHaveAttribute('data-head', /.+/)
  const importedHead = await panel.getAttribute('data-head')
  const mesh = panel.getByRole('button', { name: 'Run mesh preview', exact: true })
  await expect(mesh).toBeEnabled()
  await reachable(mesh)
  await mesh.click()
  await expect(panel.getByTestId('solar-terrain-announce')).toHaveText('The mesh preview was updated', { timeout: 120_000 })
  await expect(workspace).toHaveAttribute('data-physical-head-index', String(importedHeadIndex + 1))
  await expect(panel).toHaveAttribute('data-phase', 'ready', { timeout: 120_000 })
  await expect(panel).toHaveAttribute('data-head', /.+/)
  await expect(panel).not.toHaveAttribute('data-head', importedHead)
  const meshHead = await panel.getAttribute('data-head')
  const close = workspace.getByRole('button', { name: 'Close', exact: true })
  await reachable(close)
  await close.click()
  await expect(trigger).toBeFocused()
  await expect(panel).toHaveCount(0)
  await expect(workspace).toHaveAttribute('data-physical-head-index', String(importedHeadIndex + 1))
  await reachable(trigger)
  await trigger.click()
  await expect(panel.getByTestId('solar-terrain-summary')).toBeVisible()
  await expect(panel).toHaveAttribute('data-phase', 'ready')
  await expect(panel).toHaveAttribute('data-head', meshHead)
  await expect(workspace).toHaveAttribute('data-physical-head-index', String(importedHeadIndex + 1))
  const finalVersions = await context.versionsOf()
  expect(finalVersions).toMatchObject({ head: 2, latest: 2 })
  const slug = `w1-${viewport.width}x${viewport.height}`
  mkdirSync(PROOF_DIR, { recursive: true })
  const screenshot = join(PROOF_DIR, `solar-wave20-terrain-${slug}.png`)
  await page.screenshot({ path: screenshot })
  receipt(slug, context, {
    imported_head_index: importedHeadIndex, mesh_head_index: importedHeadIndex + 1,
    closed_head_index: importedHeadIndex + 1, reopened_head_index: importedHeadIndex + 1,
    imported_terrain_head: importedHead, mesh_terrain_head: meshHead, head: finalVersions.head,
  }, [screenshot], [
    'Studio imports the tracked LandXML capture on a seeded drawing with the edit lock',
    'every control the walk clicks owns its centre at the tested viewport',
    'opening Terrain preview focuses its heading and reads the stored terrain',
    'the mesh preview announces its update, changes the artifact and advances the container head once',
    'Close returns focus to the trigger and reopening retains the mesh head',
    'terrain import, preview and reopening leave drawing head and latest at 2',
  ], true)
}

test('W20-06b W1 terrain preview reads, meshes and keeps its head at 1280x800', async ({ page }) => {
  if (!allThreeFlagsOn) {
    test.info().annotations.push({ type: 'flags', description: 'Solar workspace not exercised: VITE_SOLAR_FLOW_RAIL, VITE_CAD_EDIT and VITE_SOLAR_SETTINGS_FORM are not all enabled' })
    return
  }
  test.setTimeout(300_000)
  await terrainWalk(page, { width: 1280, height: 800 })
})

test('W20-06b W1 terrain preview reads, meshes and keeps its head at 1600x1000', async ({ page }) => {
  if (!allThreeFlagsOn) {
    test.info().annotations.push({ type: 'flags', description: 'Solar workspace not exercised: VITE_SOLAR_FLOW_RAIL, VITE_CAD_EDIT and VITE_SOLAR_SETTINGS_FORM are not all enabled' })
    return
  }
  test.setTimeout(300_000)
  await terrainWalk(page, { width: 1600, height: 1000 })
})

test('W20-06b W2 without the checkout the client disables terrain operations and sends none, reads and refreshes', async ({ page }) => {
  if (!allThreeFlagsOn) {
    test.info().annotations.push({ type: 'flags', description: 'Solar workspace not exercised: VITE_SOLAR_FLOW_RAIL, VITE_CAD_EDIT and VITE_SOLAR_SETTINGS_FORM are not all enabled' })
    return
  }
  test.setTimeout(300_000)
  const operations = []
  page.on('request', (request) => {
    if (request.method() === 'POST' && /^\/api\/drawings\/[^/]+\/terrain\/operations$/.test(new URL(request.url()).pathname)) {
      operations.push(request.url())
    }
  })
  const context = await prepareDrawing(page, true)
  // The client sends raw XML with these query values and tenant/media headers.
  // Neither the LandXML client nor its route requires a checkout capability.
  const importPath = `/api/drawings/${context.drawingId}/imports/landxml`
  const imported = await page.request.post(`${API_BASE}${importPath}?drawing_units=m&crs=none&target_cells=30`, {
    headers: { ...TENANT_HEADERS, 'Content-Type': 'application/xml' },
    data: readFileSync(LANDXML), timeout: 120_000,
  })
  context.observed.push(`POST /api/drawings/{id}/imports/landxml ${imported.status()}`)
  expect(imported.status()).toBe(200)
  const importedTerrain = await imported.json()
  expect(importedTerrain.created).toBe(true)
  const importedHeadIndex = importedTerrain.head.index
  expect(await context.versionsOf()).toMatchObject({ head: 2, latest: 2 })
  if (!(await navigateWorkspace(page, context.drawingId))) return
  await expect(page.getByRole('button', { name: 'Take edit lock' })).toBeVisible()
  await page.getByTestId('solar-flow-select').selectOption('ground-physical')
  const workspace = page.locator('section[aria-label="Solar workspace tools"]')
  const trigger = workspace.getByRole('button', { name: 'Terrain preview', exact: true })
  await reachable(trigger)
  await trigger.click()
  const panel = page.getByTestId('solar-terrain-panel')
  await expect(panel.getByTestId('solar-terrain-summary')).toBeVisible()
  await expect(panel).toHaveAttribute('data-phase', 'ready')
  await expect(panel).toHaveAttribute('data-head', /.+/)
  const terrainHead = await panel.getAttribute('data-head')
  expect(terrainHead).toBe(importedTerrain.head.state.artifact_id)
  await expect(workspace).toHaveAttribute('data-physical-head-index', String(importedHeadIndex))
  await expect(panel.getByRole('button', { name: 'Run mesh preview', exact: true })).toBeDisabled()
  await expect(panel.getByTestId('solar-terrain-reason')).toHaveText('Take the drawing checkout before changing terrain previews')
  const refresh = panel.getByRole('button', { name: 'Refresh terrain preview', exact: true })
  await expect(refresh).toBeEnabled()
  const refreshed = page.waitForResponse((response) =>
    response.request().method() === 'GET' && new URL(response.url()).pathname === `/api/drawings/${context.drawingId}/terrain`)
  await reachable(refresh)
  await refresh.click()
  expect((await refreshed).status()).toBe(200)
  await expect(panel).toHaveAttribute('data-phase', 'ready')
  await expect(refresh).toBeEnabled()
  await expect(panel).toHaveAttribute('data-head', terrainHead)
  await expect(workspace).toHaveAttribute('data-physical-head-index', String(importedHeadIndex))
  await expect(panel.getByRole('button', { name: 'Run mesh preview', exact: true })).toBeDisabled()
  const finalVersions = await context.versionsOf()
  expect(finalVersions).toMatchObject({ head: 2, latest: 2 })
  expect(operations).toEqual([])
  receipt('w2', context, {
    imported_head_index: importedHeadIndex, read_head_index: importedHeadIndex,
    refreshed_head_index: importedHeadIndex, terrain_head: terrainHead,
    terrain_operation_requests: operations.length, head: finalVersions.head,
  }, [], [
    'the seeded drawing has stored terrain imported with the exact LandXML client request',
    'without the checkout Terrain preview reads its stored summary and artifact',
    'the checkout reason disables mesh preview while Refresh remains enabled',
    'Refresh completes a terrain read and retains the artifact and container head',
    'no terrain operation POST is sent and drawing head and latest remain 2',
  ], true)
})
