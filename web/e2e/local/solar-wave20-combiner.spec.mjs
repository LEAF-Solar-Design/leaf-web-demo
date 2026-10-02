import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { SEED_RESULT_SCHEMA, runBody, seedParams } from '../solarGraphCommitProof.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const DRAWING = join(process.cwd(), 'e2e', 'fixtures', 'distinctive-panel.dxf')
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')
const REQUEST_TIMEOUT = 15_000
const IMPORT_FIRST = 'Import the combiner intake for this drawing first'
const INTAKE = {
  schema: 'leaf.combiner-placement-dump.v1', format: 'combiner-intake-v1', stage: 'input-before-placement',
  drawing: { metersPerUnit: 0.3048 },
  commandContext: { useL2Collectors: true, l2NumMppt: 1, l2StringsPerMppt: 1, l1CollectorsPerL2: 1,
    combinerBoxConnections: 1 },
  inputs: { l2Inverters: [], preBuiltStrings: [] },
}
const ONE_GROUP = { combiner_intake: INTAKE, panel_groups: [{ handle: 'a', outlines: [] }] }
const NO_GROUPS = { combiner_intake: INTAKE, panel_groups: [] }

function versionState({ drawing_id, head, latest, versions }) {
  return { drawing_id, head, latest, versions }
}

function flagsOn(testInfo) {
  if (process.env.VITE_CAD_EDIT === '1' && process.env.VITE_SOLAR_FLOW_RAIL === '1' && process.env.VITE_SOLAR_SETTINGS_FORM === '1') return true
  testInfo.annotations.push({ type: 'flag-gate', description: 'Requires VITE_CAD_EDIT=1, VITE_SOLAR_FLOW_RAIL=1 and VITE_SOLAR_SETTINGS_FORM=1.' })
  return false
}

async function prepareDrawing(page, seeded, l2 = false) {
  const api = page.request
  const observed = []
  const note = (method, path, response) => {
    observed.push(`${method} ${path.replace(/\/api\/drawings\/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?=\/|$)/, '/api/drawings/{id}')} ${response.status()}`)
    return response
  }
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
    multipart: { file: { name: 'distinctive-panel.dxf', mimeType: 'application/dxf', buffer: readFileSync(DRAWING) } },
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
  expect((await getJson(`/api/drawings/${drawingId}/intake`)).intake.solar_design_graph ?? null).toBeNull()
  if (seeded) {
    const settings = (await getJson('/api/tools')).tools.find((tool) => tool.name === 'solar-settings')
    expect(settings).toBeTruthy()
    const checkoutPath = `/api/drawings/${drawingId}/checkout`
    const checkout = note('POST', checkoutPath, await api.post(`${API_BASE}${checkoutPath}`, {
      headers: TENANT_HEADERS, data: { holder: 'drafter' }, timeout: REQUEST_TIMEOUT,
    }))
    expect(checkout.status()).toBe(200)
    const lease = await checkout.json()
    expect(lease).toMatchObject({ acquired: true, holder: 'drafter' })
    const capability = lease.checkout_capability
    expect(typeof capability === 'string' && capability.length > 0).toBe(true)
    const headers = { ...TENANT_HEADERS, 'Content-Type': 'application/json', 'X-Checkout-Capability': capability }
    try {
      const seed = note('POST', '/api/run', await api.post(`${API_BASE}/api/run?wait=1`, {
        headers, data: runBody({ tool: 'solar-settings', drawingId, params: seedParams(uploaded.versions[0]),
          catalogDigest: settings.catalog_digest }), timeout: 45_000,
      }))
      expect(seed.status()).toBe(200)
      expect(await seed.json()).toMatchObject({ ok: true, result: {
        schema_version: SEED_RESULT_SCHEMA, new_version: { drawing_id: drawingId, version: 2 },
      } })
      if (l2) {
        const change = note('POST', '/api/run', await api.post(`${API_BASE}/api/run?wait=1`, {
          headers, data: runBody({ tool: 'solar-settings', drawingId,
            params: { expected_rev: 1, changes: { use_l2_collectors: true } },
            catalogDigest: settings.catalog_digest }), timeout: 45_000,
        }))
        expect(change.status()).toBe(200)
        expect(await change.json()).toMatchObject({ ok: true, result: { new_version: { version: 3 } } })
      }
    } finally {
      const release = note('DELETE', checkoutPath, await api.delete(`${API_BASE}${checkoutPath}`, {
        headers: { ...TENANT_HEADERS, 'X-Checkout-Capability': capability }, timeout: REQUEST_TIMEOUT,
      }))
      expect(release.status()).toBe(200)
    }
  }
  return { drawingId, health, observed, versionsOf, getJson }
}

