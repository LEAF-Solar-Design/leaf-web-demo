import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { PRESET_LABELS } from '../../src/solar/solarPresetModel.js'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')
// With a blank project name and ZIP the server's readiness refuses sizing and names the first missing project field
// (project_name_required, recomputed from the project's own fields by solar_project.project_validity), so the flow rail
// disables the step with this sentence before its form can open; the form's own ZIP sentence is therefore not what a
// drafter sees from the rail.
const BLANK_PROJECT = 'Name the project in Solar settings first'
const NO_GRANT = 'String sizing needs a valid cloud sizing grant, which is unavailable for this workspace. Your inputs are kept.'
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
  'Grant reference': 'grant_1',
}
const SIZING_SELECT = { Scope: 'global', Bifacial: 'false', Racking: 'fixed_tilt', 'Open circuit rise': 'false' }

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

test('G1b ground electrical boundary through the browser', async ({ page }, testInfo) => {
  test.setTimeout(600000)
  if (!flagsOn(testInfo)) return
  if (process.env.LEAF_CLOUD_GRANTS_FILE !== undefined) {
    testInfo.annotations.push({ type: 'grant-gate', description: 'Requires LEAF_CLOUD_GRANTS_FILE to be unset; no-grant boundary was not executed.' })
    return
  }
  await page.setViewportSize({ width: 1280, height: 800 })
  await requireLocalReady(page.request, test, API_BASE)
  const healthResponse = await page.request.get(`${API_BASE}/api/health`, { timeout: 15000 })
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
  const confirmRun = async (tool) => {
    const responseWait = page.waitForResponse((response) => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/run'
      && response.request().postDataJSON()?.tool === tool, { timeout: 120000 })
    const button = page.getByRole('button', { name: `Run ${tool}`, exact: true })
    await expect(button).toBeEnabled({ timeout: 60000 })
    await click(button)
    const response = await responseWait
    expect([200, 202]).toContain(response.status())
    const body = await response.json()
    expect(body.job_id).toEqual(expect.any(String))
    await expect.poll(() => jobs.findLast((job) => job.job_id === body.job_id
      && ['complete', 'failed'].includes(job.status))?.status ?? null, { timeout: 120000 })
      .toBe(tool === 'solar-size-strings' ? 'failed' : 'complete')
    return { params: response.request().postDataJSON().params, body }
  }
  const head = async (version) => {
    const document = `${drawingId}-v${version}.dxf`
    await expect(page.locator('.workspace-card[data-engine-document]')).toHaveAttribute('data-engine-document', document, { timeout: 60000 })
    // The dock names the drawing as the footer's document tab does; the engine document id is the
    // card attribute above, and the Source row proves that document is the one shown.
    const dock = page.getByTestId('dock-drawing')
    await expect(dock.locator('dt').filter({ hasText: /^Source$/ }).locator('+ dd')).toHaveText('browser drawing', { timeout: 60000 })
    await expect(page.locator('.foot-doc-tab')).toBeVisible({ timeout: 60000 })
    const shownName = (await page.locator('.foot-doc-tab').innerText()).trim()
    expect(shownName).toContain('distinctive-panel')
    await expect(dock.locator('dt').filter({ hasText: /^Name$/ }).locator('+ dd')).toHaveText(shownName, { timeout: 60000 })
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
  await screenshot('g1b-tracker-rows-published.png')
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
  await screenshot('g1b-ground-converted.png')
  if (await editor.isVisible()) await click(editor.getByRole('button', { name: 'Cancel', exact: true }).last())
  const sizingStep = page.locator('#solar-step-solar-size-strings')
  await expect(sizingStep).toBeDisabled({ timeout: 30000 })
  await expect(page.locator('#solar-step-solar-size-strings-reason')).toHaveText(BLANK_PROJECT)
  expect(runCount('solar-size-strings')).toHaveLength(0)
  await screenshot('g1b-sizing-blank-zip.png')
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

  const historyBaseline = async () => {
    // Arm before the opening; the response must belong to this fresh UI load.
    const fresh = page.waitForResponse((response) => response.request().method() === 'GET'
      && new URL(response.url()).pathname === `/api/drawings/${drawingId}/versions`, { timeout: 30000 })
    await ribbon(page, 'View', page.getByRole('button', { name: 'History', exact: true }))
    const response = await fresh
    expect(response.status()).toBe(200)
    await response.finished()
    const drawer = page.getByRole('dialog', { name: 'Version history', exact: true })
    await expect(drawer.getByLabel('Loading history')).toHaveCount(0, { timeout: 30000 })
    const rows = drawer.locator('[data-testid^="vh-row-v"]')
    await expect(rows).toHaveCount(5, { timeout: 30000 })
    const ids = await rows.evaluateAll((elements) => elements.map((element) => element.dataset.testid).sort())
    expect(ids).toEqual(['vh-row-v1', 'vh-row-v2', 'vh-row-v3', 'vh-row-v4', 'vh-row-v5'])
    await expect(drawer.getByTestId('vh-row-v5').locator('.vh-mark')).toHaveText('head')
    await expect(drawer.locator('.vh-mark')).toHaveCount(1)
    await expect(drawer.getByTestId('vh-row-v6')).toHaveCount(0)
    const digest = (await drawer.getByTestId('vh-row-v5').locator('.drawer-mono').innerText()).trim()
    expect(digest).toMatch(/^[0-9a-f]{12}$/)
    await click(drawer.getByRole('button', { name: 'Close version history', exact: true }))
    await expect(drawer).toHaveCount(0, { timeout: 30000 })
    return { ids, digest }
  }
  const before = await historyBaseline()
  await click(page.locator('#solar-step-solar-size-strings'))
  await expect(editor.getByText('Saved project ZIP: 44224', { exact: true })).toBeVisible({ timeout: 30000 })
  for (const [label, value] of Object.entries(SIZING_SELECT)) await select(editor.getByRole('combobox', { name: label, exact: true }), value)
  for (const [label, value] of Object.entries(SIZING_TEXT)) await fill(editor.getByLabel(label, { exact: true }), value)
  await reachable(editor.getByLabel('Use module parameters', { exact: true }))
  await editor.getByLabel('Use module parameters', { exact: true }).setChecked(false)
  await expect(editor.getByRole('button', { name: 'Review & run', exact: true })).toBeEnabled({ timeout: 30000 })
  await click(editor.getByRole('button', { name: 'Review & run', exact: true }))
  const sizing = await confirmRun('solar-size-strings')
  expect(sizing.params).toMatchObject({ expected_rev: 4, mode: 'global', grant_ref: 'grant_1', confirm: true })
  const targetIds = Object.keys(sizing.params.requests)
  expect(targetIds).toHaveLength(1)
  expect(targetIds[0]).toBe((await intakeAt(5)).intake.solar_design_graph.settings.id)
  expect(sizing.params.requests[targetIds[0]]).toEqual({
    module_name: SIZING_TEXT.Module, full_inverter_name: SIZING_TEXT.Inverter,
    bifacial: false, bifacial_coefficient: '.7', racking_params: { racking_type: 'fixed_tilt',
      surface_tilt: '5', surface_azimuth: '180', albedo: '.25' }, max_voltage: '1500',
    thermal_model_type: 'close mount glass glass', open_circuit_rise: false, zip_code: '44224',
  })
  const sizingJobId = sizing.body.job_id
  expect(sizingJobId).toEqual(expect.any(String))
  await expect.poll(() => jobs.findLast((job) => job.job_id === sizingJobId && job.status === 'failed') ?? null,
    { timeout: 120000 }).not.toBeNull()
  const terminal = jobs.findLast((job) => job.job_id === sizingJobId && job.status === 'failed')
  expect(terminal.error).toMatchObject({ reason_code: 'CLOUD_AUTH_MISSING', error_code: 'FORBIDDEN', retryable: false })
  await expect(editor.getByRole('alert')).toHaveText(NO_GRANT, { timeout: 120000 })
  for (const [label, value] of Object.entries(SIZING_TEXT)) await expect(editor.getByLabel(label, { exact: true })).toHaveValue(value)
  for (const [label, value] of Object.entries(SIZING_SELECT)) await expect(editor.getByRole('combobox', { name: label, exact: true })).toHaveValue(value)
  await expect(editor.getByLabel('Use module parameters', { exact: true })).not.toBeChecked()
  await expect(editor.getByRole('button', { name: 'Retry', exact: true })).toBeEnabled()
  await expect(editor.getByText('String sizing applied.', { exact: true })).toHaveCount(0)
  const after = await historyBaseline()
  expect(after).toEqual(before)
  await head(5)
  await expect(editor.getByRole('alert')).toHaveText(NO_GRANT)
  await reachable(editor.getByRole('alert'))
  await expect(page.getByTestId('dock-drawing')).toBeVisible()
  await noHorizontalOverflow(seat)
  expect(uploads).toHaveLength(1)
  expect(publications).toHaveLength(1)
  expect(runCount('solar-settings')).toHaveLength(2)
  expect(runCount('solar-design-presets')).toHaveLength(1)
  expect(runCount('solar-trackers-to-panel-groups')).toHaveLength(1)
  expect(runCount('solar-size-strings')).toHaveLength(1)
  expect(observationErrors).toEqual([])
  await screenshot('g1b-sizing-no-grant.png')
  const finalUrl = new URL(page.url())
  writeProofReceipt(join(PROOF_DIR, 'sf-w3-ground-boundary-walk-receipt.json'), {
    capability_ids: ['ID-04'], evidence_tier: 'local-e2e', route: finalUrl.pathname + finalUrl.search,
    runtime: 'real managed local Vite, FastAPI, upload extraction, checkout, broker/job lifecycle and version stores; account mode; no sizing grant',
    source_commit: health.source_sha, api_endpoints: observed, artifacts: screenshots,
    assertions: ['Browser upload identity continues into Solar CAD, engine document and drawing dock',
      'UI-only initialization and all 60 preset settings; preset and conversion revisions are automatic',
      'exact four-key measured tracker publication occurs once and leaves drawing v3 unchanged',
      'conversion creates revision 3, v4, two Ground frames with five slots, measured centres and 450 W power',
      BLANK_PROJECT, NO_GRANT,
      'correlated browser job refuses with CLOUD_AUTH_MISSING, FORBIDDEN and retryable false; inputs retained and Retry enabled',
      'fresh UI history before and after sizing retains exactly v1-v5, v5 head and the same displayed digest prefix; zero added versions'],
    result: { verdict: 'pass', record_id: 'sf-w3-ground-boundary-walk', entry_route: '/app?surface=browser',
      source_sha: health.source_sha, drawing_id: drawingId, tenant_kind: 'account', tenant_id: 'demo-tenant',
      graph_revisions: [1, 2, 3, 4], physical_head_index: 0, physical_artifact: publication.head.state.artifact_id,
      converted_frames: 2, converted_slots: 5, sizing_job_id: sizingJobId, terminal_reason: 'CLOUD_AUTH_MISSING',
      displayed_sentence: NO_GRANT, head_before: 5, head_after: 5, version_count_before: before.ids.length,
      version_count_after: after.ids.length, new_versions: 0, digest_prefix_before: before.digest,
      digest_prefix_after: after.digest, grant_file_present: false },
    limitations: ['Local browser workflow through a specific refusal boundary; ID-04 attribution is limited to this walk.',
      'Does not prove successful cloud sizing or real grant provisioning; successful sizing belongs to G2b.',
      'Does not prove equipment selection/placement, feeders, electrical compliance, automatic tracker layout or engineering validity; downstream work belongs to G3.',
      'Equipment names are form inputs; no equipment is installed by this row.',
      'Unchanged state covers fresh UI version chain, displayed head and digest prefix, not an independent full storage audit.',
      'Account mode uses LEAF_AUTH_LIVE=0; APS is outside this proof.'],
  })
})
