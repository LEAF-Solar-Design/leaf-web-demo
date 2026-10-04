import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
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

test('W1-12 solar lifecycle history reopen and profile continuity through the browser', async ({ page }, testInfo) => {
  test.setTimeout(900_000)
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
  const healthResponse = await page.request.get(`${API_BASE}/api/health`, { timeout: 15000 })
  expect(healthResponse.status()).toBe(200)
  const health = await healthResponse.json()
  expect(health.source_sha).toMatch(/^[0-9a-f]{40}$/)
  expect(process.env.LEAF_SOURCE_COMMIT).toMatch(/^[0-9a-f]{40}$/)
  expect(health.source_sha).toBe(process.env.LEAF_SOURCE_COMMIT)
  expect(health.aps_live).toBe(false)

  // Only browser-originated reads populate these observations; no test readback requests.
  const observed = ['GET /api/health 200']
  const uploads = []
  const publications = []
  const runs = []
  const intakes = []
  const jobs = []
  const observationErrors = []
  const observationPromises = []
  const responseBodies = new WeakMap()
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
    if (response.request().method() === 'GET' && response.status() === 200 &&
        (path.endsWith('/intake') || path.endsWith('/versions') || path === '/api/session'
          || path === '/api/jobs' || /^\/api\/jobs\/[^/]+$/.test(path))) {
      const body = response.json()
      responseBodies.set(response.request(), body)
      observationPromises.push(body.catch((error) => observationErrors.push(error.message)))
    }
    const sanitized = path.replace(/\/api\/drawings\/[^/]+(?=\/|$)/, '/api/drawings/{id}')
      .replace(/\/api\/jobs\/[^/]+(?=\/|$)/, '/api/jobs/{id}')
    observed.push(`${response.request().method()} ${sanitized} ${response.status()}`)
    if (response.request().method() !== 'GET' || response.status() !== 200) return
    const sink = path === `/api/drawings/${drawingId}/intake` ? intakes
      : /^\/api\/jobs\/[^/]+$/.test(path) ? jobs : null
    if (sink) observationPromises.push(responseBodies.get(response.request()).then((body) => sink.push(body)).catch((error) => observationErrors.push(error.message)))
  })
  const runCount = (tool) => runs.filter((run) => run.tool === tool)
  const confirmRun = async (tool) => {
    const responseWait = page.waitForResponse((response) => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/run'
      && response.request().postDataJSON()?.tool === tool, { timeout: 120000 })
    const button = page.getByRole('button', { name: `Run ${tool}`, exact: true })
    await expect(button).toBeEnabled({ timeout: 60000 })
    await click(button)
    const response = await responseWait
    expect(response.status()).toBe(202)
    const body = await response.json()
    expect(body.job_id).toEqual(expect.any(String))
    await expect.poll(() => jobs.findLast((job) => job.job_id === body.job_id
      && ['complete', 'failed'].includes(job.status))?.status ?? null, { timeout: 120000 })
      .toBe('complete')
    return { params: response.request().postDataJSON().params, body }
  }
  const head = async (version) => {
    const document = `${drawingId}-v${version}.dxf`
    await expect(page.locator('.workspace-card[data-engine-document]')).toHaveAttribute('data-engine-document', document, { timeout: 60000 })
    await expect(page.getByTestId('dock-drawing').locator('dt').filter({ hasText: /^Name$/ }).locator('+ dd')).toHaveText(document, { timeout: 60000 })
  }
  const intakeAt = async (version) => {
    await expect.poll(() => intakes.some((view) => view.version === version), { timeout: 120000 }).toBe(true)
    return intakes.findLast((view) => view.version === version)
  }
  const screenshots = []
  const screenshot = async (name) => {
    const path = testInfo.outputPath(name)
    await page.screenshot({ path })
    screenshots.push(path)
  }

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
  await page.addInitScript((id) => {
    sessionStorage.setItem('leaf.cat.workbench.id.v1', id)
  }, drawingId)
  expect(uploads).toHaveLength(1)
  await expect(page.getByRole('status').filter({ hasText: /^Drawing ready$/ })).toBeVisible({ timeout: 60000 })
  await click(page.getByRole('tab', { name: 'Solar CAD', exact: true }))
  await expect(page.getByRole('tab', { name: 'Solar CAD', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('.app[data-surface="solar"]')).toBeVisible()
  await head(1)
  await expect(page.getByTestId('dock-drawing').locator('dt').filter({ hasText: /^Entities$/ }).locator('+ dd')).toHaveText('1')
  await click(page.getByRole('button', { name: 'Take edit lock', exact: true }))
  await expect(page.getByText('You hold the edit lock', { exact: true })).toBeVisible({ timeout: 30000 })
  await screenshot('lifecycle-01-drawing-ready.png')

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
  expect(initialized.params).toMatchObject({ expected_rev: 0, changes: { panels_in_sequence: 3 }, initialize: {
    schema_version: 1, units: { drawing_units: 'm', elevation_datum: 'unknown', crs: null,
      wcs_to_ucs: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1] },
  } })
  expect(initialized.params.changes).toEqual({ panels_in_sequence: 3 })
  expect(initialized.params.initialize.source_intake_sha256).toMatch(/^[0-9a-f]{64}$/)
  expect(runCount('solar-settings')).toHaveLength(1)
  await head(2)
  await expect(settings).toHaveAttribute('data-mode', 'edit', { timeout: 30000 })
  await expect(settings.getByText('Graph revision 1', { exact: true })).toBeVisible()
  await expect(settings.getByLabel('ZIP code', { exact: true })).toHaveValue('')
  await click(settings.getByRole('button', { name: 'Close', exact: true }))

  await screenshot('lifecycle-02-initialized.png')
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
  expect(created.params).toMatchObject({ expected_rev: 1, subcommand: 'Create', name: 'Tracker rows proof', current_settings: profile })
  expect(created.params.current_settings).toEqual(profile)
  expect(runCount('solar-design-presets')).toHaveLength(1)
  await head(3)
  const ground = (await intakeAt(3)).intake.solar_design_graph
  expect(ground.rev).toBe(2)
  expect(ground.project.installation_design).toBe('Ground')
  expect(ground.project.units).toMatchObject({ drawing_units: 'm', meters_per_unit: 1 })
  expect(ground.frames).toEqual([])
  await screenshot('lifecycle-03-ground-preset.png')

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
  await screenshot('lifecycle-04-tracker-rows.png')
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
  await screenshot('lifecycle-05-ground-frames.png')
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
  expect(saved.params).toMatchObject({ expected_rev: 3, project_changes: { name: 'Ground walk', zip_code: '44224' } })
  await head(5)
  await expect(settings.getByText('Graph revision 4', { exact: true })).toBeVisible({ timeout: 30000 })
  await expect(settings.getByLabel('ZIP code', { exact: true })).toHaveValue('44224')
  await click(settings.getByRole('button', { name: 'Close', exact: true }))


  await screenshot('lifecycle-06-project-saved.png')
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
  await screenshot('lifecycle-07-sizing-form.png')
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
  await screenshot('lifecycle-07-sizing-applied.png')
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
  await screenshot('lifecycle-07-sizing-replay.png')
  await click(editor.getByRole('button', { name: 'Cancel', exact: true }).last())
  const stringStep = page.locator('#solar-step-solar-string-add')
  await expect(stringStep).toBeEnabled({ timeout: 60000 })
  await expect(stringStep.getByText('Ready', { exact: true })).toBeVisible()
  await reachable(stringStep)
  expect(runCount('solar-string-add')).toHaveLength(0)
  await noHorizontalOverflow(seat)
  await screenshot('lifecycle-08-string-ready.png')


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
  await screenshot('lifecycle-08-string-selection.png')
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
  await screenshot('lifecycle-08-string-created.png')
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

  await screenshot('lifecycle-08-valid-string.png')
  const B = structuredClone(storedGraph)
  const H = storedView.version
  const R = B.rev
  const historyDialog = page.getByRole('dialog', { name: 'Version history', exact: true })
  const browserRead = async (path, action) => {
    const waiting = page.waitForRequest((request) => request.method() === 'GET'
      && new URL(request.url()).pathname === path
      && (!path.endsWith('/versions') || new URL(request.url()).searchParams.get('include_deltas') === '1'), { timeout: 60000 })
    await action()
    const request = await waiting
    const response = await request.response()
    expect(response.status()).toBe(200)
    expect(responseBodies.has(request)).toBe(true)
    return await responseBodies.get(request)
  }
  const closeHistory = async () => {
    if (await historyDialog.isVisible()) {
      await click(historyDialog.getByRole('button', { name: 'Close version history', exact: true }))
      await expect(historyDialog).not.toBeVisible()
    }
  }
  const readHistory = async () => {
    await closeHistory()
    const view = await browserRead('/api/drawings/' + drawingId + '/versions',
      () => click(page.getByRole('button', { name: 'History', exact: true })))
    await expect(historyDialog).toBeVisible()
    for (const row of view.versions) await expect(historyDialog.getByTestId('vh-row-v' + row.v)).toBeVisible()
    return view
  }
  const immutable = (view) => view.versions.map(({ v, parent, sha256, tool, bytes, created }) =>
    ({ v, parent, sha256, tool, bytes, created }))
  const readGraph = async () => {
    await closeHistory()
    if (await settings.isVisible()) await click(settings.getByRole('button', { name: 'Close', exact: true }))
    const view = await browserRead('/api/drawings/' + drawingId + '/intake',
      () => click(page.locator('#solar-step-solar-settings')))
    await expect(settings).toHaveAttribute('data-mode', 'edit')
    await click(settings.getByRole('button', { name: 'Close', exact: true }))
    return view
  }
  const V_B = await readHistory()
  expect(V_B.head).toBe(H)
  await closeHistory()
  const ownedJobIds = [initialized, created, converted, saved, sizing, stringAdded].map((run) => run.body.job_id)
  const lifecyclePosts = []
  // Operational telemetry (POST /api/telemetry, flushed on a timer or at pagehide) is not a design mutation;
  // the other local walks exclude it the same way (version-restore.spec.mjs TELEMETRY_PATHS).
  const countPost = (request) => {
    if (request.method() !== 'POST') return
    const path = new URL(request.url()).pathname
    if (path !== '/api/telemetry') lifecyclePosts.push(path)
  }
  page.on('request', countPost)

  // Step 9: native double-clicks exercise staging and consumed confirmation intent.
  await readGraph()
  await click(page.locator('#solar-step-solar-settings'))
  await expect(settings.getByLabel('Project name', { exact: true })).toHaveValue(B.project.name)
  const changedName = B.project.name + ' lifecycle'
  await fill(settings.getByLabel('Project name', { exact: true }), changedName)
  const duplicateRequests = []
  const duplicateListener = (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/run'
        && request.postDataJSON()?.tool === 'solar-settings') duplicateRequests.push(request.postDataJSON())
  }
  page.on('request', duplicateListener)
  const apply = settings.getByRole('button', { name: 'Apply settings', exact: true })
  await reachable(apply)
  await apply.dblclick()
  expect(duplicateRequests).toHaveLength(0)
  const acceptedWait = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/run'
    && response.request().postDataJSON()?.tool === 'solar-settings', { timeout: 120000 })
  const confirmation = page.getByRole('button', { name: 'Run solar-settings', exact: true })
  await expect(confirmation).toBeEnabled()
  await reachable(confirmation)
  await confirmation.dblclick()
  const acceptedResponse = await acceptedWait
  expect(acceptedResponse.status()).toBe(202)
  const accepted = await acceptedResponse.json()
  expect(accepted.job_id).toEqual(expect.any(String))
  await expect.poll(() => jobs.findLast((job) => job.job_id === accepted.job_id
    && ['complete', 'failed'].includes(job.status))?.status, { timeout: 120000 }).toBe('complete')
  ownedJobIds.push(accepted.job_id)
  await head(H + 1)
  const changedView = await readGraph()
  const C = structuredClone(changedView.intake.solar_design_graph)
  const V_C = await readHistory()
  expect(duplicateRequests).toHaveLength(1)
  page.off('request', duplicateListener)
  expect(duplicateRequests[0].dwg_version).toBe(H)
  expect(duplicateRequests[0].params.expected_rev).toBe(R)
  expect(duplicateRequests[0].params.project_changes).toEqual({ name: changedName })
  expect(V_C.head).toBe(H + 1)
  expect(V_C.latest).toBe(V_B.latest + 1)
  expect(V_C.versions).toHaveLength(V_B.versions.length + 1)
  expect(immutable(V_C).filter((row) => row.v !== H + 1)).toEqual(immutable(V_B))
  expect(V_C.versions.find((row) => row.v === H + 1).parent).toBe(H)
  expect(C.rev).toBe(R + 1)
  expect(C.parent_rev).toBe(R)
  expect(C.project.name).toBe(changedName)
  expect(C.project.zip_code).toBe(B.project.zip_code)
  expect(C.strings).toHaveLength(1)
  expect(C.strings[0].id).toBe(storedString.id)
  expect(C.strings[0].ordered_panel_refs).toEqual(orderedPanelRefs)
  expect(C.strings[0].validity).toEqual({ state: 'stale', reasons: ['project_changed'] })
  expect(C.settings.global_string_sizing_confirmed).toBe(false)
  expect(C.settings.extra?.string_sizing).toBeUndefined()
  await screenshot('lifecycle-09-single-apply-stale.png')
  await closeHistory()

  // Steps 10 and 11 use version actions; engine editing history is unrelated.
  const versionAction = async (name, route, graph, version, screenshotName) => {
    const waiting = page.waitForResponse((response) => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/drawings/' + drawingId + route)
    await click(page.getByRole('button', { name, exact: true }))
    expect((await waiting).status()).toBe(200)
    await head(version)
    const intake = await readGraph()
    expect(intake.intake.solar_design_graph).toEqual(graph)
    const versions = await readHistory()
    expect(versions.head).toBe(version)
    expect(versions.latest).toBe(V_C.latest)
    expect(immutable(versions)).toEqual(immutable(V_C))
    await screenshot(screenshotName)
    await closeHistory()
    return { intake, versions }
  }
  const undo = await versionAction('Undo version', '/undo', B, H, 'lifecycle-10-undo-valid.png')
  const redo = await versionAction('Redo version', '/redo', C, H + 1, 'lifecycle-11-redo-stale.png')
  const readOnlyPostStart = lifecyclePosts.length

  const jobList = async () => {
    const view = await browserRead('/api/jobs', async () => {
      const toolbar = page.getByRole('toolbar', { name: 'Job monitor', exact: true })
      if (await toolbar.isVisible()) {
        await click(toolbar.getByRole('button', { name: /^Expand the job monitor/ }))
      }
    })
    for (const id of ownedJobIds) {
      const matching = view.jobs.filter((job) => job.job_id === id)
      expect(matching).toHaveLength(1)
      expect(matching[0].status).toBe('complete')
    }
    return view.jobs.filter((job) => ownedJobIds.includes(job.job_id))
  }
  const reopenJob = async () => {
    const card = page.getByTestId('build-queue-card').filter({
      has: page.locator('.rail-tool').filter({ hasText: /^solar-string-add$/ }),
    })
    await expect(card).toHaveCount(1)
    await expect(card).toHaveAttribute('data-state', 'done')
    const record = await browserRead('/api/jobs/' + stringAdded.body.job_id,
      () => click(card.getByRole('button')))
    expect(record).toMatchObject({ job_id: stringAdded.body.job_id, tool: 'solar-string-add', status: 'complete' })
    expect(record.result.result.new_version.version).toBe(H)
    const drawer = page.getByRole('dialog', { name: 'solar-string-add · provenance', exact: true })
    await expect(drawer).toBeVisible()
    await expect(drawer.getByText('job ' + stringAdded.body.job_id, { exact: true })).toBeVisible()
    await expect(drawer.getByText('version ' + H, { exact: true })).toBeVisible()
    return { record, drawer }
  }
  const baselineJobs = await jobList()
  const provenance = await reopenJob()
  await screenshot('lifecycle-12-job-provenance.png')
  await click(provenance.drawer.getByRole('button', { name: 'Close details', exact: true }))
  expect((await readGraph()).intake.solar_design_graph).toEqual(C)
  await head(H + 1)

  // Reopen only after every owned job is terminal and the genuine inflight pointer is gone. A fresh page load
  // of the drawing's own address: /app mounts the console identity, whose seed is `?drawing=` or the fixed
  // demo drawing (drawingIdentity.js seedDrawingIdentity), and the app writes only `?surface=` into its URL,
  // so a bare reload reopens the demo drawing rather than this upload (declared; its own record). A fresh boot
  // reads the drawing through its session (server/routers/session.py: GET /api/session?dwg=<id> answers
  // {intake: <head intake>}), not /intake, so the reopen is bound to that request.
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightJob'))).toBeNull()
  const sessionRead = page.waitForRequest((request) => request.method() === 'GET'
    && new URL(request.url()).pathname === '/api/session'
    && new URL(request.url()).searchParams.get('dwg') === drawingId, { timeout: 60000 })
  await page.goto('/app?surface=solar&drawing=' + encodeURIComponent(drawingId))
  const sessionRequest = await sessionRead
  expect((await sessionRequest.response()).status()).toBe(200)
  expect(responseBodies.has(sessionRequest)).toBe(true)
  const reloadIntake = await responseBodies.get(sessionRequest)
  expect(reloadIntake.intake.solar_design_graph).toEqual(C)
  await expect(page.getByRole('tab', { name: 'Solar CAD', exact: true })).toHaveAttribute('aria-selected', 'true')
  await head(H + 1)
  const reloadGraph = await readGraph()
  expect(reloadGraph.intake.solar_design_graph).toEqual(C)
  expect(reloadGraph.intake.solar_design_graph.strings[0].ordered_panel_refs).toEqual(orderedPanelRefs)
  await screenshot('lifecycle-13-drawing-reopened.png')
  const reloadHistory = await readHistory()
  expect(reloadHistory.head).toBe(V_C.head)
  expect(reloadHistory.latest).toBe(V_C.latest)
  expect(immutable(reloadHistory)).toEqual(immutable(V_C))
  await screenshot('lifecycle-13-history-reopened.png')
  await closeHistory()
  const reloadJobs = await jobList()
  expect(reloadJobs).toEqual(baselineJobs)
  const reloadedProvenance = await reopenJob()
  expect(reloadedProvenance.record).toEqual(provenance.record)
  await click(reloadedProvenance.drawer.getByRole('button', { name: 'Close details', exact: true }))

  const profileReturns = []
  const workspacePresence = []
  for (const [profileName, surface, screenshotName] of [
    ['Browser', 'browser', 'lifecycle-14-browser.png'],
    ['CAD', 'cad', 'lifecycle-14-cad.png'],
    ['iOS', 'ios', 'lifecycle-14-ios.png'],
  ]) {
    const profiles = page.getByRole('tablist', { name: 'Workspace profile', exact: true })
    await click(profiles.getByRole('tab', { name: profileName, exact: true }))
    await expect(profiles.getByRole('tab', { name: profileName, exact: true })).toHaveAttribute('aria-selected', 'true')
    await expect(page.locator('.app')).toHaveAttribute('data-surface', surface)
    await expect(page.locator('.workspace-card[data-engine-document]')).toHaveAttribute('data-engine-document', drawingId + '-v' + (H + 1) + '.dxf')
    if (surface === 'cad') await expect(page.locator('.studio-ground-viewer')).toBeVisible()
    else await expect(page.locator('.studio-ground [data-ground="' + surface + '"]')).toBeVisible()
    workspacePresence.push(await page.evaluate(() => ({
      project_states: [...document.querySelectorAll('[data-project-state]')].map((node) => ({
        test_id: node.getAttribute('data-testid'), ground: node.getAttribute('data-ground'),
        state: node.getAttribute('data-project-state'),
      })),
      collection_surfaces: [...document.querySelectorAll('[data-testid]')]
        .filter((node) => /conversation|approval|receipt/i.test(node.getAttribute('data-testid')))
        .map((node) => ({ test_id: node.getAttribute('data-testid'), text: node.textContent })),
    })))
    await screenshot(screenshotName)
    await click(profiles.getByRole('tab', { name: 'Solar CAD', exact: true }))
    await expect(profiles.getByRole('tab', { name: 'Solar CAD', exact: true })).toHaveAttribute('aria-selected', 'true')
    await head(H + 1)
    const intake = await readGraph()
    expect(intake.intake.solar_design_graph).toEqual(C)
    const versions = await readHistory()
    expect(versions.head).toBe(V_C.head)
    expect(versions.latest).toBe(V_C.latest)
    expect(immutable(versions)).toEqual(immutable(V_C))
    await closeHistory()
    const records = await jobList()
    expect(records).toEqual(baselineJobs)
    profileReturns.push({ profile: profileName, intake, versions, jobs: records })
  }
  await screenshot('lifecycle-14-solar-return.png')
  const finalHistory = await readHistory()
  expect(immutable(finalHistory)).toEqual(immutable(V_C))
  await screenshot('lifecycle-15-final.png')
  let drained = 0
  while (drained < observationPromises.length) {
    const batch = observationPromises.slice(drained)
    drained += batch.length
    await Promise.all(batch)
  }
  expect(observationErrors).toEqual([])
  expect(lifecyclePosts.slice(readOnlyPostStart)).toEqual([])
  expect(lifecyclePosts).toEqual(['/api/run', '/api/drawings/' + drawingId + '/undo', '/api/drawings/' + drawingId + '/redo'])
  page.off('request', countPost)
  expect(runs.map((run) => run.tool)).toEqual(['solar-settings', 'solar-design-presets',
    'solar-trackers-to-panel-groups', 'solar-settings', 'solar-size-strings', 'solar-string-add', 'solar-settings'])
  expect(new Set(ownedJobIds).size).toBe(7)
  expect(uploads).toHaveLength(1)
  expect(publications).toHaveLength(1)
  const replayAfter = readReplay()
  expect(replayAfter.endpoint).toBe(replayBefore.endpoint)
  expect(replayAfter.pid).toBe(replayBefore.pid)
  expect(replayAfter.events).toEqual([{ method: 'POST', path: '/string-length', status: 200,
    request_sha256: REQUEST_SHA, fixture_id: 'w1-string-length-recorded',
    response_sha256: fixture.response_wire_sha256 }])
  const finalUrl = new URL(page.url())
  writeProofReceipt(join(PROOF_DIR, 'solar-lifecycle-walk-receipt.json'), {
    capability_ids: ['ID-04'], evidence_tier: 'local-e2e', route: finalUrl.pathname + finalUrl.search,
    runtime: 'managed local account-demo authentication, LEAF_AUTH_LIVE=0, APS_LIVE=0, mock-agent posture; UI-only lifecycle',
    source_commit: health.source_sha, api_endpoints: observed, artifacts: [...screenshots, replayPath],
    assertions: ['Native duplicate gestures submit one accepted solar-settings job and one immutable version',
      'Undo and Redo restore complete baseline and changed graphs including dependent validity and sizing evidence',
      'Terminal provenance, settled reload and four-profile excursions preserve committed drawing and server history'],
    result: {
      verdict: 'pass', record_id: 'solar-parity-017', slice: 'w1-12', row_complete: false,
      inspected_base: '580cad3cc3fdef4c752ad8a6a6823d51cb54de63',
      served_sha: health.source_sha, candidate_source: process.env.LEAF_SOURCE_COMMIT,
      drawing_id: drawingId, tenant_kind: 'account', tenant_id: 'demo-tenant',
      viewport: { width: 1280, height: 800 }, entry_route: '/app?surface=browser',
      final_route: finalUrl.pathname + finalUrl.search,
      setup: 'Read-only readiness and health; all upload and design operations through UI after the workbench seed; no API setup mutation.',
      accepted_run_requests: runs, owned_job_ids: ownedJobIds, terminal_jobs: baselineJobs,
      baseline: { graph: B, versions: V_B, head: H, revision: R },
      changed: { graph: C, versions: V_C }, undo, redo,
      reload: { intake: reloadGraph, versions: reloadHistory, jobs: reloadJobs }, profile_returns: profileReturns,
      reopened_provenance: provenance.record, ordered_panel_refs: orderedPanelRefs,
      request_counts: { duplicate_settings: duplicateRequests.length, lifecycle_posts: lifecyclePosts,
        history_reopen_posts: lifecyclePosts.slice(readOnlyPostStart).length },
      workspace_presence: { project: 'absent/unexercised', conversation: 'absent/unexercised',
        approvals: 'absent/unexercised', external_receipts: 'absent/unexercised',
        observed_dom: workspacePresence },
      replay: { ...replayAfter, fixture_id: fixture.id, fixture_sha256: fixture.fixture_sha256,
        request_wire_sha256: fixture.request_wire_sha256, response_wire_sha256: fixture.response_wire_sha256 },
      recipe: { setup: 'Managed account launcher with three Solar flags and -SyntheticStringSizing.',
        actions: 'Upload; lock; initialize; Ground preset; tracker rows; convert; project; size; valid string; double-click project rename; Undo; Redo; terminal provenance; reload; Browser/CAD/iOS excursions.',
        waits: 'HTTP acceptance, correlated terminal jobs, fresh UI-originated intake/version/job reads.',
        observations: 'Full graphs, immutable versions, owned job identities, request counts and screenshots.',
        cleanup_owner: 'Managed launcher owns disposable runtime; test deletes no shared state.' },
      acceptance: [
        { sentence: 'Tab reconnect resumes the existing job.', status: 'unproven; completed history reopening substitute', artifacts: screenshots.filter((p) => p.includes('lifecycle-12')) },
        { sentence: 'Duplicate clicks do not duplicate apply.', status: 'proven locally', artifacts: screenshots.filter((p) => p.includes('lifecycle-09')) },
        { sentence: 'Save/reopen restores graph, selection references and history.', status: 'partial; committed graph references and server history', artifacts: screenshots.filter((p) => p.includes('lifecycle-13')) },
        { sentence: 'Undo/redo includes dependent validity.', status: 'proven locally', artifacts: screenshots.filter((p) => /lifecycle-1[01]/.test(p)) },
        { sentence: 'A late result appears as stale without replacing the current design.', status: 'unproven locally; jsdom-only', artifacts: [] },
        { sentence: 'Switching Browser, CAD, Solar CAD and iOS preserves project, conversation, versions, approvals and receipts.', status: 'partial; standalone drawing and server version/job continuity', artifacts: screenshots.filter((p) => p.includes('lifecycle-14')) },
        { sentence: 'Expired cloud authorization gives a recovery action without losing the submitted input.', status: 'unproven; not exercised', artifacts: [] },
      ],
    },
    limitations: [
      'Active tab reconnect survival is not proven; source sends close beacons on page hide and hidden visibility.',
      'Reopened terminal provenance is not a late-result or active-reattach test.',
      'Ordered overlapping Solar results and their stale notification remain jsdom-only evidence.',
      'Cloud authorization expiry and successful recovery are not exercised.',
      REPLAY_LABEL + ' No live-service or engineering claim.',
      'Reopen covers committed Solar graph data, graph references and server history, not selected canvas state or local engine undo stack.',
      'Separate edited-DXF Save persistence is not exercised.',
      'Four-profile continuity covers this standalone drawing and observed version/job state; populated workspace project, conversation, approvals and external receipts are unproven.',
      'The iOS profile is browser UI, not physical iOS proof.',
      'APS_LIVE=0, LEAF_AUTH_LIVE=0 and mock-agent posture remain visible boundaries; the amended walk uses UI upload, with no API support upload.',
    ],
  })
})
