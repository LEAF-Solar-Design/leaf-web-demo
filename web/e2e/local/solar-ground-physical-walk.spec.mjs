import { expect as playwrightExpect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { join } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { SEED_RESULT_SCHEMA, runBody, seedParams } from '../solarGraphCommitProof.mjs'
import { requireLocalReady } from './requireReady.mjs'

// Replay from the repo root with all three VITE flags set to 1:
// web/scripts/run_unified_local_proof.ps1 -Mode account -TestGrep "GP-01 Ground Mount Physical browser walk"
// The catalog-digest product correction must be applied to the managed source first.
const expect = playwrightExpect.configure({ timeout: 30_000 })
const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')
const BOUNDARY = [[-90, -90], [90, -90], [90, 90], [-90, 90]]
const REFUSAL = 'This read is not in the current tool catalog, so refresh the tools and run it again.'
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex')
function flagsOn(testInfo) {
  if (process.env.VITE_CAD_EDIT === '1' && process.env.VITE_SOLAR_FLOW_RAIL === '1' && process.env.VITE_SOLAR_SETTINGS_FORM === '1') return true
  testInfo.annotations.push({ type: 'flag-gate', description: 'Requires VITE_CAD_EDIT=1, VITE_SOLAR_FLOW_RAIL=1 and VITE_SOLAR_SETTINGS_FORM=1.' })
  return false
}
async function ownsCentre(control) {
  await control.scrollIntoViewIfNeeded()
  await expect(control).toBeVisible()
  expect(await control.evaluate(element => {
    const box = element.getBoundingClientRect()
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)
    return hit === element || element.contains(hit)
  })).toBe(true)
}
async function click(control) { await ownsCentre(control); await control.click() }
async function fill(control, value) { await ownsCentre(control); await control.fill(value) }
async function select(control, value) { await ownsCentre(control); await control.selectOption(value) }
// Chromium's inspector cache can evict a response body once the app moves on (the engine reloads megabytes),
// so every POST body the walk asserts on is read the moment its response arrives.
async function noHorizontalOverflow(seat) {
  expect(await seat.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
}
async function dlFields(root, fields) {
  for (const [label, value] of Object.entries(fields)) {
    const values = root.locator('dt').filter({ hasText: new RegExp(`^${label}$`) }).locator('+ dd')
    await expect(values).toHaveText(Array.isArray(value) ? value : [String(value)])
  }
}
async function readFields(root, fields) {
  for (const [label, value] of Object.entries(fields)) {
    await expect(root.locator('th').filter({ hasText: new RegExp(`^${label}$`) })
      .locator('+ td')).toHaveText(String(value))
  }
}

test('GP-01 Ground Mount Physical browser walk at 1280x800', async ({ page }, testInfo) => {
  test.setTimeout(900_000)
  if (!flagsOn(testInfo)) return
  page.setDefaultTimeout(30_000)
  page.setDefaultNavigationTimeout(60_000)
  await page.setViewportSize({ width: 1280, height: 800 })
  // Playwright's own inspector keeps the browser's default 10 MB of response bodies, and the drawing reloads that
  // follow each terrain write overflow it before a just-finished body is read ("evicted from inspector cache"). A
  // second session that only enables network events and reads bodies keeps them in a larger buffer; a response is
  // matched to its id by the start time Playwright derives from the same browser events, so the match is exact.
  const inspector = await page.context().newCDPSession(page)
  const sent = new Map()
  inspector.on('Network.requestWillBeSent', event => sent.set(event.requestId, { url: event.request.url,
    method: event.request.method, wallTime: event.wallTime, timestamp: event.timestamp, at: [event.wallTime * 1e3] }))
  inspector.on('Network.responseReceived', event => {
    const entry = sent.get(event.requestId)
    const timing = event.response.timing
    if (entry && timing) entry.at.push((timing.requestTime - entry.timestamp + entry.wallTime) * 1e3)
  })
  inspector.on('Network.loadingFinished', event => {
    const entry = sent.get(event.requestId)
    if (entry) entry.finished = true
  })
  await inspector.send('Network.enable', { maxTotalBufferSize: 512_000_000, maxResourceBufferSize: 64_000_000 })
  const bodyOf = async response => {
    await response.finished()
    const request = response.request()
    const startTime = request.timing().startTime
    let id = null
    for (let tries = 0; id === null && tries < 200; tries += 1) {
      const hits = [...sent].filter(([, entry]) => entry.finished && entry.url === request.url()
        && entry.method === request.method() && entry.at.some(at => Math.abs(at - startTime) < 0.5))
      expect(hits.length, `inspector ids for ${request.method()} ${request.url()}`).toBeLessThanOrEqual(1)
      if (hits.length === 1) id = hits[0][0]
      else await page.waitForTimeout(50)
    }
    expect(id, `inspector id for ${request.method()} ${request.url()}`).not.toBeNull()
    const { body, base64Encoded } = await inspector.send('Network.getResponseBody', { requestId: id })
    return JSON.parse(base64Encoded ? Buffer.from(body, 'base64').toString('utf8') : body)
  }
  const bodyFirst = wait => wait.then(async response => ({ response, body: await bodyOf(response) }))
  const observed = []
  const observationErrors = []
  const pendingObservations = new Set()
  const jobs = []
  const uiRuns = []
  const civilRequests = []
  const importRequests = []
  const headSequence = []
  const screenshots = []
  const downloads = []
  const completedReads = []
  const actionSequence = []
  const normalized = path => path.replace(/\/api\/drawings\/[^/]+(?=\/|$)/, '/api/drawings/{id}')
    .replace(/\/api\/jobs\/[^/]+(?=\/|$)/, '/api/jobs/{id}')
    .replace(/\/artifacts\/[^/]+(?=\/|$)/, '/artifacts/{id}')
  const note = (method, path, response) => {
    observed.push(`${method} ${normalized(path)} ${response.status()}`)
    if (new URL(response.url()).pathname === '/api/run' && response.status() === 409) observationErrors.push('HTTP 409 from /api/run')
    return response
  }
  const observeBody = (response, sink) => {
    const task = bodyOf(response).then(body => sink.push(body)).catch(error => observationErrors.push(error.message))
    pendingObservations.add(task)
    task.finally(() => pendingObservations.delete(task))
  }
  // GP-SETUP-BEGIN
  const api = page.request
  const readiness = await requireLocalReady({ get: async (url, options) => note('GET', new URL(url).pathname,
    await api.get(url, options)) }, test, API_BASE)
  expect(readiness.ready).toBe(true)
  const getJson = async path => {
    const response = note('GET', path, await api.get(`${API_BASE}${path}`, { headers: TENANT_HEADERS, timeout: 15_000 }))
    expect(response.status(), path).toBe(200)
    return response.json()
  }
  const health = await getJson('/api/health')
  expect(health.source_sha).toMatch(/^[0-9a-f]{40}$/)
  expect(process.env.LEAF_SOURCE_COMMIT, 'managed runner must bind the tested source').toMatch(/^[0-9a-f]{40}$/)
  expect(health.source_sha).toBe(process.env.LEAF_SOURCE_COMMIT)
  const dxfBytes = readFileSync(join(process.cwd(), 'e2e', 'fixtures', 'distinctive-panel.dxf'))
  expect(sha256(dxfBytes)).toBe('a1c6d31bd9ef01705a44ba3143a83774f9fc529c788e2876ca38ed1d63139d40')
  const upload = note('POST', '/api/drawings/upload', await api.post(`${API_BASE}/api/drawings/upload`, {
    headers: TENANT_HEADERS, timeout: 15_000,
    multipart: { file: { name: 'distinctive-panel.dxf', mimeType: 'application/dxf', buffer: dxfBytes } },
  }))
  expect(upload.status()).toBe(202)
  const uploadedReceipt = await upload.json()
  expect(uploadedReceipt).toMatchObject({ tenant_id: 'demo-tenant', tenant_kind: 'account' })
  const drawingId = uploadedReceipt.drawing_id
  expect(drawingId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  await expect.poll(async () => (await getJson(`/api/drawings/${drawingId}/upload-status`)).status,
    { timeout: 30_000 }).toMatch(/^(ready|failed)$/)
  expect((await getJson(`/api/drawings/${drawingId}/upload-status`)).status).toBe('ready')
  const uploaded = await getJson(`/api/drawings/${drawingId}/versions`)
  expect(uploaded).toMatchObject({ head: 1, latest: 1 })
  expect(uploaded.versions).toHaveLength(1)
  expect((await getJson(`/api/drawings/${drawingId}/intake`)).intake.solar_design_graph ?? null).toBeNull()
  const tools = (await getJson('/api/tools')).tools
  const checkoutPath = `/api/drawings/${drawingId}/checkout`
  const checkout = note('POST', checkoutPath, await api.post(`${API_BASE}${checkoutPath}`, {
    headers: TENANT_HEADERS, data: { holder: 'drafter' }, timeout: 15_000,
  }))
  expect(checkout.status()).toBe(200)
  const lease = await checkout.json()
  expect(lease).toMatchObject({ acquired: true, holder: 'drafter' })
  const capability = lease.checkout_capability
  expect(typeof capability === 'string' && capability.length > 0).toBe(true)
  const setupRuns = []
  let profile
  try {
    const run = async (tool, params) => {
      const row = tools.find(candidate => candidate.name === tool)
      expect(row.catalog_digest).toEqual(expect.any(String))
      const response = note('POST', '/api/run', await api.post(`${API_BASE}/api/run?wait=1`, {
        headers: { ...TENANT_HEADERS, 'X-Checkout-Capability': capability }, timeout: 45_000,
        data: runBody({ tool, drawingId, params, catalogDigest: row.catalog_digest }),
      }))
      expect(response.status()).toBe(200)
      const body = await response.json()
      expect(body.ok).toBe(true)
      setupRuns.push({ tool, params, response: body })
      return body
    }
    const params = seedParams(uploaded.versions[0])
    params.initialize.units.drawing_units = 'm'
    const seeded = await run('solar-settings', params)
    expect(seeded.result).toMatchObject({ schema_version: SEED_RESULT_SCHEMA, after_rev: 1,
      new_version: { drawing_id: drawingId, version: 2 } })
    profile = JSON.parse(readFileSync(join(process.cwd(), '..', 'docs', 'parity', 'evidence',
      'ground', 'generate', 'profile-settings.json'), 'utf8'))
    delete profile.Name
    delete profile.IsBuiltIn
    profile.InstallationDesign = 'Ground'
    profile.UseL2Collectors = false
    expect(Object.keys(profile)).toHaveLength(60)
    const ground = await run('solar-design-presets', { expected_rev: 1, subcommand: 'Create',
      name: 'Ground Physical proof', current_settings: profile })
    expect(ground.result.new_version.version).toBe(3)
  } finally {
    const release = note('DELETE', checkoutPath, await api.delete(`${API_BASE}${checkoutPath}`, {
      headers: { ...TENANT_HEADERS, 'X-Checkout-Capability': capability }, timeout: 15_000,
    }))
    expect(release.status()).toBe(200)
  }
  const setupIntake = await getJson(`/api/drawings/${drawingId}/intake`)
  const setupGraph = setupIntake.intake.solar_design_graph
  expect(setupIntake.version).toBe(3)
  expect(setupGraph.rev).toBe(2)
  expect(setupGraph.project.installation_design).toBe('Ground')
  expect(setupGraph.project.units).toMatchObject({ drawing_units: 'm', meters_per_unit: 1 })
  expect(setupGraph.frames).toEqual([])
  expect(await getJson(`/api/drawings/${drawingId}/versions`)).toMatchObject({ head: 3, latest: 3 })
  const projectId = setupGraph.project.id
  // GP-SETUP-END

  const xmlPath = join(process.cwd(), '..', 'docs', 'parity', 'evidence', 'probes',
    'demo-probes-20260923', 'leaflandxml_fixed.xml')
  const xmlBytes = readFileSync(xmlPath)
  expect(xmlBytes.length).toBe(44046)
  expect(sha256(xmlBytes)).toBe('1422e3a71b20a620f86b4502a7e1459e97417f1e83a089b3853a2bf7905c1f74')
  const fixtureBytes = readFileSync(join(process.cwd(), '..', 'docs', 'parity', 'evidence', 'ground', 'generate', 'intake.json'))
  const fixture = JSON.parse(fixtureBytes.toString('utf8'))
  expect(fixture.active_preset.Name).toBe('TinyTest')
  expect(fixture.pile_template.Name).toBe('Default')
  page.on('request', request => {
    if (request.method() !== 'POST') return
    const path = new URL(request.url()).pathname
    if (path === '/api/run') uiRuns.push(request.postDataJSON())
    if (path === `/api/drawings/${drawingId}/terrain/operations`) civilRequests.push(request.postDataJSON())
    if (path === `/api/drawings/${drawingId}/imports/landxml`) importRequests.push(request)
  })
  page.on('response', response => {
    const url = new URL(response.url())
    if (!url.pathname.startsWith('/api/')) return
    note(response.request().method(), url.pathname + url.search, response)
    if (response.request().method() === 'GET' && response.status() === 200 && /^\/api\/jobs\/[^/]+$/.test(url.pathname)) observeBody(response, jobs)
  })
  await page.addInitScript((id) => {
    sessionStorage.setItem('leaf.cat.workbench.id.v1', id)
  }, drawingId)
  await page.goto(`/app?surface=solar&drawing=${drawingId}`)
  await page.evaluate(({ refusal }) => {
    // A refusal already on screen at installation counts, not only one added afterwards.
    window.__gpCatalogRefusalSeen = Boolean(document.body?.textContent.includes(refusal))
    new MutationObserver(() => {
      if (document.body?.textContent.includes(refusal)) window.__gpCatalogRefusalSeen = true
    }).observe(document, { childList: true, subtree: true, characterData: true })
  }, { refusal: REFUSAL })
  const document = page.locator('.workspace-card[data-engine-document]')
  await expect(document).toHaveAttribute('data-engine-document', `${drawingId}-v3.dxf`, { timeout: 60_000 })
  const canvas = page.locator('.studio-ground .viewer-canvas canvas')
  await expect(canvas).toHaveCount(1)
  await expect.poll(async () => {
    const box = await canvas.boundingBox()
    return Boolean(box && box.width > 0 && box.height > 0)
  }).toBe(true)
  await expect(page.getByRole('tab', { name: 'Solar CAD', exact: true })).toHaveAttribute('aria-selected', 'true')
  const seat = page.getByTestId('solar-flow-seat')
  await expect(seat).toHaveCount(1)
  await expect(seat).toBeVisible()
  await click(page.getByRole('button', { name: 'Take edit lock', exact: true }))
  await expect(page.getByText('You hold the edit lock', { exact: true })).toBeVisible()
  const flowSelect = page.getByTestId('solar-flow-select')
  await expect(flowSelect.locator('option[value="ground-physical"]')).toHaveText('Ground Mount Physical (preview)')
  await select(flowSelect, 'ground-physical')
  const workspace = seat.getByRole('region', { name: 'Solar workspace tools', exact: true })
  const railCheck = async () => {
    const rail = seat.getByTestId('solar-flow-rail')
    await expect(rail).toHaveAttribute('data-flow', 'ground-physical')
    await expect(rail.getByTestId('solar-flow-unavailable')).toHaveCount(0)
    await expect(rail.locator('ol button')).toHaveCount(2)
    await expect(rail.locator('#solar-step-solar-physical-shade')).toBeEnabled({ timeout: 60_000 })
    await expect(rail.locator('#solar-step-solar-physical-export')).toBeEnabled({ timeout: 60_000 })
    await expect(rail.locator('#solar-step-solar-physical-export')).toHaveAttribute('aria-current', 'step')
    await expect(page.getByText(REFUSAL, { exact: true })).toHaveCount(0)
    expect(await page.evaluate(() => window.__gpCatalogRefusalSeen)).toBe(false)
    expect(observationErrors).toEqual([])
    await noHorizontalOverflow(seat)
  }
  const screenshot = async name => {
    await railCheck()
    const path = testInfo.outputPath(name)
    await page.screenshot({ path, fullPage: true })
    screenshots.push(path)
  }
  const readback = async readWait => {
    const { response, body } = await readWait
    expect(response).toBeTruthy()
    expect(response.status()).toBe(200)
    return body
  }
  const terrainWait = (civil = false) => page.waitForRequest(request => {
    const url = new URL(request.url())
    return request.method() === 'GET' && url.pathname === `/api/drawings/${drawingId}/terrain`
      && (url.searchParams.get('view') === 'civil') === civil
  }, { timeout: 120_000 }).then(async request => {
    // The body comes from the inspector session that keeps it, not from Playwright's evicting cache.
    const response = await request.response()
    return { response, body: response && response.status() === 200 ? await bodyOf(response) : null }
  })
  const open = async name => {
    await click(workspace.getByRole('button', { name, exact: true }))
    if (name !== 'Import LandXML terrain') await expect(workspace.getByRole('heading', { name, exact: true })).toBeFocused()
    await railCheck()
  }
  const close = async name => {
    await click(workspace.locator('.solar-workspace-panel:visible').getByRole('button', { name: 'Close', exact: true }))
    await expect(workspace.getByRole('button', { name, exact: true })).toBeFocused()
    await railCheck()
  }
  const physicalHead = async (head, index) => {
    expect(head.index).toBe(index)
    expect(head.drawing_id).toBe(drawingId)
    expect(head.project_id).toBe(projectId)
    expect(head.state.source_version).toBe(3)
    await expect(workspace).toHaveAttribute('data-physical-head-index', String(index))
    headSequence.push({ index, artifact_id: head.state.artifact_id })
  }
  await railCheck()
  // Confirm the empty physical starting state through the browser, without a setup transport.
  const initialWait = terrainWait(true)
  await open('Civil operations')
  const initialCivil = await readback(initialWait)
  expect(initialCivil).toMatchObject({ stored: false, head: null, preview: null, standing: null, grade_pads: 0 })
  await close('Civil operations')
  actionSequence.push('open seeded v3 drawing', 'take browser edit lock', 'select ground-physical', 'confirm no physical state')

  await open('Import LandXML terrain')
  const importer = workspace.getByTestId('solar-landxml-upload')
  const fileControl = importer.getByLabel('LandXML file', { exact: true })
  await ownsCentre(fileControl); await fileControl.setInputFiles(xmlPath)
  await select(importer.getByRole('combobox', { name: 'Drawing units', exact: true }), 'm')
  await fill(importer.getByLabel('Coordinate system', { exact: true }), '')
  await fill(importer.getByLabel('Grid size', { exact: true }), '30')
  // The import's response body is not available to the inspector (Chromium reports it evicted even from the
  // 512 MB session), so the import is bound by its request and the summary the drafter sees, and the stored head
  // is read back below through the Terrain preview's own request for it.
  const importedWait = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/drawings/${drawingId}/imports/landxml`, { timeout: 120_000 })
  await click(importer.getByRole('button', { name: 'Import terrain', exact: true }))
  const importedResponse = await importedWait
  expect(importedResponse.status()).toBe(200)
  const importUrl = new URL(importedResponse.url())
  expect(Object.fromEntries(['drawing_units', 'crs', 'target_cells'].map(key => [key, importUrl.searchParams.get(key)])))
    .toEqual({ drawing_units: 'm', crs: 'none', target_cells: '30' })
  expect(importRequests).toHaveLength(1)
  const importHeaders = await importedResponse.request().allHeaders()
  // A fetch whose body is the chosen File exposes no request body to Playwright without interception, so the
  // body is bound by what was sent: the client posts the file itself as application/xml, and the sent length
  // equals the file's bytes exactly. The server's own summary below proves it parsed those bytes.
  expect(importHeaders['content-type']).toBe('application/xml')
  expect(importHeaders['content-length']).toBe(String(xmlBytes.length))
  expect(typeof importHeaders['x-checkout-capability'] === 'string' && importHeaders['x-checkout-capability'].length > 0).toBe(true)
  await expect(workspace).toHaveAttribute('data-physical-head-index', '0')
  await dlFields(importer.getByTestId('solar-landxml-summary'), {
    Terrain: 'Stored as terrain change 1 of this drawing', 'Survey points': '441 read',
    'Terrain grid': '30 by 30 nodes', Extent: 'X -100 to 100, Y -100 to 100 meters',
    'File units': 'Meters', 'Coordinate system': 'None', 'Elevation datum': 'Not recorded',
  })
  await expect(workspace.getByText('Terrain imported for this drawing.', { exact: true })).toBeVisible()
  await screenshot('gp-01-terrain-import.png')
  await close('Import LandXML terrain')
  actionSequence.push('import tracked LandXML bytes with m / none / 30')

  const firstTerrainWait = terrainWait()
  await open('Terrain preview')
  const imported = { head: (await readback(firstTerrainWait)).head }
  await physicalHead(imported.head, 0)
  const terrain = workspace.getByTestId('solar-terrain-panel')
  await expect(terrain).toHaveAttribute('data-phase', 'ready')
  const meshWait = bodyFirst(page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/drawings/${drawingId}/terrain/operations`, { timeout: 120_000 }))
  const meshReadWait = terrainWait()
  await click(terrain.getByRole('button', { name: 'Run mesh preview', exact: true }))
  const { response: meshResponse, body: mesh } = await meshWait
  expect(meshResponse.status()).toBe(200)
  expect(meshResponse.request().postDataJSON()).toEqual({ operation: 'mesh', expected_head: imported.head.state.artifact_id })
  expect(civilRequests).toHaveLength(1)
  expect(mesh).toMatchObject({ replaced: 0, record: { faces: 841, buckets: { Green: 717, Yellow: 124, Red: 0 },
    max_slope_percent: 8.913350836855564,
    grid_sha256: '1c78f602025556918336710801265c08a50a9e9098b48d2bda4a669bc9927452',
    mesh_sha256: '3c396f556cb26e26f98afcf2a0599bb4279c3529829ea7629a8f9e1b2b21a05e' } })
  await physicalHead(mesh.head, 1)
  const meshView = await readback(meshReadWait)
  expect(meshView.head).toEqual(mesh.head)
  await expect(terrain.getByTestId('solar-terrain-announce')).toHaveText('The mesh preview was updated')
  const meshFields = { 'Grid cell': 'X 6.897 by Y 6.897 meters',
    'Mesh preview': 'Current: 841 faces, 717 green, 124 yellow, 0 red, steepest 8.913 percent' }
  await dlFields(terrain, meshFields)
  await close('Terrain preview')
  const reopenedMeshWait = terrainWait()
  await open('Terrain preview')
  expect(await readback(reopenedMeshWait)).toEqual(meshView)
  await dlFields(terrain, meshFields)
  await screenshot('gp-02-terrain-mesh.png')
  await close('Terrain preview')
  actionSequence.push('mesh preview', 'close and reopen mesh')

  const civilOpenWait = terrainWait(true)
  await open('Civil operations')
  expect((await readback(civilOpenWait)).head).toEqual(mesh.head)
  const civil = workspace.getByRole('region', { name: 'Civil operations', exact: true })
  await expect(civil).toHaveAttribute('data-state', 'ready')
  let currentHead = mesh.head
  const operations = []
  const operate = async (operation, fields, summary, index, rendered, unchanged = false) => {
    await select(civil.getByRole('combobox', { name: 'Operation', exact: true }), operation)
    for (const [key, value] of Object.entries(fields)) {
      const label = key.charAt(0).toUpperCase() + key.slice(1).replaceAll('_', ' ')
      // A select's wrapping label also carries its option text, so exact getByLabel never matches it; selects go by role.
      const isSelect = ['drawing_units', 'mode'].includes(key)
      const control = isSelect ? civil.getByRole('combobox', { name: label, exact: true }) : civil.getByLabel(label, { exact: true })
      if (isSelect) await select(control, value)
      else await fill(control, value === null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value))
    }
    const body = { operation, expected_head: currentHead.state.artifact_id, ...fields }
    const count = civilRequests.length
    const responseWait = bodyFirst(page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === `/api/drawings/${drawingId}/terrain/operations`
      && response.request().postDataJSON()?.operation === operation, { timeout: 120_000 }))
    const followWait = terrainWait(true)
    await click(civil.getByRole('button', { name: 'Run', exact: true }))
    const { response, body: value } = await responseWait
    expect(response.status()).toBe(200)
    expect(response.request().postDataJSON()).toEqual(body)
    const headers = await response.request().allHeaders()
    expect(typeof headers['x-checkout-capability'] === 'string' && headers['x-checkout-capability'].length > 0).toBe(true)
    expect(value).toMatchObject({ operation, outcome: unchanged ? 'unchanged' : 'published', created: !unchanged,
      drawing_id: drawingId, project_id: projectId, base: currentHead.state.artifact_id, summary })
    if (unchanged) expect(value.head).toEqual(currentHead)
    else expect(value.head.parent).toBe(currentHead.state.artifact_id)
    await physicalHead(value.head, index)
    const persisted = await readback(followWait)
    expect(persisted.head).toEqual(value.head)
    expect(civilRequests).toHaveLength(count + 1)
    await expect(civil).toHaveAttribute('data-state', 'ready')
    await expect(civil.getByText(`Outcome: ${value.outcome}`, { exact: true })).toBeVisible()
    await dlFields(civil, rendered)
    currentHead = value.head
    operations.push({ request: body, response: value, persisted })
    actionSequence.push(operation)
    await railCheck()
    return persisted
  }
  const counts = (piles = '0') => ({ Frames: '242', Piles: piles, 'Collision markers': '0', 'Range markers': '0' })
  const standing = async (entity, checked) => {
    const region = civil.getByRole('region', { name: `${entity} standing`, exact: true })
    await expect(region.getByText(`${entity}: current`, { exact: true })).toBeVisible()
    await dlFields(region, { Checked: String(checked), Stale: '0' })
  }
  await operate('frame-generate', { boundary: BOUNDARY, preset: fixture.active_preset, drawing_units: 'm' },
    { frames_added: 242, frames_off_terrain: 0, preset_name: 'TinyTest' }, 2,
    { ...counts(), 'Frames added': '242', 'Frames off terrain': '0', 'Preset name': 'TinyTest' })
  await standing('Frames', 242)
  await screenshot('gp-03-native-frames.png')
  await operate('grade-pad', { boundary: BOUNDARY, mode: 'Auto', value_du: null }, {
    pads_added: 1, grade_pads: 1, mode: 'Auto', elevation_m: 0.00042853201704251266, label: 'PAD 1\\P0.00 m',
    total_cut_m3: 9309.176071621352, total_fill_m3: 9323.060508973502, net_m3: -13.884437352149689,
  }, 3, { ...counts(), 'Pads added': '1', 'Grade pads': '1', Mode: 'Auto', 'Elevation m': '0.000428532',
    Label: 'PAD 1\\P0.00 m', 'Total cut m3': '9309.18', 'Total fill m3': '9323.06', 'Net m3': '-13.8844' })
  await screenshot('gp-04-grade-pad.png')
  await operate('piling-generate', { preset: fixture.active_preset, pile_template: fixture.pile_template }, {
    native_frames: 242, piles: 1936, piles_replaced: 0, grid_piles: 1936, joint_piles: 0,
    station_piles: 0, short_trackers: 0, template_name: 'Default',
  }, 4, { ...counts(['1936', '1936']), 'Native frames': '242', 'Piles replaced': '0', 'Grid piles': '1936',
    'Joint piles': '0', 'Station piles': '0', 'Short trackers': '0', 'Template name': 'Default' })
  await standing('Frames', 242)
  await standing('Piles', 1936)
  await screenshot('gp-05-native-piles.png')
  await operate('pile-length-range-check', { preset: fixture.active_preset },
    { total_piles: 1936, out_of_range: 0, min_m: 1.0, max_m: 6.0 }, 4,
    { ...counts('1936'), 'Total piles': '1936', 'Out of range': '0', 'Min m': '1', 'Max m': '6' }, true)
  await screenshot('gp-06-civil-range.png')
  const beforeCivil = await operate('frame-collision-detect', {}, { frames_checked: 242, collisions: 0 }, 4,
    { ...counts('1936'), 'Frames checked': '242', Collisions: '0' }, true)
  expect(beforeCivil.grade_pads).toBe(1)
  await screenshot('gp-06-civil-checks.png')
  await close('Civil operations')
  const beforeTerrainWait = terrainWait()
  await open('Terrain preview')
  const beforeTerrain = await readback(beforeTerrainWait)
  expect(beforeTerrain.head).toEqual(currentHead)
  await close('Terrain preview')

  const readOpenWait = terrainWait()
  await open('Physical reads')
  expect((await readback(readOpenWait)).head).toEqual(currentHead)
  const physical = workspace.getByRole('region', { name: 'Physical reads', exact: true })
  const root = physical.getByTestId('solar-read-result')
  const read = async (tool, format) => {
    await select(physical.getByRole('combobox', { name: 'Tool', exact: true }), tool)
    if (format) await select(physical.getByRole('combobox', { name: 'Format', exact: true }), format)
    const count = uiRuns.length
    const runWait = bodyFirst(page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/run'
      && response.request().postDataJSON()?.tool === tool, { timeout: 120_000 }))
    await click(physical.getByRole('button', { name: 'Run', exact: true }))
    const { response, body: accepted } = await runWait
    expect(response.status()).toBe(202)
    expect(accepted.job_id).toEqual(expect.any(String))
    expect(accepted.job_id.length).toBeGreaterThan(0)
    expect(completedReads.map(item => item.job.job_id)).not.toContain(accepted.job_id)
    const body = response.request().postDataJSON()
    expect(body.dwg_version).toBe(3)
    expect(body.catalog_digest).toEqual(tools.find(row => row.name === tool).catalog_digest)
    expect(body.params).toEqual({ drawing_id: drawingId, ...(format ? { expected_head: currentHead.state.artifact_id, format } : {}) })
    await expect.poll(() => jobs.findLast(job => job.job_id === accepted.job_id
      && ['complete', 'failed'].includes(job.status))?.status ?? null, { timeout: 120_000 }).toBe('complete')
    const job = jobs.findLast(item => item.job_id === accepted.job_id && item.status === 'complete')
    expect(job.result.ok).toBe(true)
    const data = job.result.result
    expect(data).toMatchObject({ schema_version: 'leaf.solar-graph-read.v1', adapter: 'local-graph-read',
      tool, drawing_id: drawingId, source_version: 3, job_id: accepted.job_id, drawing_changed: false })
    for (const key of ['graph_sha256', 'output_sha256', 'request_sha256']) expect(data[key]).toMatch(/^[0-9a-f]{64}$/)
    expect(data.graph_sha256).toBe(setupIntake.intake.solar_design_graph_sha256)
    if (completedReads.length) expect(data.graph_sha256).toBe(completedReads[0].data.graph_sha256)
    expect(data.output.head).toEqual(currentHead)
    await expect(physical).toHaveAttribute('data-state', 'result', { timeout: 120_000 })
    await expect(root).toHaveAttribute('data-tool', tool)
    await expect(root.getByTestId('solar-read-headline')).toHaveCount(0)
    await click(root.getByText('Read details', { exact: true }))
    await readFields(root.locator('details'), { Tool: tool, Drawing: drawingId, Version: '3', Job: accepted.job_id,
      Graph: data.graph_sha256, Output: data.output_sha256, Request: data.request_sha256, Representation: data.representation })
    await click(root.getByText('Read details', { exact: true }))
    expect(uiRuns).toHaveLength(count + 1)
    completedReads.push({ request: body, accepted, job, data })
    actionSequence.push(`${tool}${format ? ` ${format}` : ''}`)
    await railCheck()
    return data
  }
  const shade = await read('solar-physical-shade')
  expect(shade.output).toMatchObject({ sample_count: 242, mean_shade: 1.016659763357062e-06, datum_shift_m: 1.5, frames_omitted: 42 })
  await readFields(root.locator(':scope > table'), { 'Sample count': '242', 'Mean shade': '0.00000101666',
    'Datum shift m': '1.5', 'Frames omitted': '42' })
  const section = name => root.getByRole('region', { name, exact: true })
  await readFields(section('Units'), { 'Drawing units': 'm', 'Meters per unit': '1' })
  await readFields(section('Settings'), { Mode: 'defaults', 'Target clearance m': '1.5', 'Profile selection': 'automatic' })
  await readFields(section('Profile'), { Name: 'full', 'Angle count': '468', 'Ray step m': '1', 'Max ray m': '400', 'Estimated samples': '45302400' })
  await readFields(section('Surface'), { Rows: '30', Cols: '30', Cells: '900' })
  const framesTable = section('Frames').locator('table')
  const headings = await framesTable.locator('thead th').allTextContents()
  const shadeColumn = headings.indexOf('Shade')
  const indexColumn = headings.indexOf('Frame index')
  expect(shadeColumn).toBeGreaterThanOrEqual(0)
  expect(indexColumn).toBeGreaterThanOrEqual(0)
  const rows = framesTable.locator('tbody tr')
  await expect(rows).toHaveCount(200)
  for (let index = 0; index < 200; index += 1) {
    await expect(rows.nth(index).locator('td').nth(indexColumn)).toHaveText(String(index))
    await expect(rows.nth(index).locator('td').nth(shadeColumn)).toHaveText([18, 136, 139].includes(index) ? '0.0000820106' : '0')
  }
  await screenshot('gp-07-shade.png')

  const downloadArtifact = async (data, filename, length, digest, size) => {
    const ref = data.output.artifact
    expect(ref).toMatchObject({ filename, byte_length: length, content_sha256: digest, source_version: 3 })
    expect(ref.download).toBe(`/api/drawings/${drawingId}/artifacts/${ref.artifact_id}`)
    const button = root.getByTestId('solar-read-download')
    await expect(button).toHaveAccessibleName(`Download ${filename} (${size})`)
    const artifactWait = page.waitForRequest(request => request.method() === 'GET'
      && new URL(request.url()).pathname === ref.download, { timeout: 60_000 })
    const downloadWait = page.waitForEvent('download', { timeout: 60_000 })
    await click(button)
    const [request, download] = await Promise.all([artifactWait, downloadWait])
    const response = await request.response()
    expect(response.status()).toBe(200)
    const headers = response.headers()
    expect(headers['x-leaf-artifact-id']).toBe(ref.artifact_id)
    expect(headers['content-type']).toContain(ref.media_type)
    expect(download.suggestedFilename()).toBe(filename)
    const savedPath = testInfo.outputPath(filename)
    await download.saveAs(savedPath)
    expect(await download.failure()).toBeNull()
    const bytes = readFileSync(savedPath)
    expect(bytes.length).toBe(length)
    expect(bytes.length).toBe(ref.byte_length)
    expect(sha256(bytes)).toBe(digest)
    expect(sha256(bytes)).toBe(ref.content_sha256)
    // The saved file's length and sha256 above bind it to the server's artifact reference. The response body is
    // not compared: the inspector decodes a text response and drops a leading UTF-8 byte-order mark.
    await expect(root.getByTestId('solar-read-download-status')).toHaveText(`Downloaded ${filename}.`)
    downloads.push({ ref, saved_path: savedPath, byte_length: bytes.length, sha256: sha256(bytes) })
    actionSequence.push(`download ${filename}`)
  }
  const terrainExport = await read('solar-physical-export', 'terrain-csv')
  await readFields(section('Summary'), { Schema: 'leaf.solar-physical-export.v1', Maturity: 'preview',
    Scope: 'terrain-nodes', Format: 'terrain-csv', Units: '{"drawing_units":"m","meters_per_unit":1}',
    Grid: '{"rows":30,"cols":30,"cells":900}', Settings: 'none', 'Sample count': 'none', Profile: 'none',
    'Mean shade': 'none', 'Datum shift m': 'none' })
  await downloadArtifact(terrainExport, 'terrain.csv', 20260,
    'dce40cab9e64df8867ed5cd9339d32b146b5e76c12d09e5579058622b775545c', '19.8 KB')
  await screenshot('gp-08-terrain-csv.png')
  const shadeExport = await read('solar-physical-export', 'shade-sam')
  await readFields(section('Summary'), { Schema: 'leaf.solar-physical-export.v1', Maturity: 'preview',
    Scope: 'cpu-terrain-native-frame-centres', Format: 'shade-sam', 'Sample count': '242', Profile: 'full',
    'Mean shade': '0.00000101666', 'Datum shift m': '1.5',
    Settings: '{"mode":"defaults","target_clearance_m":1.5,"profile_selection":"automatic"}' })
  await downloadArtifact(shadeExport, 'shade-sam.csv', 6764,
    '4341604aa60fdf03e36a95aea69be93a8986e0af0fe37a5b7ed368309a028628', '6.6 KB')
  await screenshot('gp-09-shade-csv.png')
  await close('Physical reads')

  const finalCivilWait = terrainWait(true)
  await open('Civil operations')
  const afterCivil = await readback(finalCivilWait)
  expect(afterCivil).toEqual(beforeCivil)
  expect(afterCivil.grade_pads).toBe(1)
  await dlFields(civil, counts('1936'))
  await standing('Frames', 242)
  await standing('Piles', 1936)
  await physicalHead(afterCivil.head, 4)
  await close('Civil operations')
  const finalTerrainWait = terrainWait()
  await open('Terrain preview')
  const afterTerrain = await readback(finalTerrainWait)
  expect(afterTerrain).toEqual(beforeTerrain)
  await expect(terrain).toHaveAttribute('data-phase', 'ready')
  await expect(terrain).toHaveAttribute('data-head', currentHead.state.artifact_id)
  await expect(terrain.locator('[data-line="mesh"]')).toHaveAttribute('data-state', 'current')
  await expect(document).toHaveAttribute('data-engine-document', `${drawingId}-v3.dxf`)
  expect(uiRuns).toHaveLength(3)
  expect(importRequests).toHaveLength(1)
  expect(civilRequests).toHaveLength(6)
  expect(headSequence.map(head => head.index)).toEqual([0, 1, 2, 3, 4, 4, 4, 4])
  expect(new Set(completedReads.map(item => item.job.job_id)).size).toBe(3)
  while (pendingObservations.size) await Promise.all([...pendingObservations])
  expect(observationErrors).toEqual([])
  await railCheck()
  actionSequence.push('reopen civil and terrain; compare persisted views', 'write success receipt')
  const finalUrl = new URL(page.url())
  writeProofReceipt(join(PROOF_DIR, 'gp-01-ground-physical-receipt.json'), {
    capability_ids: ['ID-04'], evidence_tier: 'local-e2e', route: finalUrl.pathname + finalUrl.search,
    runtime: 'real managed local Vite, FastAPI, broker, worker, upload extraction and physical/version stores; account mode',
    source_commit: health.source_sha, api_endpoints: [...new Set(observed)],
    artifacts: [...screenshots, ...downloads.map(item => item.saved_path)],
    assertions: ['browser imports the unchanged 44046-byte LandXML, reads 441 points and a 30 by 30 grid',
      'mesh renders 841 faces with 717 Green, 124 Yellow, 0 Red and maximum slope 8.913350836855564',
      'browser publishes 242 native frames, one measured grade pad and 1936 native piles at head 4',
      'range and collision checks leave head 4 unchanged with zero findings and current standing',
      'shade renders 242 samples, 200 frame rows and all measured profile, surface and settings values',
      'three UI reads answer 202 with distinct browser-observed completed jobs and correlated Read details',
      'terrain.csv and shade-sam.csv browser saves match frozen byte lengths, SHA-256 and artifact references',
      'persisted civil and terrain views are identical before and after reads; drawing stays seated at v3',
      'each control owns its centre and the seat has no horizontal overflow; rail remains on export'],
    result: { record_id: 'GP-01', verdict: 'pass', source_sha: health.source_sha, tested_source: process.env.LEAF_SOURCE_COMMIT,
      drawing_id: drawingId, tenant_id: 'demo-tenant', tenant_kind: 'account', project_id: projectId,
      viewport: { width: 1280, height: 800 }, setup_declaration: 'API upload, extraction, metre graph seed, Ground preset, setup lease release; all subsequent operations through UI',
      setup_runs: setupRuns, setup_graph: setupGraph, input_hashes: { dxf: sha256(dxfBytes), landxml: sha256(xmlBytes),
        intake_fixture: sha256(fixtureBytes), profile: sha256(JSON.stringify(profile)),
        preset: sha256(JSON.stringify(fixture.active_preset)), pile_template: sha256(JSON.stringify(fixture.pile_template)),
        boundary: sha256(JSON.stringify(BOUNDARY)) },
      declared_boundary: BOUNDARY, initial_civil: initialCivil, action_sequence: actionSequence,
      import_head: imported.head, mesh_response: mesh, operations, head_sequence: headSequence,
      completed_reads: completedReads, downloads, screenshots,
      rendered_expectations: { mesh: meshFields, shade_rows: 200, nonzero_rows: [18, 136, 139],
        shade_text: '0.0000820106', mean_shade_text: '0.00000101666' },
      unchanged_state: { before_civil: beforeCivil, after_civil: afterCivil, before_terrain: beforeTerrain, after_terrain: afterTerrain,
        drawing_version: 3, graph_hashes: completedReads.map(item => item.data.graph_sha256) } },
    limitations: ['Local account preview, LEAF_AUTH_LIVE=0; no live auth or cross-tenant proof.',
      'The refusal observer starts after page load, so a refusal shown and cleared before installation is not counted; the text assertion in railCheck still catches a refusal that stays on screen.',
      'No Autodesk APS, licensed AutoCAD, DWG writeback or engineering approval.',
      'DXF is the drawing container; terrain capture, synthetic polygon and captured presets are declared inputs. The polygon is not extracted from DXF.',
      'Graph seed and Ground preset are API setup; their forms are not proved.',
      'Native physical frames and piles are separate from electrical graph frames. No tracker conversion, electrical sizing, stringing or equipment proof.',
      'Grade-pad publication appends a pad without changing the grid.',
      'Shade uses native frame centres, default clearance and automatic profiles; no weather weighting or individual-module shading.',
      'Only terrain-csv and shade-sam are covered; zero range and collision findings apply only to these inputs.',
      'Rail aria-current identifies the last enabled catalog tool, not completion of the five conceptual stages.',
      'Frozen arithmetic measurements are expectations; this receipt is written only after the positive browser assertions.'],
  })
})