async function openSolarDrawing(page, context, version) {
  await page.addInitScript((id) => {
    sessionStorage.setItem('leaf.cat.workbench.id.v1', id)
  }, context.drawingId)
  await page.goto(`/app?surface=solar&drawing=${context.drawingId}`)
  await expect(page.locator('.workspace-card')).toHaveAttribute('data-engine-document', `${context.drawingId}-v${version}.dxf`, { timeout: 60_000 })
  await expect(page.locator('.studio-ground .viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })
  await expect(page.getByRole('tab', { name: 'Solar CAD', exact: true })).toHaveAttribute('aria-selected', 'true')
  const seat = page.getByTestId('solar-flow-seat')
  await expect(seat).toHaveCount(1)
  await expect(seat).toBeVisible()
  await expect(page.getByTestId('solar-flow-select')).toHaveValue('rooftop')
  await noHorizontalOverflow(seat)
  return seat
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

async function noHorizontalOverflow(seat) {
  expect(await seat.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
}

async function takeLock(page) {
  const lock = page.getByRole('button', { name: 'Take edit lock', exact: true })
  await reachable(lock)
  await lock.click()
  await expect(page.getByText('You hold the edit lock')).toBeVisible()
}

async function openIntake(page, seat) {
  const trigger = seat.getByRole('button', { name: 'Import combiner intake', exact: true })
  await reachable(trigger)
  await trigger.click()
  await expect(trigger).toHaveAttribute('aria-expanded', 'true')
  const panel = seat.getByRole('region', { name: 'Combiner intake', exact: true })
  await expect(panel).toBeVisible()
  await noHorizontalOverflow(seat)
  return panel
}

async function fillHardware(panel) {
  const hardware = panel.getByRole('group', { name: 'Combiner hardware', exact: true })
  for (const [name, value] of [['Model', 'Combiner'], ['DC voltage', '480'], ['AC power', '10']]) {
    const field = hardware.getByRole('textbox', { name, exact: true })
    await reachable(field)
    await field.fill(value)
  }
}

async function chooseBody(panel, name, body) {
  const file = panel.getByLabel('Combiner intake file', { exact: true })
  await reachable(file)
  await file.setInputFiles({ name, mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(body)) })
}

function importResponse(page) {
  return page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/imports/combiner-intake'), { timeout: 60_000 })
}

async function submitImport(panel) {
  const submit = panel.getByRole('button', { name: 'Import', exact: true })
  await expect(submit).toBeEnabled()
  await reachable(submit)
  await submit.click()
}

function receipt(slug, context, result, artifacts, assertions) {
  writeProofReceipt(join(PROOF_DIR, `${slug}-receipt.json`), {
    capability_ids: ['ID-04'], evidence_tier: 'local-e2e',
    route: `/app?surface=solar&drawing=${context.drawingId}`,
    runtime: 'real local Vite, FastAPI, upload extraction, checkout, broker, worker and version stores',
    source_commit: context.health.source_sha, api_endpoints: context.observed, artifacts, assertions,
    result: { verdict: 'pass', drawing_id: context.drawingId, source_sha: context.health.source_sha, ...result },
    limitations: [
      'APS_LIVE=0: this local proof does not reach Autodesk APS.',
      'LEAF_AUTH_LIVE=0: this proves the account-scoped demo tenant.',
      'This walk does not prove successful combiner placement or a run from the confirmation strip.',
      'The intake bodies are constructed in the test; they do not prove a recorded production dump matches uploaded drawing geometry.',
      'Setup runs the Solar settings changes through the API, not through the Solar settings form.',
    ],
  })
}

for (const [row, width, height] of [['R1', 1280, 800], ['R2', 1600, 1000]]) {
  test(`W20-07b ${row} Rooftop intake is reachable and real empty-graph refusals preserve head at ${width}x${height}`, async ({ page }, testInfo) => {
    if (!flagsOn(testInfo)) return
    test.setTimeout(180_000)
    await page.setViewportSize({ width, height })
    const context = await prepareDrawing(page, true)
    const before = await context.versionsOf()
    expect(before).toMatchObject({ head: 2, latest: 2 })
    const seat = await openSolarDrawing(page, context, 2)
    let panel = await openIntake(page, seat)
    await expect(seat.getByTestId('solar-combiner-reason')).toHaveText('Take the drawing checkout before importing the combiner intake')
    await expect(panel.getByLabel('Combiner intake file', { exact: true })).toBeDisabled()
    await expect(panel.getByRole('button', { name: 'Import', exact: true })).toBeDisabled()
    await takeLock(page)
    const close = seat.getByRole('button', { name: 'Close', exact: true })
    await reachable(close)
    await close.click()
    await expect(seat.getByRole('button', { name: 'Import combiner intake', exact: true })).toBeFocused()
    panel = await openIntake(page, seat)
    await expect(panel.getByLabel('Combiner intake file', { exact: true })).toBeFocused()
    await expect(panel.getByRole('button', { name: 'Import', exact: true })).toBeDisabled()
    await fillHardware(panel)
    const hardware = panel.getByRole('group', { name: 'Combiner hardware', exact: true })
    const placement = hardware.getByRole('button', { name: 'Run placement', exact: true })
    await expect(placement).toBeDisabled()
    await expect(hardware.getByText(IMPORT_FIRST, { exact: true })).toBeVisible()
    for (const [name, body, status, code, sentence] of [
      ['combiner-intake-one-group.json', ONE_GROUP, 409, 'COMBINER_L2_MODE_REQUIRED', 'Turn on L2 collectors in Solar settings before importing a combiner intake'],
      ['combiner-intake-no-groups.json', NO_GROUPS, 400, 'COMBINER_OUTLINES_INVALID', 'The panel group outlines are missing or not valid, so export them again'],
    ]) {
      await chooseBody(panel, name, body)
      const answered = importResponse(page)
      await submitImport(panel)
      const response = await answered
      expect(response.status()).toBe(status)
      expect((await response.json()).error.reason_code).toBe(code)
      // Playwright exposes no request body for a File-bodied fetch without interception, which this file forbids.
      // The body is bound by the sent Content-Length here and by the server's stored intake readback in R3.
      const sent = await response.request().allHeaders()
      expect(sent['content-length']).toBe(String(Buffer.byteLength(JSON.stringify(body))))
      expect(sent['content-type']).toBe('application/json')
      await expect(panel.getByRole('status')).toHaveText(sentence)
      expect(versionState(await context.versionsOf())).toEqual(versionState(before))
      await expect(placement).toBeDisabled()
      await expect(hardware.getByText(IMPORT_FIRST, { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Run solar-combiners', exact: true })).toHaveCount(0)
    }
    await noHorizontalOverflow(seat)
    const screenshot = testInfo.outputPath(`combiner-${width}x${height}.png`)
    await page.screenshot({ path: screenshot })
    receipt(`solar-wave20-combiner-${row.toLowerCase()}`, context, { head: 2, latest: 2 }, [screenshot], [
      'Rooftop intake is reachable inside the Solar flow seat; checkout gates import and close restores trigger focus',
      'valid hardware cannot enable placement before an import is stored',
      'real L2-mode and outlines refusals show the exact client sentences and preserve every version entry at head and latest 2',
      'no Run solar-combiners confirmation button exists',
    ])
  })
}

test('W20-07b R3 real empty-L2 intake refreshes the drawing; placement remains unavailable', async ({ page }, testInfo) => {
  if (!flagsOn(testInfo)) return
  test.setTimeout(180_000)
  await page.setViewportSize({ width: 1280, height: 800 })
  const context = await prepareDrawing(page, true, true)
  const before = await context.versionsOf()
  expect(before).toMatchObject({ head: 3, latest: 3 })
  const graphBefore = (await context.getJson(`/api/drawings/${context.drawingId}/intake`)).intake.solar_design_graph
  expect(graphBefore.rev).toBe(2)
  const seat = await openSolarDrawing(page, context, 3)
  await takeLock(page)
  const panel = await openIntake(page, seat)
  await fillHardware(panel)
  const capabilitiesAnswered = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'GET' && url.pathname === '/api/capabilities'
      && url.searchParams.get('drawing_id') === context.drawingId && url.searchParams.get('drawing_version') === '4'
  }, { timeout: 60_000 })
  const importAnswered = importResponse(page)
  await chooseBody(panel, 'combiner-intake-one-group.json', ONE_GROUP)
  await submitImport(panel)
  const imported = await importAnswered
  expect(imported.status()).toBe(200)
  const sent = await imported.request().allHeaders()
  expect(sent['content-length']).toBe(String(Buffer.byteLength(JSON.stringify(ONE_GROUP))))
  expect(sent['content-type']).toBe('application/json')
  expect(await imported.json()).toMatchObject({ created: true, version: 4, parent_version: 3, graph_rev: 2,
    bound: { l2_inverters: 0, strings: 0 }, panel_groups: 1 })
  await expect(panel.getByRole('status')).toHaveText('Combiner intake imported for this drawing.')
  const after = await context.versionsOf()
  expect(after).toMatchObject({ head: 4, latest: 4 })
  const stored = await context.getJson(`/api/drawings/${context.drawingId}/intake`)
  expect(stored.version).toBe(4)
  expect(stored.intake.combiner_intake).toEqual(ONE_GROUP.combiner_intake)
  expect(stored.intake.panel_groups).toEqual(ONE_GROUP.panel_groups)
  expect(after.versions).toHaveLength(before.versions.length + 1)
  for (const entry of before.versions) expect(after.versions).toContainEqual(entry)
  const graphAfter = (await context.getJson(`/api/drawings/${context.drawingId}/intake`)).intake.solar_design_graph
  expect(graphAfter.rev).toBe(2)
  expect(graphAfter).toEqual(graphBefore)
  const capabilitiesResponse = await capabilitiesAnswered
  expect(capabilitiesResponse.status()).toBe(200)
  const catalog = await capabilitiesResponse.json()
  const combiners = catalog.families.flatMap((family) => family.capabilities).find((entry) => entry.name === 'solar-combiners')
  expect(combiners).toBeTruthy()
  expect(combiners.availability.refusal_reasons).toContain('string_collectors_required')
  await expect(page.locator('.workspace-card')).toHaveAttribute('data-engine-document', `${context.drawingId}-v4.dxf`, { timeout: 60_000 })
  const placement = panel.getByRole('button', { name: 'Run placement', exact: true })
  await expect(placement).toBeEnabled({ timeout: 60_000 })
  const runs = []
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/run') runs.push(request.postDataJSON())
  })
  // The stored import's "Version 4 created" toast (Toast.jsx, 5 s) covers Run placement at 1280x800; wait for it to leave.
  await expect(page.locator('.toast', { hasText: 'Version 4 created' })).toHaveCount(0, { timeout: 15_000 })
  await reachable(placement)
  await placement.click()
  await expect(seat.getByTestId('solar-combiner-reason')).toHaveText('Combiner placement was not staged')
  await expect(page.getByRole('button', { name: 'Run solar-combiners', exact: true })).toHaveCount(0)
  expect(versionState(await context.versionsOf())).toEqual(versionState(after))
  expect(runs.filter((body) => body?.tool === 'solar-combiners')).toEqual([])
  await noHorizontalOverflow(seat)
  const screenshot = testInfo.outputPath('combiner-empty-l2.png')
  await page.screenshot({ path: screenshot })
  receipt('solar-wave20-combiner-r3', context, {
    head: 4, latest: 4, graph_rev: 2,
    description: 'real empty-L2 import; zero collectors, zero strings, zero outline vertices; placement not staged',
  }, [screenshot], [
    'the real import creates version 4 from parent 3 and binds zero collectors and zero strings with one empty-outline group',
    'the drawing refreshes to version 4 and its graph remains unchanged at revision 2',
    'the refreshed capabilities catalog refuses solar-combiners with string_collectors_required',
    'valid hardware enables Run placement, which reports placement not staged without a confirmation button or solar-combiners POST',
    'the placement request preserves all entries at head and latest 4',
  ])
})
