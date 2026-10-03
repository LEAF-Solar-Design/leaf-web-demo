import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { SEED_RESULT_SCHEMA, runBody, seedParams } from '../solarGraphCommitProof.mjs'
import { requireLocalReady } from './requireReady.mjs'

// Replay from web/ on a fresh managed account stack (one worker):
// $env:VITE_CAD_EDIT='1'; $env:VITE_SOLAR_FLOW_RAIL='1'; $env:VITE_SOLAR_SETTINGS_FORM='1'
// powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_unified_local_proof.ps1
//   -Mode account -TestGrep 'W26-R4 tracker rows publish and convert through the browser'
// The flag-gated return writes no receipt. Only the executed walk below writes its served SHA.
test.describe.configure({ mode: 'serial' })
const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')
const EXPECTED_M1_BODY = {
  operation: 'manual-create', rows: [
    { axis_start: [0, 0], axis_end: [0, 6], cross_axis_width_du: 2, slots: 3 },
    { axis_start: [4, 0], axis_end: [4, 10], cross_axis_width_du: 1, slots: 2 },
  ], module_power_watts: 450, expected_head: null,
}
function flagsOn(testInfo) {
  if (process.env.VITE_CAD_EDIT === '1' && process.env.VITE_SOLAR_FLOW_RAIL === '1' && process.env.VITE_SOLAR_SETTINGS_FORM === '1') return true
  testInfo.annotations.push({ type: 'flag-gate', description: 'Requires VITE_CAD_EDIT=1, VITE_SOLAR_FLOW_RAIL=1 and VITE_SOLAR_SETTINGS_FORM=1.' })
  return false
}
async function reachable(control) {
  await control.scrollIntoViewIfNeeded()
  await expect(control).toBeVisible()
  expect(await control.evaluate((element) => {
    const r = element.getBoundingClientRect()
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
    return hit === element || element.contains(hit)
  })).toBe(true)
}
async function click(control) { await reachable(control); await control.click() }
async function noHorizontalOverflow(seat) {
  expect(await seat.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
}

test('W26-R4 tracker rows publish and convert through the browser at 1280x800', async ({ page }, testInfo) => {
  test.setTimeout(300000)
  if (!flagsOn(testInfo)) return
  await page.setViewportSize({ width: 1280, height: 800 })
  const api = page.request
  await requireLocalReady(api, test, API_BASE)
  const observed = []
  const note = (method, path, response) => {
    observed.push(`${method} ${path.replace(/\/api\/drawings\/[^/]+(?=\/|$)/, '/api/drawings/{id}')} ${response.status()}`)
    return response
  }
  const getJson = async (path) => {
    const response = note('GET', path, await api.get(`${API_BASE}${path}`, { headers: TENANT_HEADERS, timeout: 15000 }))
    expect(response.status(), path).toBe(200)
    return response.json()
  }
  const upload = note('POST', '/api/drawings/upload', await api.post(`${API_BASE}/api/drawings/upload`, {
    headers: TENANT_HEADERS, timeout: 15000, multipart: { file: { name: 'distinctive-panel.dxf',
      mimeType: 'application/dxf', buffer: readFileSync(join(process.cwd(), 'e2e', 'fixtures', 'distinctive-panel.dxf')) } },
  }))
  expect(upload.status()).toBe(202)
  const uploadedReceipt = await upload.json()
  expect(uploadedReceipt).toMatchObject({ tenant_id: 'demo-tenant', tenant_kind: 'account' })
  const drawingId = uploadedReceipt.drawing_id
  expect(drawingId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  await expect.poll(async () => (await getJson(`/api/drawings/${drawingId}/upload-status`)).status,
    { timeout: 30000 }).toMatch(/^(ready|failed)$/)
  expect((await getJson(`/api/drawings/${drawingId}/upload-status`)).status).toBe('ready')
  const versionsOf = () => getJson(`/api/drawings/${drawingId}/versions`)
  const intakeOf = () => getJson(`/api/drawings/${drawingId}/intake`)
  const uploaded = await versionsOf()
  expect(uploaded).toMatchObject({ head: 1, latest: 1 })
  expect(uploaded.versions).toHaveLength(1)
  expect((await intakeOf()).intake.solar_design_graph ?? null).toBeNull()
  const health = await getJson('/api/health')
  expect(health.source_sha).toMatch(/^[0-9a-f]{40}$/)
  const tools = (await getJson('/api/tools')).tools
  const digestOf = (name) => {
    const tool = tools.find((candidate) => candidate.name === name)
    expect(tool).toBeTruthy()
    return tool.catalog_digest
  }
  const checkoutPath = `/api/drawings/${drawingId}/checkout`
  const checkout = note('POST', checkoutPath, await api.post(`${API_BASE}${checkoutPath}`, {
    headers: TENANT_HEADERS, data: { holder: 'drafter' }, timeout: 15000,
  }))
  expect(checkout.status()).toBe(200)
  const lease = await checkout.json()
  expect(lease).toMatchObject({ acquired: true, holder: 'drafter' })
  const capability = lease.checkout_capability
  expect(typeof capability === 'string' && capability.length > 0).toBe(true)
  try {
    const run = async (tool, params) => {
      const response = note('POST', '/api/run', await api.post(`${API_BASE}/api/run?wait=1`, {
        headers: { ...TENANT_HEADERS, 'Content-Type': 'application/json', 'X-Checkout-Capability': capability },
        data: runBody({ tool, drawingId, params, catalogDigest: digestOf(tool) }), timeout: 45000,
      }))
      expect(response.status()).toBe(200)
      const body = await response.json()
      expect(body.ok).toBe(true)
      return body
    }
    const params = seedParams(uploaded.versions[0])
    params.initialize.units.drawing_units = 'm'
    const seeded = await run('solar-settings', params)
    expect(seeded.result).toMatchObject({ schema_version: SEED_RESULT_SCHEMA, after_rev: 1,
      new_version: { drawing_id: drawingId, version: 2 } })
    expect((await intakeOf()).intake.solar_design_graph.rev).toBe(1)
    const profile = JSON.parse(readFileSync(join(process.cwd(), '..', 'docs', 'parity', 'evidence',
      'ground', 'generate', 'profile-settings.json'), 'utf8'))
    delete profile.Name
    delete profile.IsBuiltIn
    expect(Object.keys(profile)).toHaveLength(60)
    profile.InstallationDesign = 'Ground'
    profile.UseL2Collectors = false
    const ground = await run('solar-design-presets', { expected_rev: 1, subcommand: 'Create',
      name: 'Tracker rows proof', current_settings: profile })
    expect(ground.result.new_version.version).toBe(3)
    const graph = (await intakeOf()).intake.solar_design_graph
    expect(graph.rev).toBe(2)
    expect(graph.project.installation_design).toBe('Ground')
    expect(graph.project.units).toMatchObject({ drawing_units: 'm', meters_per_unit: 1 })
    expect(graph.frames).toEqual([])
  } finally {
    const release = note('DELETE', checkoutPath, await api.delete(`${API_BASE}${checkoutPath}`, {
      headers: { ...TENANT_HEADERS, 'X-Checkout-Capability': capability }, timeout: 15000,
    }))
    expect(release.status()).toBe(200)
  }
  const projectId = (await intakeOf()).intake.solar_design_graph.project.id
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname
    if (path.startsWith('/api/')) note(response.request().method(), path, response)
  })
  await page.addInitScript((id) => sessionStorage.setItem('leaf.cat.workbench.id.v1', id), drawingId)
  await page.goto(`/app?surface=solar&drawing=${drawingId}`)
  await expect(page.locator('.workspace-card')).toHaveAttribute('data-engine-document', `${drawingId}-v3.dxf`, { timeout: 60000 })
  await expect(page.locator('.studio-ground .viewer-canvas canvas')).toHaveCount(1, { timeout: 30000 })
  await expect(page.getByRole('tab', { name: 'Solar CAD', exact: true })).toHaveAttribute('aria-selected', 'true')
  const seat = page.getByTestId('solar-flow-seat')
  await expect(seat).toHaveCount(1)
  await expect(seat).toBeVisible()
  await click(page.getByRole('button', { name: 'Take edit lock', exact: true }))
  await expect(page.getByText('You hold the edit lock')).toBeVisible()
  const select = page.getByTestId('solar-flow-select')
  await reachable(select); await select.selectOption('ground-physical')
  await click(seat.getByRole('button', { name: 'Create tracker rows', exact: true }))
  const panel = seat.getByRole('region', { name: 'Tracker layout', exact: true })
  await expect(panel.getByRole('heading', { name: 'Tracker layout', exact: true })).toBeFocused()
  await expect(panel.getByText('No physical state has been published for this drawing.', { exact: true })).toBeVisible()
  await expect(panel.getByText('Metres', { exact: true })).toBeVisible()
  await expect(panel.getByText('Current head index', { exact: true })).toHaveCount(0)
  for (let index = 0; index < 2; index += 1) {
    if (index) await click(panel.getByRole('button', { name: 'Add row', exact: true }))
    const group = panel.getByRole('group', { name: `Row ${index + 1}`, exact: true })
    const row = EXPECTED_M1_BODY.rows[index]
    for (const [name, value] of [['Axis start X', row.axis_start[0]], ['Axis start Y', row.axis_start[1]],
      ['Axis end X', row.axis_end[0]], ['Axis end Y', row.axis_end[1]], ['Cross axis width', row.cross_axis_width_du], ['Slots', row.slots]]) {
      const input = group.getByRole('textbox', { name, exact: true })
      await reachable(input); await input.fill(String(value))
    }
  }
  const power = panel.getByRole('textbox', { name: 'Module power', exact: true })
  await reachable(power); await power.fill('450')
  await noHorizontalOverflow(seat)
  const trackerRequests = []
  const conversionRequests = []
  page.on('request', (request) => {
    if (request.method() !== 'POST') return
    const url = new URL(request.url())
    if (url.pathname === `/api/drawings/${drawingId}/tracker-rows`) trackerRequests.push(request)
    if (url.pathname === '/api/run' && request.postDataJSON()?.tool === 'solar-trackers-to-panel-groups') conversionRequests.push(request)
  })
  const publishedResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/drawings/${drawingId}/tracker-rows`, { timeout: 120000 })
  const followed = page.waitForResponse((response) => response.request().method() === 'GET'
    && new URL(response.url()).pathname === `/api/drawings/${drawingId}/terrain`, { timeout: 120000 })
  const publish = panel.getByRole('button', { name: 'Publish tracker rows', exact: true })
  await expect(publish).toBeEnabled()
  await click(publish)
  const response = await publishedResponse
  expect(response.status()).toBe(201)
  expect(response.request().postDataJSON()).toEqual(EXPECTED_M1_BODY)
  // The live local mount supplies no project query; the response binds the recorded graph project.
  expect(response.url()).toBe(`${API_BASE}/api/drawings/${drawingId}/tracker-rows`)
  expect(new URL(response.url()).searchParams.get('project_id')).toBeNull()
  const sentHeaders = await response.request().allHeaders()
  expect(sentHeaders['content-type']).toBe('application/json')
  expect(typeof sentHeaders['x-checkout-capability'] === 'string' && sentHeaders['x-checkout-capability'].length > 0).toBe(true)
  const publication = await response.json()
  expect(publication).toMatchObject({ drawing_id: drawingId, project_id: projectId, expected_head: null,
    operation: 'manual-create', outcome: 'published', created: true,
    summary: { rows: 2, slots: 5, module_power_watts: 450 }, head: { index: 0, parent: null } })
  expect(trackerRequests).toHaveLength(1)
  await expect(panel.getByTestId('solar-tracker-rows-status')).toHaveText('Manual tracker rows were published for this drawing.')
  const receipt = panel.locator('dl[aria-label="Published tracker rows"]')
  await expect(receipt.locator('dd')).toHaveText(['2', '5', '450 W', '0', 'Metres'])
  const followResponse = await followed
  expect(followResponse.status()).toBe(200)
  expect(await followResponse.json()).toMatchObject({ stored: true, head: { index: 0,
    state: { artifact_id: publication.head.state.artifact_id } } })
  const physical = await getJson(publication.head.state.download)
  expect(physical.units).toMatchObject({ drawing_units: 'm', meters_per_unit: 1 })
  expect(physical.state.tracker_rows).toEqual(EXPECTED_M1_BODY.rows.map((row, row_index) => ({ ...row, row_index, source_command: 'manual' })))
  expect(physical.state.settings.TrackerModulePmaxW).toBe(450)
  expect(await versionsOf()).toMatchObject({ head: 3, latest: 3 })
  await noHorizontalOverflow(seat)
  const publicationScreenshot = testInfo.outputPath('tracker-rows-published-1280x800.png')
  await page.screenshot({ path: publicationScreenshot })
  await click(panel.getByRole('button', { name: 'Close', exact: true }))
  await reachable(select); await select.selectOption('ground-electrical')
  const conversion = page.locator('#solar-step-solar-trackers-to-panel-groups')
  await expect(conversion).toBeEnabled({ timeout: 60000 })
  await click(conversion)
  const editor = page.locator('#solar-step-editor')
  await expect(editor.getByLabel('Expected rev', { exact: true })).toHaveValue('2', { timeout: 30000 })
  await click(editor.getByRole('button', { name: 'Review & run', exact: true }))
  const runResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/run'
    && response.request().postDataJSON()?.tool === 'solar-trackers-to-panel-groups', { timeout: 120000 })
  await click(page.getByRole('button', { name: 'Run solar-trackers-to-panel-groups', exact: true }))
  const conversionResponse = await runResponse
  expect([200, 202]).toContain(conversionResponse.status())
  expect(conversionRequests).toHaveLength(1)
  expect(conversionRequests[0].postDataJSON().params).toEqual({ drawing_id: drawingId, expected_rev: 2 })
  // The browser follows its normal job lifecycle. Readback waits for the committed version.
  await expect.poll(async () => (await intakeOf()).version, { timeout: 120000 }).toBe(4)
  const committed = await intakeOf()
  const graph = committed.intake.solar_design_graph
  expect(committed.version).toBe(4)
  expect(graph.rev).toBe(3)
  expect(graph.frames).toHaveLength(2)
  expect(graph.frames.map((frame) => frame.module_slots)).toEqual([3, 2])
  expect(graph.frames.reduce((sum, frame) => sum + frame.module_slots, 0)).toBe(5)
  expect(graph.frames.map((frame) => frame.insertion_point)).toEqual([[0, 3], [4, 5]])
  expect(graph.frames.map((frame) => frame.module_power_watts)).toEqual([450, 450])
  expect(graph.frames.map((frame) => frame.installation_design)).toEqual(['Ground', 'Ground'])
  expect(await versionsOf()).toMatchObject({ head: 4, latest: 4 })
  await expect(page.locator('.workspace-card')).toHaveAttribute('data-engine-document', `${drawingId}-v4.dxf`, { timeout: 60000 })
  expect(trackerRequests).toHaveLength(1)
  expect(conversionRequests).toHaveLength(1)
  await noHorizontalOverflow(seat)
  const conversionScreenshot = testInfo.outputPath('tracker-rows-converted-1280x800.png')
  await page.screenshot({ path: conversionScreenshot })
  writeProofReceipt(join(PROOF_DIR, 'w26-r4-tracker-rows-receipt.json'), {
    capability_ids: ['ID-04'], evidence_tier: 'local-e2e', route: `/app?surface=solar&drawing=${drawingId}`,
    runtime: 'real local Vite, FastAPI, upload extraction, checkout, broker, worker and version stores',
    source_commit: health.source_sha, api_endpoints: observed, artifacts: [publicationScreenshot, conversionScreenshot],
    assertions: ['browser request equals the four-key metre M1 body and publishes exactly once at physical head zero',
      'immutable physical readback retains the ordered manual rows and module power without advancing drawing version three',
      'the browser conversion sends revision two once and seats drawing version four',
      'persisted revision three has two Ground frames with slots three and two, power 450 W and metre centres [0,3] and [4,5]'],
    result: { verdict: 'pass', source_sha: health.source_sha, drawing_id: drawingId,
      physical_artifact: publication.head.state.artifact_id, physical_head_index: publication.head.index,
      graph_revisions: [1, 2, 3], converted_frames: 2, converted_slots: 5, centres: [[0, 3], [4, 5]] },
    limitations: ['Local demo tenant; LEAF_AUTH_LIVE=0.', 'APS_LIVE=0; Autodesk APS is outside this proof.',
      'Setup seed and Ground preset calls used the API.',
      'Manually entered rows are not automatic layout or engineering validation.'],
  })
})
