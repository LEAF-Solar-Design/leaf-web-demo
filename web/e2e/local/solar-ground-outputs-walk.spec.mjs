import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { inflateRawSync } from 'node:zlib'
import { basename, dirname, join, resolve } from 'node:path'
import { PRESET_LABELS } from '../../src/solar/solarPresetModel.js'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')
const REPLAY_LABEL = 'SYNTHETIC, TEST ONLY: recorded String Sizer replay, not live sizing.'
const REQUEST_SHA = 'c2adcc2616b290a64eacbdc9f3857447d5de567a3d297bb23b87d7bdabe269b5'
const EXPECTED_M1_BODY = {
  operation: 'manual-create',
  rows: [
    { axis_start: [0, 0], axis_end: [0, 6], cross_axis_width_du: 2, slots: 3 },
    { axis_start: [4, 0], axis_end: [4, 10], cross_axis_width_du: 1, slots: 2 },
  ],
  module_power_watts: 450,
  expected_head: null,
}
// Measured outputs (the four NEC builtins run with this walk's params, rendered through the read-result
// view in the server's key order): pinned so a changed computed value fails the walk instead of passing.
const AMPACITY_REJECTED = [{ description: 'I_base with no temperature correction', would_have_resulted_in: 70,
  why_rejected: 'Required when ambient exceeds 30\u00b0C.' }]
const CONDUCTORS = [
  { role: 'current-carrying', gauge: '10', unit: 'AWG', insulation: 'THWN2', count: 2, area_sq_in: 0.0211 },
  { role: 'egc', gauge: '10', unit: 'AWG', insulation: 'Bare', count: 1, area_sq_in: 0.0106 }]
const CONDUCTOR_ROWS = [['current-carrying', '10', 'AWG', 'THWN2', '2', '0.0211'],
  ['egc', '10', 'AWG', 'Bare', '1', '0.0106']]
const CONDUIT_ROWS = [['1/2', '0.285'], ['3/4', '0.508'], ['1', '0.832'], ['1-1/4', '1.453'], ['1-1/2', '1.986'],
  ['2', '3.291'], ['2-1/2', '4.695'], ['3', '7.268'], ['3-1/2', '9.737'], ['4', '12.554']]
const CONDUIT_TABLE = CONDUIT_ROWS.map(([trade_size, area]) => ({ trade_size, area_sq_in: Number(area) }))
// The export builtins run on this chain's final graph (v13 / rev 12, the G5 walk's stored graph): the cable
// workbook's sheets in order with their row counts and the two header rows, and the schedules' tables.
const CABLE_SHEETS = ['Homeruns', 'Inverter Schedule', 'String Schedule', 'Feeder Schedule']
const CABLE_SHEET_ROWS = [5, 7, 9, 2]
const HOMERUN_HEADERS = ['Inverter/MPPT', 'Circuit', 'Which', 'Mod / String', 'Length (ft)',
  'Circuit Length (ft)', 'One-Way Length (ft)']
const FEEDER_HEADERS = ['Combiner Box #', 'Inverter #', 'Feeder Length (ft)']
const SCHEDULE_TABLES = ['COMBINER / INVERTER SCHEDULE', 'STRING SCHEDULE', 'FEEDER SCHEDULE']
const COMBINER_SCHEDULE_HEADERS = ['Inverter', 'Combiner', 'Strings', 'Mod/String', 'Total Modules',
  'DC Power (kW)', 'AC Power (kW)', 'DC/AC Ratio']
const STRING_SCHEDULE_HEADERS = ['CB', 'Inv', 'String', 'Zone', 'Modules', 'Voc (V)', 'Voc_cold (V)', 'Vmax (V)',
  'Margin (V)', 'Vmp (V)', 'Isc (A)', 'Isc\u00d71.25', 'Power (W)', 'HR (ft)', 'Gauge', 'Amp (A)', 'Vd (%)', 'Std']
// A schedule length is feet at one decimal. The server rounds half to even on the written value, so the test
// allows half a tenth rather than restating that rule: the cell must still be a number near the graph's length.
function expectFeet(cell, feet) {
  expect(cell).toMatch(/^\d+(\.\d)?$/)
  expect(Math.abs(Number(cell) - feet)).toBeLessThanOrEqual(0.05 + 1e-9)
}
function tagNumber(string) {
  const match = /^S(\d+)$/.exec(string.circuit_tag)
  expect(match).not.toBeNull()
  return Number(match[1])
}

// An .xlsx is a zip of XML parts. The writer records each part's size in its local header (no data
// descriptor) and deflates every part, so walk the local headers, inflate each part, and refuse any other
// shape: a matching length and digest prove the transfer, this proves the file is the workbook.
function workbookParts(bytes) {
  const parts = new Map()
  let offset = 0
  expect(bytes.length).toBeGreaterThanOrEqual(30)
  while (offset + 30 <= bytes.length && bytes.readUInt32LE(offset) === 0x04034b50) {
    expect(parts.size).toBeLessThan(64)
    expect(bytes.readUInt16LE(offset + 6) & 0x08).toBe(0)
    expect(bytes.readUInt16LE(offset + 8)).toBe(8)
    const size = bytes.readUInt32LE(offset + 18)
    const nameLength = bytes.readUInt16LE(offset + 26)
    const start = offset + 30 + nameLength + bytes.readUInt16LE(offset + 28)
    expect(start + size).toBeLessThanOrEqual(bytes.length)
    const name = bytes.subarray(offset + 30, offset + 30 + nameLength).toString('utf8')
    expect(parts.has(name)).toBe(false)
    parts.set(name, inflateRawSync(bytes.subarray(start, start + size)).toString('utf8'))
    offset = start + size
  }
  expect(parts.size).toBeGreaterThan(0)
  expect(offset + 4).toBeLessThanOrEqual(bytes.length)
  expect(bytes.readUInt32LE(offset)).toBe(0x02014b50)
  return parts
}

const SIZING_TEXT = {
  Module: 'JA_Solar_JAM72D40-595/MB', Inverter: 'Sungrow SG-HX SG250HX',
  'Bifacial coefficient': '.7', 'Surface tilt': '5', 'Surface azimuth': '180',
  Albedo: '.25', 'Maximum voltage': '1500', 'Thermal model': 'close mount glass glass',
  'Grant reference': 'synthetic-sizing-g2',
}
const SIZING_SELECT = { Scope: 'global', Bifacial: 'false', Racking: 'fixed_tilt', 'Open circuit rise': 'false' }

// Match Python uuid.UUID(bytes=sha256(seed)[:16], version=4), including its version and variant bits.
function groundPanelId(sourceHash, frameIndex, slotIndex) {
  const bytes = createHash('sha256').update(`${sourceHash}:ground-panel:${frameIndex}:${slotIndex}`).digest().subarray(0, 16)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = bytes.toString('hex')
  return `leaf:panel:${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function flagsOn(testInfo) {
  if (process.env.VITE_CAD_EDIT === '1' && process.env.VITE_SOLAR_SETTINGS_FORM === '1' && process.env.VITE_SOLAR_FLOW_RAIL === '1') return true
  testInfo.annotations.push({ type: 'flag-gate', description: 'Requires VITE_CAD_EDIT=1, VITE_SOLAR_FLOW_RAIL=1 and VITE_SOLAR_SETTINGS_FORM=1.' })
  return false
}
// A <select> wrapped by its <label> carries the option text in the label's text, so getByLabel(exact) never
// matches it (measured with a probe page, 2026-10-03); every select here is found by its combobox role and name.
async function reachable(control) {
  await control.scrollIntoViewIfNeeded()
  await expect(control).toBeVisible()
  // A transient toast (about 5 s, components/Toast.jsx) may sit over a control for a moment, so the centre
  // must come free within 10 s; a cover that outlasts that is a real defect and fails here.
  await expect.poll(() => control.evaluate((element) => {
    const r = element.getBoundingClientRect()
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
    return hit === element || element.contains(hit)
  }), { timeout: 10000 }).toBe(true)
}
async function click(control) { await reachable(control); await control.click() }
async function fill(control, value) { await reachable(control); await control.fill(String(value)) }
async function select(control, value) { await reachable(control); await control.selectOption(value) }
async function noHorizontalOverflow(seat) {
  expect(await seat.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
}
async function ribbon(page, tab, target) {
  await click(page.getByRole('tab', { name: tab, exact: true }))
  // isVisible() does not wait: right after the tab click the panel's tools may not be painted yet, and on a
  // desktop width there is no More panels button, so wait for either before choosing (W1 run, 2026-10-03).
  const more = page.getByRole('button', { name: 'More panels', exact: true })
  // Poll the two: .or().first() resolves to the first match in DOM order, which is the target even while it is hidden
  // in a collapsed overflow (the Solar tab at 1280 wide keeps presets behind More panels).
  await expect.poll(async () => (await target.isVisible()) || (await more.isVisible()), { timeout: 60000 }).toBe(true)
  const overflow = !await target.isVisible()
  if (overflow) await click(more)
  await expect(target).toBeEnabled({ timeout: 60000 })
  await click(target)
  // A tool picked from the overflow leaves the overflow open over the form it opened (DraftingRibbon closes it only
  // on Escape or when focus leaves the ribbon), so close it the way a drafter would before using the form.
  // Press it only while the overflow is open: a bare Escape on a closed ribbon reaches the window and can dismiss the
  // form the tool just opened.
  const ribbonBar = page.getByTestId('drafting-ribbon')
  if (overflow && await ribbonBar.getAttribute('data-overflow-open') === 'true') {
    await page.keyboard.press('Escape')
    await expect(ribbonBar).not.toHaveAttribute('data-overflow-open', 'true')
  }
}

test('G6 ground NEC outputs and downloads through the browser', async ({ page }, testInfo) => {
  test.setTimeout(1200000)
  page.setDefaultTimeout(60000)
  if (!flagsOn(testInfo)) return
  expect(process.env.LEAF_E2E_MANAGED).toBe('1')
  expect(process.env.LEAF_AUTH_LIVE).toBe('0')
  for (const key of ['LEAF_RUNTIME_ENV', 'LEAF_ENV']) {
    expect(['', 'local', 'test', 'development', 'dev']).toContain((process.env[key] || '').trim().toLowerCase())
  }
  expect(process.env.LEAF_STORE_DIR).toEqual(expect.any(String))
  const runRoot = dirname(resolve(process.env.LEAF_STORE_DIR))
  const runId = basename(runRoot).replace(/^leaf-unified-local-e2e-/, '')
  expect(basename(runRoot)).toBe(`leaf-unified-local-e2e-${runId}`)
  expect(runId).toMatch(/^\d{8}-\d{6}$/)
  const replayPath = join(runRoot, 'synthetic-sizing-receipt.json')
  const manifest = JSON.parse(readFileSync(join(process.cwd(), '..', 'scripts', 'fixtures', 'string_sizer_replay_manifest.json'), 'utf8'))
  const fixture = manifest.fixtures.find((entry) => entry.id === 'w1-string-length-recorded')
  expect(fixture.request_wire_sha256).toBe(REQUEST_SHA)
  const readReplay = () => {
    const receipt = JSON.parse(readFileSync(replayPath, 'utf8'))
    expect(receipt).toMatchObject({ schema: 'leaf.synthetic-string-sizing-receipt.v1',
      label: REPLAY_LABEL, run_id: runId, record: 'sf-w3-sizing-replay',
      grant_ref: 'synthetic-sizing-g2', tenant_id: 'demo-tenant', ok: true, ttl_expired: false })
    expect(receipt.endpoint).toMatch(/^http:\/\/127\.0\.0\.1:[0-9]{1,5}\/string-length$/)
    const endpoint = new URL(receipt.endpoint)
    expect(Number(endpoint.port)).toBeGreaterThan(0)
    expect(Number(endpoint.port)).toBeLessThanOrEqual(65535)
    expect([...receipt.bootstrap_roles].sort()).toEqual(['app', 'broker'])
    expect(receipt.fixtures).toEqual([{ id: fixture.id, fixture_sha256: fixture.fixture_sha256,
      request_wire_sha256: fixture.request_wire_sha256, response_wire_sha256: fixture.response_wire_sha256 }])
    return receipt
  }
  // Current runner root only; an absent or stale supervisor cannot authorize mutations.
  const replayBefore = readReplay()
  expect(replayBefore.events).toEqual([])
  await page.setViewportSize({ width: 1280, height: 800 })
  await requireLocalReady(page.request, test, API_BASE)
  const healthClient = page.request
  const healthResponse = await healthClient.get(`${API_BASE}/api/health`, { timeout: 15000 })
  expect(healthResponse.status()).toBe(200)
  const health = await healthResponse.json()
  expect(health.source_sha).toMatch(/^[0-9a-f]{40}$/)
  expect(process.env.LEAF_SOURCE_COMMIT).toMatch(/^[0-9a-f]{40}$/)
  expect(health.source_sha).toBe(process.env.LEAF_SOURCE_COMMIT)

  // Only browser-originated reads populate these observations; no test readback requests.
  const observed = ['GET /api/health 200']
  const uploads = []
  const publications = []
  const runs = []
  const intakes = []
  const jobs = []
  const observationErrors = []
  let drawingId
  page.on('request', (request) => {
    if (request.method() !== 'POST') return
    const path = new URL(request.url()).pathname
    if (path === '/api/drawings/upload') uploads.push(request)
    if (path === '/api/run') runs.push(request.postDataJSON())
    if (path === `/api/drawings/${drawingId}/tracker-rows`) publications.push(request)
  })
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname
    if (!path.startsWith('/api/')) return
    const sanitized = path.replace(/\/api\/drawings\/[^/]+(?=\/|$)/, '/api/drawings/{id}')
      .replace(/\/api\/jobs\/[^/]+(?=\/|$)/, '/api/jobs/{id}')
    observed.push(`${response.request().method()} ${sanitized} ${response.status()}`)
    if (response.request().method() !== 'GET' || response.status() !== 200) return
    const sink = path === `/api/drawings/${drawingId}/intake` ? intakes
      : /^\/api\/jobs\/[^/]+$/.test(path) ? jobs : null
    if (sink) response.json().then((body) => sink.push(body)).catch((error) => observationErrors.push(error.message))
  })
  const runCount = (tool) => runs.filter((run) => run.tool === tool)
  const completedRuns = []
  const storedGraphs = []
  let verifiedHead = 1
  const confirmRun = async (tool) => {
    const currentHead = verifiedHead
    const responseWait = page.waitForResponse((response) => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/run'
      && response.request().postDataJSON()?.tool === tool, { timeout: 120000 })
    const button = page.getByRole('button', { name: `Run ${tool}`, exact: true })
    await expect(button).toBeEnabled({ timeout: 60000 })
    await click(button)
    const response = await responseWait
    // Every run is submitted as a job, so the route answers 202 (accepted) and never 200; a 200 is a regression.
    expect(response.status()).toBe(202)
    const body = await response.json()
    expect(body.job_id).toEqual(expect.any(String))
    await expect.poll(() => jobs.findLast((job) => job.job_id === body.job_id
      && ['complete', 'failed'].includes(job.status))?.status ?? null, { timeout: 120000 })
      .toBe('complete')
    const run = response.request().postDataJSON()
    expect(run.dwg_version).toBe(currentHead)
    expect(run.params.drawing_id).toBe(drawingId)
    expect(completedRuns.some((entry) => entry.body.job_id === body.job_id)).toBe(false)
    expect(runs.filter((entry) => entry.tool === tool && entry.dwg_version === currentHead)).toHaveLength(1)
    const completeJob = jobs.findLast((job) => job.job_id === body.job_id && job.status === 'complete')
    const entry = { tool, dwg_version: run.dwg_version, params: run.params, body, job: completeJob }
    completedRuns.push(entry)
    return entry
  }
  const head = async (version) => {
    const document = `${drawingId}-v${version}.dxf`
    await expect(page.locator('.workspace-card[data-engine-document]')).toHaveAttribute('data-engine-document', document, { timeout: 60000 })
    await expect(page.getByTestId('dock-drawing').locator('dt').filter({ hasText: /^Name$/ }).locator('+ dd')).toHaveText(document, { timeout: 60000 })
    verifiedHead = version
  }
  const reopenGraph = async (tool, version, rev) => {
    const freshIntake = page.waitForRequest((request) => request.method() === 'GET'
      && new URL(request.url()).pathname === `/api/drawings/${drawingId}/intake`, { timeout: 60000 })
    await click(page.locator(`#solar-step-${tool}`))
    const response = await (await freshIntake).response()
    expect(response.status()).toBe(200)
    const view = await response.json()
    expect(view.version).toBe(version)
    const graph = view.intake.solar_design_graph
    expect(graph.rev).toBe(rev)
    expect(graph.parent_rev).toBe(rev - 1)
    storedGraphs.push({ version, graph })
    return view
  }
  const intakeAt = async (version) => {
    for (const form of [page.getByTestId('solar-preset-form'), page.locator('#solar-step-editor')]) {
      if (await form.isVisible()) await click(form.getByRole('button', { name: 'Cancel', exact: true }).last())
    }
    const form = page.getByTestId('solar-settings-form')
    if (await form.isVisible()) await click(form.getByRole('button', { name: 'Close', exact: true }))
    const view = await reopenGraph('solar-settings', version, version - 1)
    await expect(form).toHaveAttribute('data-mode', 'edit', { timeout: 30000 })
    await click(form.getByRole('button', { name: 'Close', exact: true }))
    return view
  }
  const screenshots = []
  const screenshot = async (name) => {
    const path = testInfo.outputPath(name)
    await page.screenshot({ path })
    screenshots.push(path)
  }

  const chain = await test.step('G6-1 ground chain reaches feeders', async () => {
  await page.goto('/app?surface=browser')
  await expect(page.getByRole('tab', { name: 'Browser', exact: true })).toHaveAttribute('aria-selected', 'true')
  await ribbon(page, 'Project', page.getByRole('button', { name: 'Upload drawing', exact: true }))
  await expect(page.getByLabel('Drawing file', { exact: true })).toHaveCount(1)
  const uploadButton = page.getByRole('button', { name: 'Upload DWG or DXF', exact: true })
  await expect(uploadButton).toBeEnabled({ timeout: 60000 })
  const uploadWait = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/drawings/upload', { timeout: 120000 })
  const chooser = page.waitForEvent('filechooser', { timeout: 60000 })
  await click(uploadButton)
  await (await chooser).setFiles(join(process.cwd(), 'e2e', 'fixtures', 'distinctive-panel.dxf'))
  const uploadResponse = await uploadWait
  expect(uploadResponse.status()).toBe(202)
  const uploadedReceipt = await uploadResponse.json()
  expect(uploadedReceipt).toMatchObject({ tenant_kind: 'account', tenant_id: 'demo-tenant' })
  drawingId = uploadedReceipt.drawing_id
  expect(drawingId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  expect(uploads).toHaveLength(1)
  await expect(page.getByRole('status').filter({ hasText: /^Drawing ready$/ })).toBeVisible({ timeout: 60000 })
  await click(page.getByRole('tab', { name: 'Solar CAD', exact: true }))
  await expect(page.getByRole('tab', { name: 'Solar CAD', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('.app[data-surface="solar"]')).toBeVisible()
  await head(1)
  await expect(page.getByTestId('dock-drawing').locator('dt').filter({ hasText: /^Entities$/ }).locator('+ dd')).toHaveText('1')
  await click(page.getByRole('button', { name: 'Take edit lock', exact: true }))
  await expect(page.getByText('You hold the edit lock', { exact: true })).toBeVisible({ timeout: 30000 })

  const seat = page.getByTestId('solar-flow-seat')
  await expect(seat).toBeVisible()
  await click(page.locator('#solar-step-solar-settings'))
  const settings = page.getByTestId('solar-settings-form')
  await expect(settings).toHaveAttribute('data-mode', 'initialize', { timeout: 30000 })
  await select(settings.getByRole('combobox', { name: 'Drawing units', exact: true }), 'm')
  await fill(settings.getByLabel('Panels in sequence', { exact: true }), '3')
  await expect(settings.getByLabel('ZIP code', { exact: true })).toHaveValue('')
  await click(settings.getByRole('button', { name: 'Start Solar design', exact: true }))
  const initialized = await confirmRun('solar-settings')
  expect(initialized.params).toEqual({ drawing_id: drawingId, expected_rev: 0, changes: { panels_in_sequence: 3 }, initialize: {
    schema_version: 1, source_intake_sha256: initialized.params.initialize.source_intake_sha256, units: { drawing_units: 'm', elevation_datum: 'unknown', crs: null,
      wcs_to_ucs: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1] },
  } })
  expect(initialized.params.changes).toEqual({ panels_in_sequence: 3 })
  expect(initialized.params.initialize.source_intake_sha256).toMatch(/^[0-9a-f]{64}$/)
  expect(runCount('solar-settings')).toHaveLength(1)
  await head(2)
  await expect(settings).toHaveAttribute('data-mode', 'edit', { timeout: 30000 })
  const initializedGraph = (await intakeAt(2)).intake.solar_design_graph
  expect(initializedGraph.source_hash).toBe(initialized.params.initialize.source_intake_sha256)
  await click(page.locator('#solar-step-solar-settings'))
  await expect(settings.getByText('Graph revision 1', { exact: true })).toBeVisible()
  await expect(settings.getByLabel('ZIP code', { exact: true })).toHaveValue('')
  await click(settings.getByRole('button', { name: 'Close', exact: true }))

  await ribbon(page, 'Solar', page.locator('button[data-tool="solar-design-presets"]'))
  const preset = page.getByTestId('solar-preset-form')
  await expect(preset.getByLabel('Graph revision', { exact: true })).toHaveValue('1', { timeout: 30000 })
  await select(preset.getByRole('combobox', { name: 'Action', exact: true }), 'Create')
  await fill(preset.getByLabel('Preset name', { exact: true }), 'Tracker rows proof')
  await click(preset.getByRole('radio', { name: 'Supply the settings', exact: true }))
  const profile = JSON.parse(readFileSync(join(process.cwd(), '..', 'docs', 'parity', 'evidence', 'ground', 'generate', 'profile-settings.json'), 'utf8'))
  delete profile.Name
  delete profile.IsBuiltIn
  expect(Object.keys(profile)).toHaveLength(60)
  profile.InstallationDesign = 'Ground'
  profile.UseL2Collectors = false
  for (const [key, value] of Object.entries(profile)) {
    expect(PRESET_LABELS[key]).toBeTruthy()
    const control = preset.getByLabel(PRESET_LABELS[key], { exact: true })
    await reachable(control)
    if (typeof value === 'boolean') await control.setChecked(value)
    else await control.fill(String(value))
  }
  await click(preset.getByRole('button', { name: 'Review & run', exact: true }))
  const created = await confirmRun('solar-design-presets')
  expect(created.params).toEqual({ drawing_id: drawingId, expected_rev: 1, subcommand: 'Create', name: 'Tracker rows proof', current_settings: profile })
  expect(created.params.current_settings).toEqual(profile)
  expect(runCount('solar-design-presets')).toHaveLength(1)
  await head(3)
  const ground = (await intakeAt(3)).intake.solar_design_graph
  expect(ground.rev).toBe(2)
  expect(ground.project.installation_design).toBe('Ground')
  expect(ground.project.units).toMatchObject({ drawing_units: 'm', meters_per_unit: 1 })
  expect(ground.frames).toEqual([])

  const flow = page.getByTestId('solar-flow-select')
  await select(flow, 'ground-physical')
  await click(seat.getByRole('button', { name: 'Create tracker rows', exact: true }))
  const panel = seat.getByRole('region', { name: 'Tracker layout', exact: true })
  await expect(panel.getByRole('heading', { name: 'Tracker layout', exact: true })).toBeFocused()
  await expect(panel.getByText('No physical state has been published for this drawing.', { exact: true })).toBeVisible({ timeout: 30000 })
  await expect(panel.getByText('Metres', { exact: true })).toBeVisible()
  await expect(panel.getByText('Current head index', { exact: true })).toHaveCount(0)
  for (let index = 0; index < 2; index += 1) {
    if (index) await click(panel.getByRole('button', { name: 'Add row', exact: true }))
    const group = panel.getByRole('group', { name: `Row ${index + 1}`, exact: true })
    const row = EXPECTED_M1_BODY.rows[index]
    for (const [label, value] of [['Axis start X', row.axis_start[0]], ['Axis start Y', row.axis_start[1]],
      ['Axis end X', row.axis_end[0]], ['Axis end Y', row.axis_end[1]], ['Cross axis width', row.cross_axis_width_du], ['Slots', row.slots]]) {
      await fill(group.getByRole('textbox', { name: label, exact: true }), value)
    }
  }
  await fill(panel.getByRole('textbox', { name: 'Module power', exact: true }), '450')
  await noHorizontalOverflow(seat)
  const publicationWait = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/drawings/${drawingId}/tracker-rows`, { timeout: 120000 })
  await expect(panel.getByRole('button', { name: 'Publish tracker rows', exact: true })).toBeEnabled({ timeout: 30000 })
  await click(panel.getByRole('button', { name: 'Publish tracker rows', exact: true }))
  const publicationResponse = await publicationWait
  expect(publicationResponse.status()).toBe(201)
  expect(publicationResponse.request().postDataJSON()).toEqual(EXPECTED_M1_BODY)
  expect(new URL(publicationResponse.url()).searchParams.has('project_id')).toBe(false)
  const publication = await publicationResponse.json()
  expect(publication).toMatchObject({ drawing_id: drawingId, operation: 'manual-create', outcome: 'published',
    created: true, summary: { rows: 2, slots: 5, module_power_watts: 450 }, head: { index: 0, parent: null } })
  expect(publication.head.state.artifact_id).toEqual(expect.any(String))
  expect(publications).toHaveLength(1)
  await expect(panel.getByTestId('solar-tracker-rows-status')).toHaveText('Manual tracker rows were published for this drawing.', { timeout: 120000 })
  await expect(panel.locator('dl[aria-label="Published tracker rows"] dd')).toHaveText(['2', '5', '450 W', '0', 'Metres'])
  await head(3)
  await noHorizontalOverflow(seat)
  await screenshot('g6-tracker-rows-published.png')
  await click(panel.getByRole('button', { name: 'Close', exact: true }))
  await select(flow, 'ground-electrical')
  await expect(page.locator('#solar-step-solar-trackers-to-panel-groups')).toBeEnabled({ timeout: 60000 })
  await click(page.locator('#solar-step-solar-trackers-to-panel-groups'))
  const editor = page.locator('#solar-step-editor')
  await expect(editor.getByLabel('Expected rev', { exact: true })).toHaveValue('2', { timeout: 30000 })
  await click(editor.getByRole('button', { name: 'Review & run', exact: true }))
  const converted = await confirmRun('solar-trackers-to-panel-groups')
  expect(converted.params).toEqual({ drawing_id: drawingId, expected_rev: 2 })
  expect(runCount('solar-trackers-to-panel-groups')).toHaveLength(1)
  await head(4)
  const convertedGraph = (await intakeAt(4)).intake.solar_design_graph
  expect(convertedGraph.rev).toBe(3)
  expect(convertedGraph.frames).toHaveLength(2)
  expect(convertedGraph.frames.map((frame) => frame.installation_design)).toEqual(['Ground', 'Ground'])
  expect(convertedGraph.frames.map((frame) => frame.module_slots)).toEqual([3, 2])
  expect(convertedGraph.frames.map((frame) => frame.insertion_point)).toEqual([[0, 3], [4, 5]])
  expect(convertedGraph.frames.map((frame) => frame.module_power_watts)).toEqual([450, 450])
  await noHorizontalOverflow(seat)
  await screenshot('g6-ground-converted.png')
  if (await editor.isVisible()) await click(editor.getByRole('button', { name: 'Cancel', exact: true }).last())
  await click(page.locator('#solar-step-solar-settings'))
  await expect(settings).toHaveAttribute('data-mode', 'edit', { timeout: 30000 })
  await expect(settings.getByText('Graph revision 3', { exact: true })).toBeVisible()
  // A project is valid only with a name, a ZIP and valid (or absent) coordinates (server solar_project.project_validity);
  // the seed leaves the name and ZIP blank, so both are entered here (measured, W5 run 2026-10-03).
  await fill(settings.getByLabel('Project name', { exact: true }), 'Ground walk')
  await fill(settings.getByLabel('ZIP code', { exact: true }), '44224')
  await click(settings.getByRole('button', { name: 'Apply settings', exact: true }))
  const saved = await confirmRun('solar-settings')
  expect(saved.params).toEqual({ drawing_id: drawingId, expected_rev: 3, project_changes: { name: 'Ground walk', zip_code: '44224' } })
  await head(5)
  await expect(settings.getByText('Graph revision 4', { exact: true })).toBeVisible({ timeout: 30000 })
  await expect(settings.getByLabel('ZIP code', { exact: true })).toHaveValue('44224')
  await click(settings.getByRole('button', { name: 'Close', exact: true }))


  const beforeGraph = (await intakeAt(5)).intake.solar_design_graph
  expect(beforeGraph.rev).toBe(4)
  const settingsId = beforeGraph.settings.id
  await expect(page.locator('#solar-step-solar-size-strings')).toBeEnabled({ timeout: 60000 })
  await click(page.locator('#solar-step-solar-size-strings'))
  await expect(editor.getByText('Saved project ZIP: 44224', { exact: true })).toBeVisible({ timeout: 30000 })
  await expect(editor.getByText(settingsId, { exact: true })).toBeVisible()
  for (const [label, value] of Object.entries(SIZING_SELECT)) await select(editor.getByRole('combobox', { name: label, exact: true }), value)
  for (const [label, value] of Object.entries(SIZING_TEXT)) await fill(editor.getByLabel(label, { exact: true }), value)
  await reachable(editor.getByLabel('Use module parameters', { exact: true }))
  await editor.getByLabel('Use module parameters', { exact: true }).setChecked(false)
  await expect(editor.getByLabel('Use module parameters', { exact: true })).not.toBeChecked()
  await noHorizontalOverflow(seat)
  await screenshot('g6-sizing-form.png')
  expect(readReplay().events).toEqual([])
  await expect(editor.getByRole('button', { name: 'Review & run', exact: true })).toBeEnabled({ timeout: 30000 })
  await click(editor.getByRole('button', { name: 'Review & run', exact: true }))
  const sizing = await confirmRun('solar-size-strings')
  const request = {
    module_name: SIZING_TEXT.Module, full_inverter_name: SIZING_TEXT.Inverter,
    bifacial: false, bifacial_coefficient: '.7', racking_params: { racking_type: 'fixed_tilt',
      surface_tilt: '5', surface_azimuth: '180', albedo: '.25' }, max_voltage: '1500',
    thermal_model_type: 'close mount glass glass', open_circuit_rise: false, zip_code: '44224',
  }
  expect(sizing.params).toEqual({ drawing_id: drawingId, expected_rev: 4, mode: 'global',
    requests: { [settingsId]: request }, grant_ref: 'synthetic-sizing-g2', confirm: true })
  const sizingJobId = sizing.body.job_id
  expect(runCount('solar-size-strings')).toHaveLength(1)
  await expect(editor.getByText('String sizing applied.', { exact: true })).toBeVisible({ timeout: 120000 })
  await head(6)
  await screenshot('g6-sizing-applied.png')
  await click(editor.getByRole('button', { name: 'Cancel', exact: true }).last())

  // Arm before reopening and bind to a REQUEST issued after arming: an intake read still in flight from the
  // previous step cannot satisfy it, so the response read below is the reopening's own.
  const freshIntake = page.waitForRequest((request) => request.method() === 'GET'
    && new URL(request.url()).pathname === `/api/drawings/${drawingId}/intake`, { timeout: 60000 })
  await click(page.locator('#solar-step-solar-size-strings'))
  const freshResponse = await (await freshIntake).response()
  expect(freshResponse.status()).toBe(200)
  const freshView = await freshResponse.json()
  expect(freshView.version).toBe(6)
  const sizedGraph = freshView.intake.solar_design_graph
  expect(sizedGraph.parent_rev).toBe(4)
  storedGraphs.push({ version: 6, graph: sizedGraph })
  expect(sizedGraph.rev).toBe(5)
  expect(sizedGraph.settings.id).toBe(settingsId)
  expect(sizedGraph.settings.global_string_sizing_confirmed).toBe(true)
  expect(sizedGraph.settings.panels_in_sequence).toBe(27)
  expect(sizedGraph.settings.extra.string_sizing.mode).toBe('global')
  const records = sizedGraph.settings.extra.string_sizing.records
  expect(Object.keys(records)).toEqual([settingsId])
  expect(records[settingsId].response.pmp).toBe(595)
  expect(records[settingsId].response.simulation_results.standard.string_length).toBe(27)
  expect(sizedGraph.frames.map((frame) => frame.module_power_watts)).toEqual([595, 595])
  expect(sizedGraph.frames.map((frame) => frame.module_slots)).toEqual([3, 2])
  expect(sizedGraph.frames.map((frame) => frame.installation_design)).toEqual(['Ground', 'Ground'])
  await expect(editor.getByText('Confirmed module power: 595 W', { exact: true })).toBeVisible({ timeout: 60000 })
  await screenshot('g6-confirmed-power.png')
  await click(editor.getByRole('button', { name: 'Cancel', exact: true }).last())
  const stringStep = page.locator('#solar-step-solar-string-add')
  await expect(stringStep).toBeEnabled({ timeout: 60000 })
  await expect(stringStep.getByText('Ready', { exact: true })).toBeVisible()
  await reachable(stringStep)
  expect(runCount('solar-string-add')).toHaveLength(0)
  await noHorizontalOverflow(seat)
  await screenshot('g6-string-ready.png')


  // The valid_strings_required gate is visible before any string is created.
  await expect(page.locator('#solar-step-solar-assign-equipment')).toBeDisabled()
  await expect(page.locator('#solar-step-solar-assign-equipment-reason')).toHaveText('Solve valid strings first')
  expect(sizedGraph.strings).toEqual([])
  expect(sizedGraph.panels).toEqual([])
  const sourceHash = sizedGraph.source_hash
  expect(sourceHash).toEqual(expect.any(String))
  expect(sourceHash.length).toBeGreaterThan(0)
  const slotIds = sizedGraph.frames.map((frame, frameIndex) =>
    Array.from({ length: frame.module_slots }, (_, slotIndex) => groundPanelId(sourceHash, frameIndex, slotIndex)))
  expect(slotIds.map((ids) => ids.length)).toEqual([3, 2])
  const orderedPanelRefs = [slotIds[0][2], slotIds[0][1]]
  await click(stringStep)
  const rows = editor.getByRole('region', { name: 'Rows', exact: true })
  await expect(rows).toBeVisible({ timeout: 60000 })
  await expect(rows.getByRole('button', { name: 'Select row 1 Group 1', exact: true })).toBeVisible()
  await expect(rows.getByRole('button', { name: 'Select row 2 Group 2', exact: true })).toBeVisible()
  await click(editor.getByRole('button', { name: 'Select row 1 Group 1', exact: true }))
  await fill(editor.getByLabel('First slot', { exact: true }), '3')
  await fill(editor.getByLabel('Last slot', { exact: true }), '2')
  await click(editor.getByRole('button', { name: 'Add range', exact: true }))
  const queue = editor.getByRole('region', { name: 'Selection queue', exact: true })
  await expect(queue.getByText('1 selections', { exact: true })).toBeVisible()
  await expect(queue.getByText('Group 1 slots 3 to 2', { exact: true })).toBeVisible()
  const composed = editor.getByRole('region', { name: 'Composed strings', exact: true })
  for (const text of ['2 panels', 'Saved maximum: 27', '1 strings', 'String 1: 2 panels']) {
    await expect(composed.getByText(text, { exact: true })).toBeVisible()
  }
  await screenshot('g6-string-selection.png')
  await expect(editor.getByRole('button', { name: 'Review & run', exact: true })).toBeEnabled()
  await click(editor.getByRole('button', { name: 'Review & run', exact: true }))
  const stringAdded = await confirmRun('solar-string-add')
  const stringParams = { drawing_id: drawingId, operation: 'add-string', expected_rev: 5,
    ordered_panel_refs: orderedPanelRefs }
  expect(stringAdded.params).toEqual(stringParams)
  expect(runCount('solar-string-add')).toHaveLength(1)
  expect(runCount('solar-string-add')[0].dwg_version).toBe(6)
  await expect(editor.getByRole('status').filter({ hasText: /^Strings created\.$/ })).toBeVisible({ timeout: 120000 })
  await head(7)
  await screenshot('g6-string-created.png')
  await click(editor.getByRole('button', { name: 'Cancel', exact: true }))

  // Reopen through the rail and bind read-back to a REQUEST issued after arming: the composer's completion reload
  // may still be in flight when it closes, and its response must never stand in for the reopening's read.
  const storedIntake = page.waitForRequest((request) => request.method() === 'GET'
    && new URL(request.url()).pathname === `/api/drawings/${drawingId}/intake`, { timeout: 60000 })
  await click(stringStep)
  const storedResponse = await (await storedIntake).response()
  expect(storedResponse.status()).toBe(200)
  const storedView = await storedResponse.json()
  expect(storedView.version).toBe(7)
  const storedGraph = storedView.intake.solar_design_graph
  storedGraphs.push({ version: 7, graph: storedGraph })
  expect(storedGraph.rev).toBe(6)
  expect(storedGraph.parent_rev).toBe(5)
  expect(storedGraph.source_hash).toBe(sourceHash)
  expect(storedGraph.strings).toHaveLength(1)
  const storedString = storedGraph.strings[0]
  expect(storedString.ordered_panel_refs).toEqual(orderedPanelRefs)
  expect(storedString.module_count).toBe(2)
  expect(storedString.from_ref).toBe(orderedPanelRefs[0])
  expect(storedString.to_ref).toBe(orderedPanelRefs[1])
  expect(storedString.rev).toBe(6)
  expect(storedString.validity.state).toBe('valid')
  expect(storedString.circuit_tag).toBe('S1')
  expect(storedString.route).toHaveLength(2)
  for (const [index, point] of [[0, [0, 5]], [1, [0, 3]]]) {
    expect(storedString.route[index]).toHaveLength(2)
    for (let axis = 0; axis < 2; axis += 1) {
      expect(Math.abs(storedString.route[index][axis] - point[axis])).toBeLessThanOrEqual(1e-9)
    }
  }
  expect(Math.abs(storedString.length_ft - 2 / 0.3048)).toBeLessThanOrEqual(1e-6)
  expect(storedGraph.panels).toEqual([])
  expect(storedGraph.frames).toHaveLength(2)
  expect(storedGraph.frames.map((frame) => frame.installation_design)).toEqual(['Ground', 'Ground'])
  expect(storedGraph.frames.map((frame) => frame.module_slots)).toEqual([3, 2])
  expect(storedGraph.frames.map((frame) => frame.module_power_watts)).toEqual([595, 595])
  await expect(rows).toBeVisible({ timeout: 60000 })
  await click(editor.getByRole('button', { name: 'Cancel', exact: true }))

  const additionalStrings = []
  for (const [index, rowName, first, last, refs] of [
    [0, 'Select row 1 Group 1', 1, 1, [slotIds[0][0]]],
    [1, 'Select row 2 Group 2', 1, 2, slotIds[1]],
  ]) {
    await click(stringStep)
    await expect(rows).toBeVisible({ timeout: 60000 })
    await click(editor.getByRole('button', { name: rowName, exact: true }))
    await fill(editor.getByLabel('First slot', { exact: true }), first)
    await fill(editor.getByLabel('Last slot', { exact: true }), last)
    await click(editor.getByRole('button', { name: 'Add range', exact: true }))
    await click(editor.getByRole('button', { name: 'Review & run', exact: true }))
    const added = await confirmRun('solar-string-add')
    expect(added.params).toEqual({ drawing_id: drawingId, operation: 'add-string',
      expected_rev: 6 + index, ordered_panel_refs: refs })
    await expect(editor.getByRole('status').filter({ hasText: /^Strings created\.$/ })).toBeVisible({ timeout: 120000 })
    await head(8 + index)
    await click(editor.getByRole('button', { name: 'Cancel', exact: true }))
    const view = await reopenGraph('solar-string-add', 8 + index, 7 + index)
    additionalStrings.push(view.intake.solar_design_graph)
    await expect(rows).toBeVisible({ timeout: 60000 })
    await click(editor.getByRole('button', { name: 'Cancel', exact: true }))
  }
  const coverageGraph = additionalStrings[1]
  expect(coverageGraph.strings).toHaveLength(3)
  expect(coverageGraph.strings.map((string) => string.module_count)).toEqual([2, 1, 2])
  expect(coverageGraph.strings.map((string) => string.ordered_panel_refs))
    .toEqual([orderedPanelRefs, [slotIds[0][0]], slotIds[1]])
  for (const string of coverageGraph.strings) expect(string.validity.state).toBe('valid')
  const assignedRefs = coverageGraph.strings.flatMap((string) => string.ordered_panel_refs)
  const coverage = {
    duplicate_panel_refs: assignedRefs.filter((ref, index) => assignedRefs.indexOf(ref) !== index).sort(),
    unassigned_panel_refs: slotIds.flat().filter((ref) => !assignedRefs.includes(ref)).sort(),
  }
  expect(coverage).toEqual({ duplicate_panel_refs: [], unassigned_panel_refs: [] })
  await noHorizontalOverflow(seat)
  await screenshot('g6-coverage.png')

  const I = 'leaf:inverter:00000000-0000-4000-8000-0000000000e1'
  const E = { id: I, number: 1, type_key: 'A', model: 'fixture-inverter',
    position: [5, 0, 0], rotation: 0, scale: [1, 1, 1], block_name: 'FixtureInverter', layer: '0',
    mppt_count: 1, total_dc_inputs: 3, mppt_inputs: { A: 3 }, max_dc_voltage: 1500,
    max_ac_power_kw: 5, max_dc_power_kw: 5, is_solaredge: false }
  const A = coverageGraph.strings.map((string, index) => ({
    string_ref: string.id, inverter_ref: I, mppt_letter: 'A', input_number: index,
  }))
  const openGeneric = async (tool, rev) => {
    const step = page.locator(`#solar-step-${tool}`)
    await expect(step).toBeEnabled({ timeout: 60000 })
    await click(step)
    await expect(editor.getByLabel('Expected rev', { exact: true })).toBeVisible({ timeout: 60000 })
    await fill(editor.getByLabel('Expected rev', { exact: true }), rev)
  }
  const runGeneric = async (tool, params, version, rev) => {
    await noHorizontalOverflow(seat)
    await click(editor.getByRole('button', { name: 'Review & run', exact: true }))
    const result = await confirmRun(tool)
    expect(result.params).toEqual(params)
    await head(version)
    await click(editor.getByRole('button', { name: 'Cancel', exact: true }))
    const view = await reopenGraph(tool, version, rev)
    await expect(editor.getByLabel('Expected rev', { exact: true })).toBeVisible({ timeout: 60000 })
    await click(editor.getByRole('button', { name: 'Cancel', exact: true }))
    return { result, graph: view.intake.solar_design_graph }
  }
  await openGeneric('solar-assign-equipment', 8)
  await fill(editor.getByLabel('Equipment', { exact: true }), JSON.stringify([E]))
  await fill(editor.getByLabel('Assignments', { exact: true }), JSON.stringify(A))
  const equipment = await runGeneric('solar-assign-equipment',
    { drawing_id: drawingId, expected_rev: 8, equipment: [E], assignments: A }, 10, 9)
  expect(equipment.graph.inverters).toHaveLength(1)
  const l1 = equipment.graph.inverters[0]
  expect(Object.fromEntries(Object.keys(E).map((key) => [key, l1[key]]))).toEqual(E)
  expect(l1.is_l2).toBe(false)
  expect(l1.validity.state).toBe('valid')
  expect(l1.input_assignments).toEqual(A.map(({ inverter_ref, ...assignment }) => assignment))
  expect(equipment.graph.extra.equipment.assignment_requests).toEqual(A)
  for (const string of equipment.graph.strings) {
    expect(string.validity.state).toBe('valid')
    expect(string.inverter_ref).toBe(I)
  }
  await screenshot('g6-equipment.png')

  await click(page.locator('#solar-step-solar-settings'))
  await expect(settings.getByText('Graph revision 9', { exact: true })).toBeVisible({ timeout: 60000 })
  const l2Control = settings.getByLabel('L2 collectors', { exact: true })
  await reachable(l2Control)
  await l2Control.setChecked(true)
  await click(settings.getByRole('button', { name: 'Apply settings', exact: true }))
  const l2Run = await confirmRun('solar-settings')
  expect(l2Run.params).toEqual({ drawing_id: drawingId, expected_rev: 9, changes: { use_l2_collectors: true } })
  await head(11)
  await expect(settings.getByText('Graph revision 10', { exact: true })).toBeVisible({ timeout: 60000 })
  await click(settings.getByRole('button', { name: 'Close', exact: true }))
  const l2Graph = (await reopenGraph('solar-settings', 11, 10)).intake.solar_design_graph
  expect(l2Graph.settings.use_l2_collectors).toBe(true)
  expect(l2Graph.settings.global_string_sizing_confirmed).toBe(false)
  expect(l2Graph.settings.extra).not.toHaveProperty('string_sizing')
  expect(l2Graph.inverters).toHaveLength(1)
  expect(l2Graph.inverters[0].equipment_type).toBe('string_inverter')
  expect(l2Graph.inverters[0].l2_ref).toBeNull()
  await expect(settings.getByLabel('L2 collectors', { exact: true })).toBeChecked({ timeout: 60000 })
  await screenshot('g6-l2.png')
  await click(settings.getByRole('button', { name: 'Close', exact: true }))

  const H = { model: 'Central 100', max_dc_voltage: 1500, max_ac_power_kw: 100,
    mppt_count: 1, total_dc_inputs: 1, collector_capacity: 2 }
  await openGeneric('solar-central-inverter-add', 10)
  await fill(editor.getByLabel('Number', { exact: true }), 1)
  await fill(editor.getByLabel('Point', { exact: true }), JSON.stringify([2, 12]))
  await fill(editor.getByLabel('Hardware', { exact: true }), JSON.stringify(H))
  const central = await runGeneric('solar-central-inverter-add',
    { drawing_id: drawingId, expected_rev: 10, number: 1, point: [2, 12], hardware: H }, 12, 11)
  expect(central.graph.inverters).toHaveLength(2)
  for (const inverter of central.graph.inverters) expect(inverter.validity.state).toBe('valid')
  const centralInverter = central.graph.inverters.find((inverter) => inverter.is_l2)
  expect(centralInverter.id).toEqual(expect.any(String))
  expect(centralInverter.number).toBe(1)
  expect(centralInverter.is_l2).toBe(true)
  expect(centralInverter.position).toEqual([2, 12])
  expect(Object.fromEntries(Object.keys(H).map((key) => [key, centralInverter[key]]))).toEqual(H)
  expect(centralInverter.input_assignments).toEqual([])
  expect(centralInverter.l1_assignments).toEqual([])
  await screenshot('g6-central.png')

  await openGeneric('solar-feeders', 11)
  const feeders = await runGeneric('solar-feeders', { drawing_id: drawingId, expected_rev: 11 }, 13, 12)
  const feederRoutes = feeders.graph.routes.filter((route) => route.route_kind === 'feeder')
  expect(feederRoutes).toHaveLength(1)
  const feeder = feederRoutes[0]
  expect(feeder.from_ref).toBe(I)
  expect(feeder.to_ref).toBe(centralInverter.id)
  expect(feeders.graph.inverters.find((inverter) => inverter.id === I).l2_ref).toBe(centralInverter.id)
  expect(feeder.points.length).toBeGreaterThanOrEqual(2)
  for (const point of feeder.points) {
    expect(point).toHaveLength(2)
    for (const coordinate of point) expect(typeof coordinate === 'number' && Number.isFinite(coordinate)).toBe(true)
  }
  expect(feeder.points[0]).toEqual(E.position.slice(0, 2))
  expect(feeder.points.at(-1)).toEqual(centralInverter.position)
  const measuredLengthFt = feeder.points.slice(1).reduce((total, point, index) =>
    total + Math.hypot(point[0] - feeder.points[index][0], point[1] - feeder.points[index][1]), 0) / 0.3048
  expect(Number.isFinite(feeder.length_ft)).toBe(true)
  expect(Math.abs(feeder.length_ft - measuredLengthFt)).toBeLessThanOrEqual(measuredLengthFt * 1e-9)
  await noHorizontalOverflow(seat)
  await screenshot('g6-feeders.png')

  const toolSequence = runs.map((run) => run.tool)
  expect(toolSequence).toEqual(['solar-settings', 'solar-design-presets', 'solar-trackers-to-panel-groups',
    'solar-settings', 'solar-size-strings', 'solar-string-add', 'solar-string-add', 'solar-string-add',
    'solar-assign-equipment', 'solar-settings', 'solar-central-inverter-add', 'solar-feeders'])
  expect(runs).toHaveLength(12)
  expect(completedRuns).toHaveLength(12)
  expect(runCount('solar-string-add')).toHaveLength(3)
  expect(runCount('solar-settings')).toHaveLength(3)
  for (const tool of ['solar-design-presets', 'solar-trackers-to-panel-groups', 'solar-size-strings',
    'solar-assign-equipment', 'solar-central-inverter-add', 'solar-feeders']) expect(runCount(tool)).toHaveLength(1)
  const replayAfter = readReplay()
  expect(replayAfter.endpoint).toBe(replayBefore.endpoint)
  expect(replayAfter.pid).toBe(replayBefore.pid)
  expect(replayAfter.events).toEqual([{ method: 'POST', path: '/string-length', status: 200,
    request_sha256: REQUEST_SHA, fixture_id: 'w1-string-length-recorded',
    response_sha256: fixture.response_wire_sha256 }])
  expect(uploads).toHaveLength(1)
  expect(publications).toHaveLength(1)
  expect(observationErrors).toEqual([])
  return { feeders, seat, editor, flow, sourceHash, slotIds, coverage, E, A, H,
    centralInverter, feeder, measuredLengthFt, publication, sizingJobId, replayAfter }
  })
  console.log('PASS G6-1 ground chain reaches feeders')
  const { feeders, seat, editor, flow, sourceHash, slotIds, coverage, E, A, H,
    centralInverter, feeder, measuredLengthFt, publication, sizingJobId, replayAfter } = chain
  const feederGraph = feeders.graph
  const readResults = []
  const calculations = []
  const downloads = []
  const root = page.getByTestId('solar-read-result')
  const section = (name) => root.getByRole('region', { name, exact: true })
  const field = async (scope, label, text) => {
    const row = scope.getByRole('row').filter({ has: page.getByRole('rowheader', { name: label, exact: true }) })
    await expect(row).toHaveCount(1)
    await expect(row).toBeVisible()
    await expect(row.getByRole('cell')).toHaveText(String(text))
  }
  const fields = async (scope, values) => {
    for (const [label, text] of Object.entries(values)) await field(scope, label, text)
  }
  const table = async (name, columns, expectedRows) => {
    const scope = section(name)
    await expect(scope).toBeVisible()
    await expect(scope.getByRole('columnheader')).toHaveText(columns)
    const rows = scope.locator('tbody > tr')
    await expect(rows).toHaveCount(expectedRows.length)
    for (let index = 0; index < expectedRows.length; index += 1) {
      await expect(rows.nth(index).getByRole('cell')).toHaveText(expectedRows[index])
    }
  }
  const visibleText = (value) => value == null ? 'none'
    : typeof value === 'boolean' ? (value ? 'yes' : 'no')
    : typeof value === 'number' ? String(Number(value.toPrecision(6)))
    : Array.isArray(value) ? (value.length ? value.map(visibleText).join(', ') : 'none')
    : String(value)
  const read = async (tool, operands) => {
    const params = { drawing_id: drawingId, ...operands }
    const step = page.locator(`#solar-step-${tool}`)
    await expect(step).toBeEnabled({ timeout: 60000 })
    await click(step)
    await expect(editor.getByLabel('Drawing id', { exact: true })).toBeVisible()
    for (const [key, value] of Object.entries(params)) {
      const label = key.replaceAll('_', ' ')
      const control = editor.getByLabel(label.charAt(0).toUpperCase() + label.slice(1), { exact: true })
      if (typeof value === 'boolean') {
        await reachable(control)
        await control.setChecked(value)
        await expect(control).toBeChecked()
      } else await fill(control, Array.isArray(value) ? JSON.stringify(value) : value)
    }
    await expect(editor.getByLabel('Expected rev', { exact: true })).toHaveCount(0)
    await noHorizontalOverflow(seat)
    await click(editor.getByRole('button', { name: 'Review & run', exact: true }))
    const entry = await confirmRun(tool)
    expect(entry.params).toEqual(params)
    expect(entry.dwg_version).toBe(13)
    expect(entry.job.job_id).toBe(entry.body.job_id)
    expect(entry.job.status).toBe('complete')
    expect(entry.job.result.ok).toBe(true)
    const data = entry.job.result.result
    expect(data).toMatchObject({ schema_version: 'leaf.solar-graph-read.v1',
      adapter: 'local-graph-read', tool, drawing_id: drawingId,
      job_id: entry.body.job_id, source_version: 13, drawing_changed: false })
    for (const key of ['graph_sha256', 'output_sha256', 'request_sha256']) expect(data[key]).toMatch(/^[0-9a-f]{64}$/)
    if (readResults.length) expect(data.graph_sha256).toBe(readResults[0].graph_sha256)
    readResults.push(data)
    await click(editor.getByRole('button', { name: 'Cancel', exact: true }))
    await expect(root).toHaveAttribute('data-tool', tool)
    const details = root.locator('details')
    await click(details.getByText('Read details', { exact: true }))
    await fields(details, { Tool: tool, Drawing: drawingId, Version: '13',
      Representation: data.representation, Graph: data.graph_sha256, Output: data.output_sha256,
      Request: data.request_sha256, Job: entry.body.job_id })
    await click(details.getByText('Read details', { exact: true }))
    return data
  }
  const downloadArtifact = async (data, filename, mediaType) => {
    const ref = data.output.artifact
    expect(data.output.summary.status).toBe('written')
    expect(ref).toMatchObject({ filename, media_type: mediaType })
    expect(ref.artifact_id).toEqual(expect.any(String))
    expect(ref.content_sha256).toMatch(/^[0-9a-f]{64}$/)
    expect(Number.isInteger(ref.byte_length) && ref.byte_length > 0).toBe(true)
    expect(ref.download).toBe(`/api/drawings/${drawingId}/artifacts/${ref.artifact_id}`)
    await expect(root).toHaveAttribute('data-tool', data.tool)
    const artifactRequest = page.waitForRequest(request => request.method() === 'GET'
      && new URL(request.url()).pathname === ref.download, { timeout: 60000 })
    // The page's default timeout bounds this event wait too.
    const downloadEvent = page.waitForEvent('download')
    await click(root.getByTestId('solar-read-download'))
    const [request, download] = await Promise.all([artifactRequest, downloadEvent])
    const response = await request.response()
    expect(response.status()).toBe(200)
    expect(response.headers()['x-leaf-artifact-id']).toBe(ref.artifact_id)
    expect(download.suggestedFilename()).toBe(ref.filename)
    const savedPath = testInfo.outputPath(ref.filename)
    await download.saveAs(savedPath)
    expect(await download.failure()).toBeNull()
    const bytes = readFileSync(savedPath)
    const sha256 = createHash('sha256').update(bytes).digest('hex')
    expect(bytes.length).toBe(ref.byte_length)
    expect(sha256).toBe(ref.content_sha256)
    await expect(root.getByTestId('solar-read-download-status')).toHaveText(`Downloaded ${ref.filename}.`)
    downloads.push({ tool: data.tool, ref, saved_path: savedPath, byte_length: bytes.length, sha256 })
    return bytes
  }
  const summary = async (data, expected) => {
    expect(data.output.summary).toMatchObject({ status: 'written', ...expected })
    const values = Object.fromEntries(Object.entries(data.output.summary).map(([key, value]) => {
      const label = key.replaceAll('_', ' ')
      return [label.charAt(0).toUpperCase() + label.slice(1), visibleText(value)]
    }))
    await fields(section('Summary'), values)
  }

  await test.step('G6-2 ampacity value reaches the drafter', async () => {
    const data = await read('solar-nec-ampacity-correction',
      { base_ampacity: 100, temp_factor: 0.82, conduit_factor: 0.7 })
    expect(data.output.result).toBe(57.4)
    await expect(root.getByTestId('solar-read-headline')).toHaveText('57.4 A')
    await field(root, 'Article', 'NEC 310.16')
    await fields(section('Inputs'), { 'I base': '100', 'Conduit factor': '0.7', 'Temp factor': '0.82' })
    expect(data.output.rejected_alternatives).toEqual(AMPACITY_REJECTED)
    // The view lists a row's keys in the order the server sent them, as measured.
    await table('Rejected alternatives', ['Description', 'Would have resulted in', 'Why rejected'],
      [['I_base with no temperature correction', '70', 'Required when ambient exceeds 30\u00b0C.']])
    calculations.push(data.output)
    await screenshot('g6-ampacity.png')
  })
  console.log('PASS G6-2 ampacity value reaches the drafter')

  await test.step('G6-3 AC voltage drop reaches the drafter', async () => {
    const data = await read('solar-nec-ac-voltage-drop', { current_a: 10, one_way_length_ft: 100,
      r_ohm_per_1000ft: 0.5, x_ohm_per_1000ft: 0.1, power_factor: 1, phase: 1, source_voltage: 240 })
    expect(data.output.result).toBe(0.4166666666666667)
    await expect(root.getByTestId('solar-read-headline')).toHaveText('0.416667 % (3% recommended max)')
    await field(root, 'Rejected alternatives', 'none')
    await fields(section('Inputs'), { I: '10', 'L ft': '100', 'R ohm per 1000ft': '0.5',
      'V source': '240', 'X ohm per 1000ft': '0.1', Phase: '1', PowerFactor: '1' })
    calculations.push(data.output)
    await screenshot('g6-ac-voltage-drop.png')
  })
  console.log('PASS G6-3 AC voltage drop reaches the drafter')

  await test.step('G6-4 conduit fill reaches the drafter', async () => {
    const data = await read('solar-nec-conduit-fill', { conductor_gauge: '10 AWG',
      current_carrying_count: 2, egc_gauge: '10 AWG', conduit_type: 'PvcSch40', conductor_insulation: 'THWN2' })
    await expect(root.getByTestId('solar-read-headline')).toHaveCount(0)
    await fields(root, { Success: 'yes', 'Trade size': '1/2', 'Conduit type label': 'PVC Sch 40',
      'Fill pct': '18.5263', 'Max fill pct': '40', 'Total conductors': '3', 'Failure reason': 'none' })
    expect(data.output.conductors).toEqual(CONDUCTORS)
    expect(data.output.conduit_table).toEqual(CONDUIT_TABLE)
    await table('Conductors', ['Role', 'Gauge', 'Unit', 'Insulation', 'Count', 'Area sq in'], CONDUCTOR_ROWS)
    await table('Conduit table', ['Trade size', 'Area sq in'], CONDUIT_ROWS)
    calculations.push(data.output)
    await screenshot('g6-conduit-fill.png')
  })
  console.log('PASS G6-4 conduit fill reaches the drafter')

  await test.step('G6-5 feeder OCPD reaches the drafter', async () => {
    const data = await read('solar-nec-feeder-ocpd',
      { continuous_current_a: 20, is_continuous: true, egc_material: 'Copper' })
    await expect(root.getByTestId('solar-read-headline')).toHaveCount(0)
    await fields(root, { 'Min ocpd a': '25', 'Ocpd rating a': '25', 'Egc gauge': '10 AWG',
      'Is continuous': 'yes', 'Continuous current a': '20' })
    calculations.push(data.output)
    await screenshot('g6-feeder-ocpd.png')
  })
  console.log('PASS G6-5 feeder OCPD reaches the drafter')

  const graphCounts = { strings: feederGraph.strings.length,
    modules: feederGraph.strings.reduce((sum, string) => sum + string.module_count, 0) }
  await test.step('G6-6 cable export downloads verified bytes', async () => {
    const data = await read('solar-cable-export', { circuit_source: 'topology' })
    await summary(data, { ...graphCounts, feeders: feederGraph.routes.filter(route => route.route_kind === 'feeder').length,
      circuit_source: 'topology', sizing: 'absent', inverter_record: 'absent', module_catalog: 'unresolved' })
    expect(data.output.summary.sheets).toEqual(CABLE_SHEETS)
    expect(data.output.summary.rows).toEqual(CABLE_SHEET_ROWS)
    const bytes = await downloadArtifact(data, 'CableExport.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    const parts = workbookParts(bytes)
    expect([...parts.keys()].sort()).toEqual(['[Content_Types].xml', '_rels/.rels', 'xl/_rels/workbook.xml.rels',
      'xl/workbook.xml', ...CABLE_SHEETS.map((_, index) => `xl/worksheets/sheet${index + 1}.xml`)].sort())
    expect(parts.get('[Content_Types].xml')).toContain('spreadsheetml.sheet.main+xml')
    expect([...parts.get('xl/workbook.xml').matchAll(/<sheet name="([^"]*)"/g)].map(match => match[1])).toEqual(CABLE_SHEETS)
    const sheetRows = CABLE_SHEETS.map((_, index) => [...parts.get(`xl/worksheets/sheet${index + 1}.xml`)
      .matchAll(/<row r="\d+">(.*?)<\/row>/g)].map(match =>
        [...match[1].matchAll(/<t xml:space="preserve">([^<]*)<\/t>/g)].map(cell => cell[1])))
    expect(sheetRows.map(rows => rows.length)).toEqual(CABLE_SHEET_ROWS)
    expect(sheetRows[0][0]).toEqual(HOMERUN_HEADERS)
    expect(sheetRows[3][0]).toEqual(FEEDER_HEADERS)
    await screenshot('g6-cable-export.png')
  })
  console.log('PASS G6-6 cable export downloads verified bytes')

  await test.step('G6-7 electrical schedules download verified bytes', async () => {
    const data = await read('solar-electrical-schedules', { circuit_source: 'topology' })
    await summary(data, { ...graphCounts, circuit_source: 'topology', sizing: 'absent',
      inverter_record: 'absent', module_catalog: 'unresolved', tables: SCHEDULE_TABLES })
    const bytes = await downloadArtifact(data, 'ElectricalSchedules.json', 'application/json')
    const document = JSON.parse(bytes.toString('utf8'))
    expect(document.schema).toBe('leaf.solar-electrical-schedules.v1')
    expect(document.circuit_source).toBe('topology')
    expect(document.tables.map(item => item.title)).toEqual(data.output.summary.tables)
    // Every row is checked against the graph the drafter built, so a schedule with titles and no data,
    // or with another design's numbers, cannot pass. Local stack: no module or inverter record, so the
    // electrical columns read '-'.
    expect(feederGraph.inverters.map(inverter => inverter.is_l2).sort()).toEqual([false, true])
    const l1 = feederGraph.inverters.find(inverter => !inverter.is_l2)
    const l2 = feederGraph.inverters.find(inverter => inverter.is_l2)
    const ordered = [...feederGraph.strings].sort((a, b) => tagNumber(a) - tagNumber(b))
    const moduleCounts = ordered.map(string => string.module_count)
    const totalModules = moduleCounts.reduce((sum, count) => sum + count, 0)
    const perString = new Set(moduleCounts).size === 1 ? String(moduleCounts[0]) : moduleCounts.join(', ')
    const feederRoutes = feederGraph.routes.filter(route => route.route_kind === 'feeder')
    expect(feederRoutes.length).toBeGreaterThan(0)
    const titleRow = (title, width) => [title, ...Array(width - 1).fill('')]
    const [combinerTable, stringTable, feederTable] = document.tables
    expect(document.tables.map(item => item.cells.length)).toEqual([5, ordered.length + 3, feederRoutes.length + 2])
    expect(combinerTable.cells).toEqual([titleRow(SCHEDULE_TABLES[0], 8), COMBINER_SCHEDULE_HEADERS,
      [`INV-${l2.number}`, `CB-${l1.number}`, String(ordered.length), perString, String(totalModules), '-', '-', '-'],
      [`INV-${l2.number} Total`, '', String(ordered.length), '', String(totalModules), '-', '-', '-'],
      ['GRAND TOTAL', '', String(ordered.length), '', String(totalModules), '-', '-', '-']])
    expect(stringTable.cells.slice(0, 2)).toEqual([titleRow(SCHEDULE_TABLES[1], 18), STRING_SCHEDULE_HEADERS])
    ordered.forEach((string, index) => {
      const row = stringTable.cells[2 + index]
      expect(row).toHaveLength(18)
      expect(row.slice(0, 13)).toEqual([String(l1.number), String(l2.number), String(tagNumber(string)), '-',
        String(string.module_count), '-', '-', '-', '-', '-', '-', '-', '-'])
      expectFeet(row[13], string.length_ft)
      expect(row.slice(14)).toEqual(['-', '-', '-', '-'])
    })
    expect(stringTable.cells.at(-1)).toEqual(['TOTAL', '', String(ordered.length), '', String(totalModules),
      '', '', '', '', '', '', '', '-', '', '', '', '', ''])
    expect(feederTable.cells.slice(0, 2)).toEqual([titleRow(SCHEDULE_TABLES[2], 3), FEEDER_HEADERS])
    feederRoutes.forEach((route, index) => {
      const row = feederTable.cells[2 + index]
      expect(row).toHaveLength(3)
      expect(row.slice(0, 2)).toEqual([String(l1.number), String(l2.number)])
      expectFeet(row[2], route.length_ft)
    })
    await screenshot('g6-electrical-schedules.png')
  })
  console.log('PASS G6-7 electrical schedules download verified bytes')

  await test.step('G6-8 string data downloads verified bytes', async () => {
    const stringRefs = feederGraph.strings.map(string => string.id)
    const stringInverter = feederGraph.inverters.find(inverter => !inverter.is_l2)
    const data = await read('solar-string-data', { string_refs: stringRefs })
    await summary(data, { selected_strings: stringRefs.length, groups: feederGraph.frames.length,
      grouped_strings: stringRefs.length })
    const bytes = await downloadArtifact(data, 'StringData.json', 'application/json')
    const document = JSON.parse(bytes.toString('utf8'))
    expect(document.groups).toHaveLength(feederGraph.frames.length)
    expect(document.groups.map(group => group.handle).sort()).toEqual(feederGraph.frames.map(frame => frame.id).sort())
    const savedStrings = document.groups.flatMap(group => group.strings)
    expect(savedStrings.map(string => string.handle).sort()).toEqual([...stringRefs].sort())
    for (const group of document.groups) {
      const frame = feederGraph.frames.find(frame => frame.id === group.handle)
      expect(group.name).toBe(frame.name)
      const refs = slotIds[feederGraph.frames.indexOf(frame)]
      for (const row of group.strings) {
        const string = feederGraph.strings.find(string => string.id === row.handle)
        expect(row.startPoint.handle).toBe(string.from_ref)
        expect(row.endPoint.handle).toBe(string.to_ref)
        expect(refs).toContain(row.startPoint.handle)
        // Equipment repoints every string's to_ref at its inverter, so the end point is the inverter.
        expect(row.endPoint.handle).toBe(stringInverter.id)
        expect(row.startPoint.coordinate).toEqual(expect.any(String))
        expect(row.endPoint.coordinate).toEqual(expect.any(String))
      }
    }
    await screenshot('g6-string-data.png')
  })
  console.log('PASS G6-8 string data downloads verified bytes')

  await test.step('G6-9 unchanged graph and proof receipt', async () => {
    const finalView = await reopenGraph('solar-feeders', 13, 12)
    await expect(editor.getByLabel('Expected rev', { exact: true })).toBeVisible()
    await click(editor.getByRole('button', { name: 'Cancel', exact: true }))
    expect(finalView.intake.solar_design_graph).toEqual(feederGraph)
    await head(13)
    expect(readResults).toHaveLength(7)
    expect(new Set(readResults.map(data => data.graph_sha256)).size).toBe(1)
    expect(runs).toHaveLength(19)
    expect(completedRuns).toHaveLength(19)
    expect(calculations).toHaveLength(4)
    expect(downloads).toHaveLength(3)
    expect(observationErrors).toEqual([])
    await noHorizontalOverflow(seat)
    await screenshot('g6-final.png')
    const finalUrl = new URL(page.url())
    writeProofReceipt(join(PROOF_DIR, 'sf-w3-ground-outputs-walk-receipt.json'), {
      capability_ids: ['ID-04'], evidence_tier: 'local-e2e', route: finalUrl.pathname + finalUrl.search,
      runtime: 'real managed local Vite, FastAPI, upload extraction, checkout, broker/job lifecycle and version stores; account mode; synthetic String Sizer replay',
      source_commit: health.source_sha, api_endpoints: observed,
      artifacts: [...screenshots, ...downloads.map(item => item.saved_path), replayPath],
      assertions: ['independent UI Ground chain reaches v13 / rev12 with exact equipment and measured feeder',
        'four entered NEC calculations show their measured field, section and table values',
        'seven exact read requests correlate with completed jobs and visible Read details',
        'three written exports show graph-derived summaries and browser downloads match artifact lengths and content SHA-256',
        'downloaded string data preserves frame, string and endpoint identities',
        'final browser intake equals the pre-read graph and head stays v13 / rev12'],
      result: { verdict: 'pass', record_id: 'sf-w3-ground-outputs-walk', entry_route: '/app?surface=browser',
        served_sha: health.source_sha, source_sha: process.env.LEAF_SOURCE_COMMIT, source_hash: sourceHash,
        managed_run_id: runId, drawing_id: drawingId, tenant_kind: 'account', tenant_id: 'demo-tenant',
        viewport: { width: 1280, height: 800 }, head_before: 13, head_after: 13,
        revision_before: 12, revision_after: 12, parent_revision: 11,
        requests: runs, completed_runs: completedRuns, stored_graphs: storedGraphs,
        tool_sequence: runs.map(run => run.tool), before_graph: feederGraph,
        action_sequence: ['upload distinctive-panel.dxf', 'Solar CAD', 'take edit lock',
          'initialize metres and three panels in sequence', 'create 60-setting Ground preset',
          'publish two tracker rows', 'convert tracker rows', 'save project name and ZIP',
          'run synthetic sizing replay', 'compose descending row-1 range', 'finish string coverage',
          'assign equipment', 'enable L2', 'place central inverter', 'run feeders',
          ...readResults.map(data => data.tool), 'reopen feeders and cancel', 'write proof receipt'],
        tracker_publication_request: EXPECTED_M1_BODY, tracker_publication: publication,
        after_graph: finalView.intake.solar_design_graph, calculations, read_results: readResults,
        downloads, screenshots, slot_ids: slotIds, coverage, equipment: E, assignments: A,
        central_hardware: H, central_inverter: centralInverter, feeder,
        measured_feeder_points: feeder.points, measured_feeder_length_ft: measuredLengthFt,
        physical_head_index: 0, physical_artifact: publication.head.state.artifact_id,
        sizing_job_id: sizingJobId, replay: replayAfter, replay_artifact: replayPath },
      limitations: [REPLAY_LABEL, 'Synthetic fixture; no live-service or engineering proof.',
        'NEC operands are entered examples; catalogs remain unresolved. No electrical compliance claim.',
        'Equipment and feeders are local graph commits; licensed AutoCAD placement is outside this proof.',
        'Sizing evidence is bound to this replay endpoint and cannot reverify under another port.',
        'Account mode uses LEAF_AUTH_LIVE=0; APS is outside this proof.'],
    })
  })
  console.log('PASS G6-9 unchanged graph and proof receipt')
})
