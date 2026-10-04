import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createHash } from 'node:crypto'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api.js'
import * as landxmlUpload from './SolarLandxmlUpload.jsx'
import * as landxmlClients from './solarLandxmlClient.js'
import * as terrainClients from './solarTerrainClient.js'
import * as terrainPanels from './SolarTerrainPanel.jsx'
import * as trackerClients from './solarTrackerRowsClient.js'
import * as trackerPanels from './SolarTrackerRowsPanel.jsx'
import * as importClients from './solarImportClient.js'
import * as solarEdgePanels from './SolarEdgeImportPanel.jsx'
import * as civilClients from './solarCivilClient.js'
import * as civilPanels from './SolarCivilPanel.jsx'
import { FLOW_PANELS } from './solarWorkspacePanels.js'
import { TRACKER_ROWS_PANEL_REASONS as TRACKER_SENTENCES } from './SolarTrackerRowsPanel.jsx'
import SolarWorkspaceTools, { TERRAIN_WORKSPACE_REASONS, SOLAREDGE_WORKSPACE_REASONS } from './SolarWorkspaceTools.jsx'
import { COMBINER_INTAKE_REASONS } from './solarCombinerIntakeClient.js'

// The route capture also used by SolarLandxmlUpload.test.jsx; the real client validates it.
const FIRST_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8","media_type":"application/json","filename":"physical-state.json","byte_length":18283,"content_sha256":"0de2afeb7ae692538ce33fd011b043438cd65cc426fd1cead1933eb4046d22f0","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8"}},"error":null,"degraded_mode":false}`
const UNAUTH_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"UNAUTHENTICATED","message":"missing bearer token (Authorization header)","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Sign in, then repeat the request."},"degraded_mode":false}`
const CREATED = 'Terrain imported for this drawing.'
const EXISTING = 'This terrain was already imported, so nothing changed.'
const F3_SOURCE_ID = 'a'.repeat(64)
const gp5ReadHead = (artifact = gp5H, drawing = 'solar', project = 'p') => ({
  schema: 'leaf.solar-physical-head.v1', drawing_id: drawing, project_id: project, index: artifact === gp5H ? 0 : 1, parent: artifact === gp5H ? null : gp5H,
  state: { schema: 'leaf.solar-artifact-ref.v1', artifact_id: artifact, content_sha256: gp5D, media_type: 'application/json',
    filename: 'physical-state.json', byte_length: 1024, source_version: 1, download: `/api/drawings/${drawing}/artifacts/${artifact}` },
})
const gp5ReadTerrain = (head = gp5ReadHead()) => ({
  schema: 'leaf.solar-terrain-view-response.v1', stored: true, head,
  terrain: { schema: 'leaf.solar-terrain-view.v1', maturity: 'preview', drawing_id: head.drawing_id, project_id: head.project_id,
    frame: { coordinate_system: 'world', transform: 'identity', drawing_units: 'm', meters_per_unit: 1, crs: 'none',
      elevation_datum: 'unrecorded', horizontal: 'drawing-units', elevation: 'metres' },
    grid: { rows: 2, cols: 2, x_min: 0, x_max: 10, y_min: 0, y_max: 10, cell_x: 10, cell_y: 10, cell_x_m: 10, cell_y_m: 10,
      elevation_min_m: 0, elevation_max_m: 0, grid_sha256: gp5D },
    mesh_faces: 0, slope_markers: 0, previews: {
      'terrain-mesh-render': { state: 'absent', record: null }, 'tracker-slope-violations': { state: 'absent', record: null },
    },
  },
})
const gp5ReadArtifact = (filename = 'terrain.csv', byteLength = 138) => ({
  schema: 'leaf.solar-artifact-ref.v1', artifact_id: gp5D, content_sha256: gp5D, media_type: 'text/csv', filename,
  byte_length: byteLength, source_version: 1, download: `/api/drawings/solar/artifacts/${gp5D}`,
})
const gp5ReadAnswer = (tool = 'solar-physical-shade', format = 'terrain-csv', head = gp5ReadHead(), version = 1) => ({
  ok: true, result: {
    schema_version: 'leaf.solar-graph-read.v1', adapter: 'local-graph-read', drawing_id: head.drawing_id, project_id: head.project_id,
    source_version: version, drawing_changed: false, tool, job_id: 'physical-test', output_sha256: gp5D,
    output: tool === 'solar-physical-shade' ? {
      schema: 'leaf.solar-physical-shade.v1', maturity: 'preview', scope: 'cpu-terrain-native-frame-centres', head,
      sample_count: 15, mean_shade: 0, datum_shift_m: 1.5,
      units: { drawing_units: 'm', meters_per_unit: 1 },
      settings: { mode: 'defaults', target_clearance_m: 1.5, profile_selection: 'automatic' },
      profile: { name: 'full', angle_count: 468, ray_step_m: 1, max_ray_m: 400, estimated_samples: 2808000 },
      surface: { rows: 21, cols: 21, cells: 441 },
      frames: Array.from({ length: 15 }, (_, i) => ({ sample_index: i, frame_index: i, shade: 0 })), frames_omitted: 0,
    } : {
      head,
      summary: { schema: 'leaf.solar-physical-export.v1', maturity: 'preview',
        scope: format === 'terrain-csv' ? 'terrain-nodes' : 'cpu-terrain-native-frame-centres', head: gp5clone(head), format,
        units: { drawing_units: 'm', meters_per_unit: 1 }, grid: { rows: 2, cols: 2, cells: 4 },
        settings: null, sample_count: null, profile: null, mean_shade: null, datum_shift_m: null },
      artifact: gp5ReadArtifact(),
    },
  },
})
const gp5Deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done }); return { promise, resolve } }
const gp5Success = (value) => ({ ok: true, status: 200, value })
function gp5CivilProps(extra = {}) {
  const p = supplied({ projectId: 'p', ...extra })
  const client = { getCivil: vi.fn(async () => gp5Success(gp5viewOf())),
    runCivilOperation: vi.fn(async () => gp5Success(gp5resultOf())) }
  vi.spyOn(civilClients, 'createSolarCivilClient').mockReturnValue(client)
  return { p, client }
}
const gp5OpenCivil = () => fireEvent.click(screen.getByRole('button', { name: 'Civil operations' }))
const gp5Ready = async () => { await waitFor(() => expect(screen.getByRole('button', { name: 'Run' }).disabled).toBe(false)) }
function gp5EditCivil() {
  fireEvent.change(screen.getByLabelText('Boundary'), { target: { value: JSON.stringify(gp5square) } })
  fireEvent.change(screen.getByLabelText('Preset'), { target: { value: JSON.stringify(gp5preset) } })
  fireEvent.change(screen.getByLabelText('Drawing units'), { target: { value: 'm' } })
}
function gp5WriterHarness({ realKind, fetchImpl } = {}) {
  const captured = {}
  const refusal = () => Promise.resolve({ ok: false, code: 'TERRAIN_CLIENT_REQUEST_INVALID' })
  const clients = {
    civil: { getCivil: vi.fn(refusal), runCivilOperation: vi.fn(refusal) },
    terrain: { getTerrain: vi.fn(refusal), runTerrainOperation: vi.fn(refusal) },
    tracker: { createTrackerRows: vi.fn(refusal) }, landxml: { uploadLandxml: vi.fn(refusal) },
  }
  if (realKind !== 'civil') vi.spyOn(civilClients, 'createSolarCivilClient').mockReturnValue(clients.civil)
  vi.spyOn(landxmlClients, 'createSolarLandxmlClient').mockReturnValue(clients.landxml)
  vi.spyOn(terrainClients, 'createSolarTerrainClient').mockReturnValue(clients.terrain)
  if (realKind !== 'tracker') vi.spyOn(trackerClients, 'createSolarTrackerRowsClient').mockReturnValue(clients.tracker)
  vi.spyOn(civilPanels, 'default').mockImplementation((props) => { captured.civil = props; return <p>Civil test control</p> })
  vi.spyOn(terrainPanels, 'default').mockImplementation((props) => { captured.terrain = props; return <p>Terrain test control</p> })
  vi.spyOn(trackerPanels, 'default').mockImplementation((props) => { captured.tracker = props; return <p>Tracker test control</p> })
  vi.spyOn(landxmlUpload, 'default').mockImplementation((props) => { captured.landxml = props; return <p>LandXML test control</p> })
  const p = supplied()
  if (fetchImpl) p.transport.fetchImpl = fetchImpl
  const mounted = render(<SolarWorkspaceTools {...p} />)
  // Capture actual container wrappers before opening civil. They remain callable even when hidden.
  open()
  fireEvent.click(screen.getByRole('button', { name: 'Terrain preview' }))
  fireEvent.click(screen.getByRole('button', { name: 'Create tracker rows' }))
  gp5OpenCivil()
  return { captured, clients, p, mounted }
}

it('GP5-01 the host property is the original frozen five-flow registry', () => {
  expect(SolarWorkspaceTools.workspacePanelsByFlow).toBe(FLOW_PANELS)
  expect(Object.isFrozen(FLOW_PANELS)).toBe(true)
  expect(Object.keys(FLOW_PANELS)).toHaveLength(5)
  expect(FLOW_PANELS['ground-physical']).toEqual(['landxml', 'terrain', 'tracker-rows', 'civil', 'physical-read'])
  expect(Object.getOwnPropertyDescriptor(SolarWorkspaceTools, 'workspacePanelsByFlow').writable).toBe(false)
})

it('GP5-10 civil mounting is lazy for every flow and local or project scope', async () => {
  const { p, client } = gp5CivilProps()
  const mounted = render(<SolarWorkspaceTools {...p} />)
  for (const drawingId of [null, 'solar']) for (const projectId of [null, TERRAIN_PROJECT]) {
    for (const flow of Object.keys(FLOW_PANELS)) mounted.rerender(<SolarWorkspaceTools {...p} {...{ drawingId, projectId, flow }} />)
  }
  expect(client.getCivil).not.toHaveBeenCalled()
  expect(client.runCivilOperation).not.toHaveBeenCalled()
  expect(p.transport.fetchImpl).not.toHaveBeenCalled()
  client.getCivil.mockResolvedValue(gp5Success(gp5absent()))
  for (const projectId of [null, TERRAIN_PROJECT]) {
    mounted.rerender(<SolarWorkspaceTools {...p} projectId={projectId} />)
    gp5OpenCivil(); await gp5Ready()
    expect(client.getCivil).toHaveBeenLastCalledWith({ drawingId: 'solar', projectId, signal: expect.any(AbortSignal) })
  }
  expect(client.getCivil).toHaveBeenCalledTimes(2)
  expect(client.runCivilOperation).not.toHaveBeenCalled()
})

it('GP5-11 explicit open close and A to B to A discard old civil scope', async () => {
  const { p, client } = gp5CivilProps()
  const mounted = render(<SolarWorkspaceTools {...p} />)
  gp5OpenCivil(); await gp5Ready()
  expect(client.getCivil.mock.calls[0][0]).toMatchObject({ drawingId: 'solar', projectId: 'p' })
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Civil operations' }))
  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Civil operations' }))
  gp5OpenCivil(); await gp5Ready(); gp5EditCivil()
  const pending = gp5Deferred(); client.runCivilOperation.mockReturnValue(pending.promise)
  fireEvent.click(screen.getByRole('button', { name: 'Run' }))
  for (const projectId of ['other', 'p']) mounted.rerender(<SolarWorkspaceTools {...p} projectId={projectId} />)
  gp5OpenCivil(); await gp5Ready()
  await act(async () => pending.resolve(gp5Success(gp5resultOf())))
  expect(p.onPhysicalHeadChanged).not.toHaveBeenCalled()
  expect(screen.queryByText('Outcome: published')).toBeNull()
  mounted.rerender(<SolarWorkspaceTools {...p} flow="rooftop" />)
  mounted.rerender(<SolarWorkspaceTools {...p} />)
  expect(screen.queryByRole('region', { name: 'Civil operations' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Civil operations' }).getAttribute('aria-expanded')).toBe('false')
})

it('GP5-12 civil authentication and sanitized checkout are obtained per request', async () => {
  let current = 'cap-one'
  const p = supplied({ projectId: TERRAIN_PROJECT, getCheckoutCapability: () => current })
  p.transport.headers.mockImplementation(() => ({ Authorization: `Bearer ${current}`, 'X-Tenant-Id': current,
    'x-checkout-capability': 'stale', 'X-CHECKOUT-CAPABILITY': 'also-stale' }))
  p.transport.fetchImpl.mockImplementation(async (url, init) => init.method === 'GET'
    ? new Response(JSON.stringify(gp5absent()), { status: 200 })
    : new Response(UNAUTH_TEXT, { status: 401 }))
  render(<SolarWorkspaceTools {...p} />)
  gp5OpenCivil(); await gp5Ready(); gp5EditCivil()
  const first = p.transport.fetchImpl.mock.calls[0]
  expect(first[0]).toContain(`view=civil&project_id=${encodeURIComponent(TERRAIN_PROJECT)}`)
  expect(first[1].headers).toMatchObject({ Authorization: 'Bearer cap-one', 'X-Tenant-Id': 'cap-one', 'X-Checkout-Capability': 'cap-one' })
  current = 'cap-two'
  fireEvent.click(screen.getByRole('button', { name: 'Run' }))
  await screen.findByRole('alert')
  const post = p.transport.fetchImpl.mock.calls.find(([, init]) => init.method === 'POST')
  expect(JSON.parse(post[1].body)).toEqual(gp5bodyOf('frame-generate', null))
  expect(post[0]).toContain(`project_id=${encodeURIComponent(TERRAIN_PROJECT)}`)
  expect(post[1].headers).toMatchObject({ Authorization: 'Bearer cap-two', 'X-Tenant-Id': 'cap-two', 'X-Checkout-Capability': 'cap-two' })
  expect(Object.keys(post[1].headers).filter((key) => key.toLowerCase() === 'x-checkout-capability')).toEqual(['X-Checkout-Capability'])
  expect(p.transport.onResponse).toHaveBeenCalled()
  expect(p.onPhysicalHeadChanged).not.toHaveBeenCalled()
  expect(p.onDrawingVersionChanged).not.toHaveBeenCalled()
})

it('GP5-13 civil and all physical writers exclude each other synchronously', async () => {
  const { captured, clients, mounted } = gp5WriterHarness()
  const request = { drawingId: 'solar', projectId: null, body: {} }
  const methods = { landxml: () => captured.landxml.upload(request),
    terrain: () => captured.terrain.client.runTerrainOperation(request),
    tracker: () => captured.tracker.client.createTrackerRows(request) }
  const actual = { landxml: clients.landxml.uploadLandxml, terrain: clients.terrain.runTerrainOperation,
    tracker: clients.tracker.createTrackerRows }
  for (const kind of ['landxml', 'terrain', 'tracker']) {
    const pending = gp5Deferred(); actual[kind].mockReturnValueOnce(pending.promise)
    let civil
    act(() => { methods[kind](); civil = captured.civil.client.runCivilOperation(request) })
    expect(await civil).toMatchObject({ ok: false, code: 'TERRAIN_CLIENT_REQUEST_INVALID' })
    expect(clients.civil.runCivilOperation).not.toHaveBeenCalled()
    await act(async () => pending.resolve({ ok: false }))
  }
  const pending = gp5Deferred(); clients.civil.runCivilOperation.mockReturnValueOnce(pending.promise)
  let blocked
  act(() => {
    captured.civil.client.runCivilOperation(request)
    blocked = Object.values(methods).map((send) => send())
  })
  const refusals = await Promise.all(blocked)
  expect(refusals.every((answer) => answer.ok === false)).toBe(true)
  expect(actual.terrain).toHaveBeenCalledTimes(1)
  expect(actual.tracker).toHaveBeenCalledTimes(1)
  expect(actual.landxml).toHaveBeenCalledTimes(1)
  await act(async () => pending.resolve({ ok: false }))
  mounted.unmount()
})

it('GP5-14 a cancelled civil UI retains its token until its promise settles', async () => {
  const { captured, clients, mounted } = gp5WriterHarness()
  const pending = gp5Deferred(); clients.civil.runCivilOperation.mockReturnValueOnce(pending.promise)
  const controller = new AbortController()
  const request = { drawingId: 'solar', projectId: null, body: {}, signal: controller.signal }
  act(() => { captured.civil.client.runCivilOperation(request); controller.abort() })
  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  expect(await captured.terrain.client.runTerrainOperation({ ...request, signal: undefined })).toMatchObject({ ok: false })
  expect(clients.terrain.runTerrainOperation).not.toHaveBeenCalled()
  await act(async () => pending.resolve({ ok: false }))
  let answer
  await act(async () => { answer = await captured.terrain.client.runTerrainOperation({ ...request, signal: undefined }) })
  expect(clients.terrain.runTerrainOperation).toHaveBeenCalledTimes(1)
  expect(answer.ok).toBe(false)
  gp5OpenCivil()
  const older = gp5Deferred(), remaining = gp5Deferred()
  clients.landxml.uploadLandxml.mockReturnValueOnce(older.promise).mockReturnValueOnce(remaining.promise)
  act(() => { captured.landxml.upload({ ...request, signal: undefined }); captured.landxml.upload({ ...request, signal: undefined }) })
  await act(async () => older.resolve({ ok: false }))
  expect(await captured.civil.client.runCivilOperation({ ...request, signal: undefined })).toMatchObject({ ok: false, code: 'TERRAIN_CLIENT_REQUEST_INVALID' })
  expect(clients.civil.runCivilOperation).toHaveBeenCalledTimes(1)
  await act(async () => remaining.resolve({ ok: false }))
  await act(async () => { await captured.civil.client.runCivilOperation({ ...request, signal: undefined }) })
  expect(clients.civil.runCivilOperation).toHaveBeenCalledTimes(2)
  mounted.unmount()
})

async function gp5RealAbortRetainsTransport(kind) {
  for (const rejectTransport of [false, true]) {
    let resolve, reject
    const pending = new Promise((done, fail) => { resolve = done; reject = fail })
    const fetchImpl = vi.fn(async () => new Response(UNAUTH_TEXT, { status: 401 }))
      .mockImplementationOnce(() => pending)
    const { captured, clients, mounted } = gp5WriterHarness({ realKind: kind, fetchImpl })
    const controller = new AbortController()
    const request = { drawingId: 'solar', projectId: null, signal: controller.signal,
      ...(kind === 'civil' ? { body: gp5bodyOf('frame-generate', null) }
        : { drawingUnits: 'm', request: trackerM1() }) }
    let answer
    act(() => { answer = kind === 'civil' ? captured.civil.client.runCivilOperation(request)
      : captured.tracker.client.createTrackerRows(request) })
    await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(1))
    act(() => controller.abort())
    await act(async () => {
      expect(await answer).toMatchObject({ ok: false,
        code: kind === 'civil' ? 'TERRAIN_CLIENT_ABORTED' : 'TRACKER_ROWS_CLIENT_ABORTED' })
    })
    expect(fetchImpl.mock.calls[0][1].signal.aborted).toBe(true)
    const otherRequest = { drawingId: 'solar', projectId: null, body: {} }
    const excluded = [
      () => captured.terrain.client.runTerrainOperation(otherRequest),
      () => captured.landxml.upload(otherRequest),
      () => kind === 'civil' ? captured.tracker.client.createTrackerRows(otherRequest)
        : captured.civil.client.runCivilOperation(otherRequest),
    ]
    await act(async () => {
      for (const send of excluded) expect(await send()).toMatchObject({ ok: false })
    })
    expect(clients.terrain.runTerrainOperation).not.toHaveBeenCalled()
    expect(clients.landxml.uploadLandxml).not.toHaveBeenCalled()
    expect(kind === 'civil' ? clients.tracker.createTrackerRows : clients.civil.runCivilOperation).not.toHaveBeenCalled()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    await act(async () => {
      if (rejectTransport) reject(new Error('aborted transport'))
      else resolve(new Response(UNAUTH_TEXT, { status: 401 }))
    })
    await act(async () => { await captured.terrain.client.runTerrainOperation(otherRequest) })
    expect(clients.terrain.runTerrainOperation).toHaveBeenCalledTimes(1)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    await act(async () => {
      const next = { ...request, signal: undefined }
      const admitted = await (kind === 'civil' ? captured.civil.client.runCivilOperation(next)
        : captured.tracker.client.createTrackerRows(next))
      expect(admitted).toMatchObject({ ok: false, code: 'UNAUTHENTICATED', status: 401 })
    })
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    mounted.unmount()
    vi.restoreAllMocks()
  }
}

it('GP5-20 real civil cancellation holds the interlock until its fetch settles', async () => {
  await gp5RealAbortRetainsTransport('civil')
})

it('GP5-21 real tracker cancellation holds the interlock until its fetch settles', async () => {
  await gp5RealAbortRetainsTransport('tracker')
})

it('GP5-16 physical shade and export preserve automatic head reads and authenticated downloads without mutations', async () => {
  const p = supplied({ projectId: 'p', onRunPlacement: vi.fn() })
  const terrain = { getTerrain: vi.fn(async () => gp5Success(gp5ReadTerrain())), runTerrainOperation: vi.fn() }
  vi.spyOn(terrainClients, 'createSolarTerrainClient').mockReturnValue(terrain)
  const exportAnswer = gp5ReadAnswer('solar-physical-export')
  const artifact = exportAnswer.result.output.artifact
  const bytes = new Uint8Array(artifact.byte_length).fill(65)
  artifact.content_sha256 = createHash('sha256').update(bytes).digest('hex')
  const fetchImpl = vi.fn(async () => new Response(bytes, { headers: {
    'x-leaf-artifact-id': artifact.artifact_id, 'content-length': String(bytes.byteLength), 'content-type': 'text/csv',
  } }))
  const authenticated = importClients.createSolarImportClient({ fetchImpl,
    headers: () => ({ Authorization: 'Bearer gp5', 'X-Tenant-Id': 'gp5' }),
    sha256Hex: async (content) => createHash('sha256').update(content).digest('hex') })
  vi.spyOn(importClients, 'createSolarImportClient').mockReturnValue(authenticated)
  p.transport.runRead = vi.fn(async () => gp5ReadAnswer())
  p.transport.save = vi.fn()
  render(<SolarWorkspaceTools {...p} />)
  fireEvent.click(screen.getByRole('button', { name: 'Physical reads' }))
  await gp5Ready()
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Physical reads' }))
  fireEvent.click(screen.getByRole('button', { name: 'Run' }))
  await screen.findByTestId('solar-read-result')
  expect(p.transport.runRead).toHaveBeenLastCalledWith('solar-physical-shade', { drawing_id: 'solar' }, 'solar', { projectId: 'p', dwgVersion: 1 })
  expect(terrain.getTerrain).toHaveBeenCalledTimes(3)
  fireEvent.change(screen.getByLabelText('Tool'), { target: { value: 'solar-physical-export' } })
  expect([...screen.getByLabelText('Format').options].map((option) => option.value)).toEqual(['terrain-csv', 'shade-azal-matrix', 'shade-sam', 'shade-per-panel'])
  p.transport.runRead.mockResolvedValue(exportAnswer)
  fireEvent.click(screen.getByRole('button', { name: 'Run' }))
  await screen.findByTestId('solar-read-result')
  expect(p.transport.runRead).toHaveBeenLastCalledWith('solar-physical-export', { drawing_id: 'solar', expected_head: gp5H, format: 'terrain-csv' }, 'solar', { projectId: 'p', dwgVersion: 1 })
  fireEvent.click(screen.getByTestId('solar-read-download'))
  await waitFor(() => expect(p.transport.save).toHaveBeenCalledTimes(1))
  expect(fetchImpl.mock.calls[0][1].headers).toMatchObject({ Authorization: 'Bearer gp5', 'X-Tenant-Id': 'gp5' })
  expect(p.transport.save).toHaveBeenCalledWith(bytes, 'text/csv', artifact.filename)
  expect(terrain.runTerrainOperation).not.toHaveBeenCalled()
  expect(p.onPhysicalHeadChanged).not.toHaveBeenCalled()
  expect(p.onDrawingVersionChanged).not.toHaveBeenCalled()
  expect(p.onRunPlacement).not.toHaveBeenCalled()
  expect(announcement()).toBe('')
})

it('GP5-15 created civil publication updates peers once without clearing its own outcome', async () => {
  const { p, client } = gp5CivilProps()
  const peers = {}
  vi.spyOn(terrainPanels, 'default').mockImplementation((props) => { peers.terrain = props; return <p>Terrain peer</p> })
  vi.spyOn(trackerPanels, 'default').mockImplementation((props) => { peers.tracker = props; return <p>Tracker peer</p> })
  const mounted = render(<SolarWorkspaceTools {...p} />)
  fireEvent.click(screen.getByRole('button', { name: 'Create tracker rows' }))
  gp5OpenCivil(); await gp5Ready(); gp5EditCivil()
  client.getCivil.mockResolvedValue(gp5Success(gp5viewOf(gp5headOf(gp5H2, 1, gp5H))))
  fireEvent.click(screen.getByRole('button', { name: 'Run' }))
  await gp5Ready()
  expect(screen.getByText('Outcome: published')).toBeTruthy()
  expect(client.getCivil).toHaveBeenCalledTimes(2)
  expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
  expect(p.onDrawingVersionChanged).not.toHaveBeenCalled()
  expect(peers.tracker.headSignal).toBe(gp5H2)
  expect(screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index')).toBe('1')
  fireEvent.click(screen.getByRole('button', { name: 'Terrain preview' }))
  expect(peers.terrain.headSignal).toBe(gp5H2)
  mounted.unmount()
})
// Synthetic fixtures use the closed server contracts; no fixture comes from another worktree.
const gp5H = 'a'.repeat(64)
const gp5H2 = 'b'.repeat(64)
const gp5D = 'd'.repeat(64)
const gp5clone = (v) => JSON.parse(JSON.stringify(v))
const gp5headOf = (artifact = gp5H, index = 0, parent = null, drawing = 'solar', project = 'p') => ({
  schema: 'leaf.solar-physical-head.v1', drawing_id: drawing, project_id: project, index, parent,
  state: { schema: 'leaf.solar-artifact-ref.v1', artifact_id: artifact, content_sha256: gp5D,
    media_type: 'application/json', filename: 'physical-state.json', byte_length: 1024, source_version: 1,
    download: `/api/drawings/${drawing}/artifacts/${artifact}` },
})
const gp5previewOf = (frames = 242, piles = 0, terrain = true) => ({
  schema: 'leaf.solar-frames-piles-preview.v1', maturity: 'preview', frames, piles,
  collision_markers: 0, range_markers: 0, terrain,
})
const gp5standingOf = (frames = 242, piles = 0, stale = 0, terrain = true) => ({
  schema: 'leaf.solar-frames-piles-terrain-standing.v1', maturity: 'preview', grid_sha256: terrain ? gp5D : null,
  frames: { state: frames === 0 ? 'absent' : stale ? 'stale' : 'current', checked: frames, stale },
  piles: { state: piles === 0 ? 'absent' : 'current', checked: piles, stale: 0 },
})
const gp5viewOf = (head = gp5headOf(), frames = 242, piles = 0) => ({
  schema: 'leaf.solar-civil-view-response.v1', stored: true, head, preview: gp5previewOf(frames, piles),
  standing: gp5standingOf(frames, piles), grade_pads: 0,
})
const gp5absent = () => ({ schema: 'leaf.solar-civil-view-response.v1', stored: false, head: null, preview: null, standing: null, grade_pads: 0 })
const gp5square = [[0, 0], [100, 0], [100, 100], [0, 100]]
const gp5preset = { Name: 'Full', PileTemplateName: 'Full', Rows: 2, Columns: 1, Piling: { MinPileLengthM: 1, MaxPileLengthM: 4 } }
const gp5template = { Name: 'Full', Stations: [{ Position: 0.5, Length: 2 }] }
const gp5bodyOf = (operation = 'frame-generate', expected = gp5H) => ({
  operation, expected_head: expected,
  ...(operation === 'frame-generate' ? { boundary: gp5clone(gp5square), preset: gp5clone(gp5preset), drawing_units: 'm' } : {}),
  ...(operation === 'piling-generate' ? { preset: gp5clone(gp5preset), pile_template: gp5clone(gp5template) } : {}),
  ...(operation === 'pile-length-range-check' ? { preset: gp5clone(gp5preset) } : {}),
  ...(operation === 'grade-pad' ? { boundary: gp5clone(gp5square), mode: 'Auto', value_du: null } : {}),
})
const gp5resultOf = (operation = 'frame-generate', outcome = 'published', base = gp5H) => ({
  schema: operation === 'grade-pad' ? 'leaf.solar-civil-operation.v1' : 'leaf.solar-frames-piles.v1',
  operation, maturity: 'preview', outcome, created: outcome === 'published',
  drawing_id: 'solar', project_id: 'p', base, units: { drawing_units: 'm', meters_per_unit: 1 },
  terrain: { present: true, sampled: ['frame-generate', 'piling-generate', 'grade-pad'].includes(operation), rows: 2, cols: 2, grid_sha256: gp5D },
  summary: outcome === 'retry' ? null : {
    'frame-generate': { frames_added: 242, frames_off_terrain: 0, preset_name: 'Full' },
    'frame-collision-detect': { frames_checked: 242, collisions: 0 },
    'piling-generate': { native_frames: 242, piles: 1936, piles_replaced: 0, grid_piles: 1936, joint_piles: 0, station_piles: 0, short_trackers: 0, template_name: 'Full' },
    'pile-length-range-check': { total_piles: 1936, out_of_range: 0, min_m: 1, max_m: 4 },
    'grade-pad': { pads_added: 1, grade_pads: 1, mode: 'Auto', elevation_m: 0, label: 'Pad', total_cut_m3: 0, total_fill_m3: 0, net_m3: 0 },
  }[operation],
  preview: gp5previewOf(242, operation === 'piling-generate' ? 1936 : 0),
  standing: gp5standingOf(242, operation === 'piling-generate' ? 1936 : 0),
  head: outcome === 'unchanged' ? gp5headOf(base) : gp5headOf(gp5H2, base === null ? 0 : 1, base),
})
const F3_REPORT_ID = 'b'.repeat(64)
function f3Artifact(id, pdf = false) {
  return { schema: 'leaf.solar-artifact-ref.v1', artifact_id: id,
    media_type: pdf ? 'application/pdf' : 'application/json',
    filename: pdf ? 'solaredge-source.pdf' : 'solaredge-report.json', byte_length: 100,
    content_sha256: 'c'.repeat(64), source_version: 3,
    download: `/api/drawings/solar/artifacts/${id}` }
}
function f3Source() {
  return { schema: 'leaf.solar-import-source.v1', kind: 'solaredge-pdf', drawing_id: 'solar',
    project_id: TERRAIN_PROJECT, source_version: 3, graph_sha256: 'd'.repeat(64),
    page_count: 2, source: f3Artifact(F3_SOURCE_ID, true) }
}
function f3Report() {
  return { schema: 'leaf.solar-solaredge-report-result.v1', kind: 'solaredge-report', drawing_id: 'solar',
    project_id: TERRAIN_PROJECT, source_version: 3, graph_sha256: 'd'.repeat(64),
    source_artifact_id: F3_SOURCE_ID, counts: {
      pdf_matrices: 1, pdf_panels: 20, matchable_grids: 2, bridge_grids: 1, frames: 3,
      matched_frames: 2, group_strings: 4, bridge_strings: 1, strings: 5, assigned_panels: 18,
      unassigned_panels: 2, partial_strings: 1,
    }, report: f3Artifact(F3_REPORT_ID) }
}
const f3Success = (value) => ({ ok: true, status: 200, value })
function f3Props(overrides = {}, real = false) {
  const p = trackerProps({ drawingVersion: 3, flow: 'solaredge-import',
    onStageSolarEdgeAccept: vi.fn(), ...overrides })
  p.transport.readIntake.mockImplementation(async (id, version) => ({
    version: version === 'head' ? p.drawingVersion : version, head: p.drawingVersion,
    intake: { solar_design_graph: { rev: 12, project: { id: p.projectId ?? TERRAIN_PROJECT,
      units: { drawing_units: 'm', meters_per_unit: 1 } } } } }))
  const client = { uploadPdf: vi.fn(async () => f3Success(f3Source())),
    requestReport: vi.fn(async () => f3Success(f3Report())) }
  if (!real) vi.spyOn(importClients, 'createSolarImportClient').mockReturnValue(client)
  else {
    const existing = p.transport.fetchImpl.getMockImplementation()
    p.transport.fetchImpl.mockImplementation((url, init) => url.includes('/imports/solaredge-pdf')
      ? Promise.resolve(terrainResponse(url.includes('/report') ? f3Report() : f3Source()))
      : existing(url, init))
  }
  return { p, client }
}
const f3Trigger = () => screen.getByRole('button', { name: 'Import SolarEdge PDF' })
const f3File = () => screen.getByLabelText('SolarEdge layout PDF')
const f3UploadButton = () => screen.getByRole('button', { name: 'Upload', exact: true })
const f3BuildButton = () => screen.getByRole('button', { name: 'Build report' })
const f3AcceptButton = () => screen.getByRole('button', { name: 'Accept tracking labels' })
function f3Handler(button) {
  return button[Object.keys(button).find((key) => key.startsWith('__reactProps'))].onClick
}
function f3Choose() {
  const file = new File(['%PDF-1.7'], 'layout.pdf', { type: 'application/pdf' })
  fireEvent.change(f3File(), { target: { files: [file] } })
  return file
}
function f3Open() { fireEvent.click(f3Trigger()) }
async function f3Upload() {
  f3Choose()
  fireEvent.click(f3UploadButton())
  await screen.findByLabelText('Alignment tolerance')
}
async function f3Build() {
  fireEvent.change(screen.getByLabelText('Alignment tolerance'), { target: { value: '0.5' } })
  fireEvent.click(f3BuildButton())
  await screen.findByRole('region', { name: 'Report review' })
}
async function f3Ready() {
  await f3Upload(); await f3Build()
  await waitFor(() => expect(f3AcceptButton().disabled).toBe(false))
}
function f3Reason(sentence) { expect(screen.getAllByText(sentence).length).toBeGreaterThan(0) }

describe('F3 SolarEdge workspace integration', () => {
  it('F3 26 closing during pending staging focuses the enabled trigger', async () => {
    const { p } = f3Props({ onStageSolarEdgeAccept: vi.fn(() => new Promise(() => {})) })
    render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Ready()
    fireEvent.click(f3AcceptButton())
    expect(p.onStageSolarEdgeAccept).toHaveBeenCalledTimes(1)
    const close = screen.getByRole('button', { name: 'Close' })
    close.focus()
    fireEvent.click(close)
    expect(f3Trigger().disabled).toBe(false)
    expect(document.activeElement).toBe(f3Trigger())
    expect(document.activeElement).not.toBe(document.body)
  })

  it('F3 27 closing during pending staging keeps focus on a reason that remains', async () => {
    const { p } = f3Props({ onStageSolarEdgeAccept: vi.fn(() => new Promise(() => {})) })
    const view = render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Ready()
    fireEvent.click(f3AcceptButton())
    expect(p.onStageSolarEdgeAccept).toHaveBeenCalledTimes(1)
    view.rerender(<SolarWorkspaceTools {...p} busy={true} />)
    const close = screen.getByRole('button', { name: 'Close' })
    close.focus()
    fireEvent.click(close)
    expect(document.activeElement).toBe(screen.getByText(SOLAREDGE_WORKSPACE_REASONS.run_in_progress))
    expect(document.activeElement.tagName).toBe('P')
    expect(document.activeElement).not.toBe(document.body)
  })

  it('F3 28 a report kept across 3 to 4 to 3 stays stale', async () => {
    const { p, client } = f3Props()
    let panelProps
    const Panel = solarEdgePanels.default
    vi.spyOn(solarEdgePanels, 'default').mockImplementation((props) => {
      panelProps = props
      return <Panel {...props} />
    })
    const view = render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Ready()
    view.rerender(<SolarWorkspaceTools {...p} drawingVersion={4} />)
    await waitFor(() => expect(panelProps.graphRev).toBe(12))
    expect(p.transport.readIntake).toHaveBeenLastCalledWith('solar', 4)
    view.rerender(<SolarWorkspaceTools {...p} drawingVersion={3} />)
    await waitFor(() => expect(panelProps.graphRev).toBe(12))
    expect(p.transport.readIntake).toHaveBeenLastCalledWith('solar', 3)
    expect(p.transport.readIntake).toHaveBeenCalledTimes(3)
    expect(panelProps.onAccept({ expected_rev: 12, report_artifact_id: F3_REPORT_ID })).toBe(false)
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.report_stale)
    expect(f3AcceptButton().disabled).toBe(true)
    expect(p.onStageSolarEdgeAccept).not.toHaveBeenCalled()
    expect(client.requestReport).toHaveBeenCalledTimes(1)
  })

  it('F3 29 a report kept across 3 to undefined to 3 stays stale', async () => {
    const { p, client } = f3Props()
    let panelProps
    const Panel = solarEdgePanels.default
    vi.spyOn(solarEdgePanels, 'default').mockImplementation((props) => {
      panelProps = props
      return <Panel {...props} />
    })
    const view = render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Ready()
    view.rerender(<SolarWorkspaceTools {...p} drawingVersion={undefined} />)
    expect(panelProps.graphRev).toBeUndefined()
    expect(p.transport.readIntake).toHaveBeenCalledTimes(1)
    view.rerender(<SolarWorkspaceTools {...p} drawingVersion={3} />)
    await waitFor(() => expect(panelProps.graphRev).toBe(12))
    expect(p.transport.readIntake).toHaveBeenLastCalledWith('solar', 3)
    expect(p.transport.readIntake).toHaveBeenCalledTimes(2)
    expect(panelProps.onAccept({ expected_rev: 12, report_artifact_id: F3_REPORT_ID })).toBe(false)
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.report_stale)
    expect(f3AcceptButton().disabled).toBe(true)
    expect(p.onStageSolarEdgeAccept).not.toHaveBeenCalled()
    expect(client.requestReport).toHaveBeenCalledTimes(1)
  })

  it('F3 30 a report rebuilt after the return stages once', async () => {
    const { p, client } = f3Props()
    let panelProps
    const Panel = solarEdgePanels.default
    vi.spyOn(solarEdgePanels, 'default').mockImplementation((props) => {
      panelProps = props
      return <Panel {...props} />
    })
    const view = render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Ready()
    view.rerender(<SolarWorkspaceTools {...p} drawingVersion={4} />)
    await waitFor(() => expect(panelProps.graphRev).toBe(12))
    expect(p.transport.readIntake).toHaveBeenLastCalledWith('solar', 4)
    view.rerender(<SolarWorkspaceTools {...p} drawingVersion={3} />)
    await waitFor(() => expect(panelProps.graphRev).toBe(12))
    expect(p.transport.readIntake).toHaveBeenLastCalledWith('solar', 3)
    await f3Build()
    await waitFor(() => expect(f3AcceptButton().disabled).toBe(false))
    fireEvent.click(f3AcceptButton())
    expect(p.onStageSolarEdgeAccept).toHaveBeenCalledExactlyOnceWith({ expected_rev: 12, report_artifact_id: F3_REPORT_ID })
    expect(client.requestReport).toHaveBeenCalledTimes(2)
  })

  it('F3 23 binds a report for another version as stale', async () => {
    const { p, client } = f3Props()
    const value = f3Report()
    value.source_version = 4
    value.report.source_version = 4
    client.requestReport.mockResolvedValueOnce(f3Success(value))
    let panelProps
    const Panel = solarEdgePanels.default
    vi.spyOn(solarEdgePanels, 'default').mockImplementation((props) => {
      panelProps = props
      return <Panel {...props} />
    })
    render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Upload(); await f3Build()
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.report_stale)
    expect(f3AcceptButton().disabled).toBe(true)
    expect(p.transport.readIntake).not.toHaveBeenCalled()
    expect(panelProps.onAccept({ expected_rev: 12, report_artifact_id: F3_REPORT_ID })).toBe(false)
    expect(p.onStageSolarEdgeAccept).not.toHaveBeenCalled()
  })

  it('F3 24 keeps the open panel when its trigger is clicked again', async () => {
    const { p } = f3Props()
    render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Ready()
    const file = f3File().files[0]
    const review = screen.getByRole('region', { name: 'Report review' })
    f3Open()
    expect(document.activeElement).toBe(f3File())
    expect(f3File().files[0]).toBe(file)
    expect(screen.getByRole('region', { name: 'Report review' })).toBe(review)
    fireEvent.click(f3AcceptButton())
    expect(p.onStageSolarEdgeAccept).toHaveBeenCalledExactlyOnceWith({ expected_rev: 12, report_artifact_id: F3_REPORT_ID })
  })

  it('F3 25 returns focus to the reason when the trigger is disabled on close', () => {
    const { p } = f3Props()
    const view = render(<SolarWorkspaceTools {...p} />); f3Open()
    view.rerender(<SolarWorkspaceTools {...p} busy={true} />)
    expect(f3Trigger().disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(document.activeElement).toBe(screen.getByText(SOLAREDGE_WORKSPACE_REASONS.run_in_progress))
    expect(document.activeElement.tagName).toBe('P')
    expect(document.activeElement).not.toBe(document.body)
  })

  it('F3 01 mounts only the registered SolarEdge flow', () => {
    const { p } = f3Props()
    const view = render(<SolarWorkspaceTools {...p} />)
    for (const drawingId of [null, 'solar']) for (const projectId of [null, TERRAIN_PROJECT]) {
      for (const flow of ['rooftop', 'ground-electrical', 'ground-physical', 'solaredge-import', 'pvcase-tutorial']) {
        view.rerender(<SolarWorkspaceTools {...p} drawingId={drawingId} projectId={projectId} flow={flow} />)
        expect(screen.queryAllByRole('button', { name: 'Import SolarEdge PDF' }))
          .toHaveLength(drawingId && flow === 'solaredge-import' ? 1 : 0)
        expect(screen.queryByRole('region', { name: 'SolarEdge PDF import' })).toBeNull()
        if (drawingId && flow === 'solaredge-import') {
          expect(screen.getAllByRole('region', { name: 'Solar workspace tools' })).toHaveLength(1)
        }
      }
    }
    expect(p.transport.fetchImpl).not.toHaveBeenCalled()
    expect(p.transport.readIntake).not.toHaveBeenCalled()
  })

  it('F3 02 opens and closes without requests', () => {
    const { p, client } = f3Props()
    render(<SolarWorkspaceTools {...p} />)
    f3Open()
    expect(document.activeElement).toBe(f3File())
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.revision_needed)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(document.activeElement).toBe(f3Trigger())
    expect(screen.queryByRole('region', { name: 'SolarEdge PDF import' })).toBeNull()
    expect(p.transport.fetchImpl).not.toHaveBeenCalled()
    expect(p.transport.readIntake).not.toHaveBeenCalled()
    expect(client.uploadPdf).not.toHaveBeenCalled()
    expect(client.requestReport).not.toHaveBeenCalled()
  })

  it('F3 03 discloses the panel group prerequisite', () => {
    const { p } = f3Props()
    render(<SolarWorkspaceTools {...p} />); f3Open()
    const prerequisite = screen.getByText('Create panel groups before matching the PDF')
    expect(prerequisite.compareDocumentPosition(f3UploadButton()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(f3File().files).toHaveLength(0)
    expect(p.transport.readIntake).not.toHaveBeenCalled()
  })

  it('F3 04 leaves upload and report usable without staging', async () => {
    const { p, client } = f3Props({ onStageSolarEdgeAccept: undefined })
    render(<SolarWorkspaceTools {...p} />); f3Open()
    await f3Upload(); await f3Build()
    await waitFor(() => expect(p.transport.readIntake).toHaveBeenCalledTimes(1))
    expect(client.uploadPdf).toHaveBeenCalledTimes(1)
    expect(client.requestReport).toHaveBeenCalledTimes(1)
    expect(f3UploadButton().disabled).toBe(false)
    expect(f3BuildButton().disabled).toBe(false)
    expect(f3AcceptButton().disabled).toBe(true)
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.staging_unavailable)
  })

  it('F3 05 uses fresh sanitized checkout headers', async () => {
    let cap = 'first', bearer = 'Bearer first'
    const { p } = f3Props({ getCheckoutCapability: () => cap }, true)
    p.transport.headers = () => ({ 'X-Tenant-Id': 'workspace-test', Authorization: bearer,
      'x-checkout-capability': 'stale', 'X-CHECKOUT-CAPABILITY': 'also-stale', 'X-Other': 'retained' })
    render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Upload()
    cap = 'second'; bearer = 'Bearer second'
    await f3Build()
    cap = ''; bearer = 'Bearer third'
    fireEvent.click(f3UploadButton())
    await waitFor(() => expect(p.transport.fetchImpl).toHaveBeenCalledTimes(3))
    const writes = p.transport.fetchImpl.mock.calls
    for (const [index, expected] of ['first', 'second', null].entries()) {
      const headers = writes[index][1].headers
      const keys = Object.keys(headers).filter((key) => key.toLowerCase() === 'x-checkout-capability')
      expect(keys).toEqual(expected === null ? [] : ['X-Checkout-Capability'])
      if (expected) expect(headers['X-Checkout-Capability']).toBe(expected)
      expect(headers.Authorization).toBe(['Bearer first', 'Bearer second', 'Bearer third'][index])
      expect(headers['X-Other']).toBe('retained')
    }
  })

  it('F3 06 pins common gate precedence', async () => {
    const { p, client } = f3Props()
    const view = render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Ready()
    const handlers = [f3Handler(f3UploadButton()), f3Handler(f3BuildButton()), f3Handler(f3AcceptButton())]
    for (const [props, sentence] of [
      [{ busy: true, checkoutHeld: false }, SOLAREDGE_WORKSPACE_REASONS.run_in_progress],
      [{ busy: false, checkoutHeld: false }, SOLAREDGE_WORKSPACE_REASONS.checkout_required],
    ]) {
      view.rerender(<SolarWorkspaceTools {...p} {...props} />)
      act(() => handlers.forEach((handler) => handler()))
      f3Reason(sentence)
    }
    expect(client.uploadPdf).toHaveBeenCalledTimes(1)
    expect(client.requestReport).toHaveBeenCalledTimes(1)
    expect(p.onStageSolarEdgeAccept).not.toHaveBeenCalled()
    const trackerWork = workspaceDeferred()
    const trackerTransport = p.transport.fetchImpl.getMockImplementation()
    p.transport.fetchImpl.mockImplementation((url, init) => url.includes('/tracker-rows')
      ? trackerWork.promise : trackerTransport(url, init))
    view.rerender(<SolarWorkspaceTools {...p} flow="ground-physical" />)
    await openTracker(); fillTracker(); fireEvent.click(trackerPublish())
    await waitFor(() => expect(trackerPosts(p)).toHaveLength(1))
    view.rerender(<SolarWorkspaceTools {...p} busy={true} checkoutHeld={false} />)
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.tracker_pending)
    act(() => handlers.forEach((handler) => handler()))
    expect(client.uploadPdf).toHaveBeenCalledTimes(1)
    expect(client.requestReport).toHaveBeenCalledTimes(1)
    expect(p.onStageSolarEdgeAccept).not.toHaveBeenCalled()
    await act(async () => trackerWork.resolve(terrainResponse(trackerAnswer(), 201)))
    cleanup(); vi.restoreAllMocks()
    const refresh = workspaceDeferred()
    const next = f3Props({ flow: 'rooftop', drawingVersion: 1,
      onDrawingVersionChanged: vi.fn(() => refresh.promise) })
    const combinerView = render(<SolarWorkspaceTools {...next.p} />)
    openCombiner(); chooseCombiner(); await importCombiner()
    combinerView.rerender(<SolarWorkspaceTools {...next.p} flow="solaredge-import" busy={true} checkoutHeld={false} />)
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.run_in_progress)
    combinerView.rerender(<SolarWorkspaceTools {...next.p} flow="solaredge-import" checkoutHeld={false} />)
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.checkout_required)
    combinerView.rerender(<SolarWorkspaceTools {...next.p} flow="solaredge-import" />)
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.refreshing)
    await act(async () => refresh.resolve(false))
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.refresh_failed)
    expect(f3Trigger().disabled).toBe(true)
    expect(next.client.uploadPdf).not.toHaveBeenCalled()
    expect(next.p.onStageSolarEdgeAccept).not.toHaveBeenCalled()
  })

  it('F3 07 blocks all SolarEdge actions behind tracker work', async () => {
    const held = workspaceDeferred()
    const { p, client } = f3Props()
    let panelProps
    let trackerProps
    const TrackerPanel = trackerPanels.default
    vi.spyOn(trackerPanels, 'default').mockImplementation((props) => {
      trackerProps = props
      return <TrackerPanel {...props} />
    })
    const Panel = solarEdgePanels.default
    vi.spyOn(solarEdgePanels, 'default').mockImplementation((props) => {
      panelProps = props
      return <Panel {...props} />
    })
    const view = render(<SolarWorkspaceTools {...p} flow="ground-physical" />)
    await openTracker(); fillTracker()
    const real = p.transport.fetchImpl.getMockImplementation()
    p.transport.fetchImpl.mockImplementation((url, init) => url.includes('/tracker-rows') ? held.promise : real(url, init))
    view.rerender(<SolarWorkspaceTools {...p} />); f3Open(); await f3Ready()
    let publication
    act(() => {
      publication = trackerProps.client.createTrackerRows({ drawingId: 'solar', projectId: p.projectId,
        drawingUnits: 'm', request: trackerM1(), signal: new AbortController().signal })
    })
    await waitFor(() => expect(trackerPosts(p)).toHaveLength(1))
    const capturedPanel = panelProps
    const handlers = [f3Handler(f3UploadButton()), f3Handler(f3BuildButton()), f3Handler(f3AcceptButton())]
    act(() => handlers.forEach((handler) => handler()))
    await act(async () => {
      for (const method of ['uploadPdf', 'requestReport']) {
        expect(await capturedPanel.client[method]({ drawingId: 'solar', projectId: null,
          file: new File(['%PDF'], 'layout.pdf'), sourceArtifactId: F3_SOURCE_ID,
          alignmentTolerance: 0.5, selectionOrder: 'recorded', signal: new AbortController().signal }))
          .toEqual({ ok: false, code: 'SOLAREDGE_CLIENT_REQUEST_INVALID', retryable: false, status: null })
      }
      expect(capturedPanel.onAccept({ expected_rev: 12, report_artifact_id: F3_REPORT_ID })).toBe(false)
    })
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.tracker_pending)
    expect(f3Trigger().disabled).toBe(true)
    expect(client.uploadPdf).toHaveBeenCalledTimes(1)
    expect(client.requestReport).toHaveBeenCalledTimes(1)
    expect(p.onStageSolarEdgeAccept).not.toHaveBeenCalled()
    await act(async () => held.resolve(terrainResponse(trackerAnswer(), 201)))
    await publication
  })

  it('F3 08 interlocks tracker creation behind either SolarEdge write', async () => {
    for (const step of ['upload', 'report']) {
      const { p, client } = f3Props({ flow: 'ground-physical' })
      const view = render(<SolarWorkspaceTools {...p} />)
      await openTracker(); fillTracker()
      const publish = f3Handler(trackerPublish())
      view.rerender(<SolarWorkspaceTools {...p} flow="solaredge-import" />); f3Open()
      const held = workspaceDeferred()
      if (step === 'report') {
        await f3Upload()
        client.requestReport.mockReturnValueOnce(held.promise)
        fireEvent.change(screen.getByLabelText('Alignment tolerance'), { target: { value: '0.5' } })
        fireEvent.click(f3BuildButton())
      } else {
        client.uploadPdf.mockReturnValueOnce(held.promise)
        f3Choose(); fireEvent.click(f3UploadButton())
      }
      view.rerender(<SolarWorkspaceTools {...p} />)
      expect(trackerTrigger().disabled).toBe(true)
      f3Reason(SOLAREDGE_WORKSPACE_REASONS.solaredge_pending)
      expect(trackerPanel().closest('[data-flow-stage]').hidden).toBe(true)
      act(() => { publish({ currentTarget: null }) })
      expect(trackerPosts(p)).toHaveLength(0)
      await act(async () => held.resolve(f3Success(step === 'upload' ? f3Source() : f3Report())))
      expect(trackerTrigger().disabled).toBe(false)
      fireEvent.click(trackerTrigger())
      await waitFor(() => expect(trackerPublish().disabled).toBe(false))
      expect(screen.getByLabelText('Module power').value).toBe('450')
      fireEvent.click(trackerPublish()); await trackerPublished()
      expect(trackerPosts(p)).toHaveLength(1)
      cleanup(); vi.restoreAllMocks()
    }
  })

  it('F3 09 releases only the matching request token', async () => {
    const helds = [workspaceDeferred(), workspaceDeferred()]
    const { p, client } = f3Props()
    client.uploadPdf.mockReturnValueOnce(helds[0].promise).mockReturnValueOnce(helds[1].promise)
    const view = render(<SolarWorkspaceTools {...p} />); f3Open(); f3Choose(); fireEvent.click(f3UploadButton())
    act(() => { f3Handler(f3UploadButton())() })
    expect(client.uploadPdf).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('status').textContent).toBe(solarEdgePanels.SOLAREDGE_PANEL_REASONS.request_pending)
    const oldSignal = client.uploadPdf.mock.calls[0][0].signal
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(oldSignal.aborted).toBe(true)
    f3Open(); f3Choose(); fireEvent.click(f3UploadButton())
    view.rerender(<SolarWorkspaceTools {...p} flow="ground-physical" />)
    expect(trackerTrigger().disabled).toBe(true)
    await act(async () => helds[0].resolve(f3Success(f3Source())))
    expect(trackerTrigger().disabled).toBe(true)
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.solaredge_pending)
    await act(async () => helds[1].resolve(f3Success(f3Source())))
    expect(trackerTrigger().disabled).toBe(false)
    expect(client.uploadPdf).toHaveBeenCalledTimes(2)
  })

  it('F3 10 reads the exact version after report publication', async () => {
    const { p } = f3Props()
    const held = workspaceDeferred()
    p.transport.readIntake.mockReturnValueOnce(held.promise)
    render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Upload()
    expect(p.transport.readIntake).not.toHaveBeenCalled()
    await f3Build()
    expect(p.transport.readIntake).toHaveBeenCalledExactlyOnceWith('solar', 3)
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.revision_loading)
    expect(f3AcceptButton().disabled).toBe(true)
    await act(async () => held.resolve({ version: 3, intake: { solar_design_graph: { rev: 12 } } }))
    fireEvent.click(f3AcceptButton())
    expect(p.onStageSolarEdgeAccept).toHaveBeenCalledExactlyOnceWith({
      expected_rev: 12, report_artifact_id: F3_REPORT_ID,
    })
  })

  it('F3 11 rejects invalid or mismatched intake revisions', async () => {
    const answers = [{ version: 4, intake: { solar_design_graph: { rev: 12 } } },
      ...[undefined, -1, 1.5, 2147483648].map((rev) => ({ version: 3, intake: { solar_design_graph: { rev } } })),
      new Error('read')]
    for (const answer of answers) {
      const { p } = f3Props()
      p.transport.readIntake.mockImplementationOnce(() => answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer))
      render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Upload(); await f3Build()
      await screen.findByRole('button', { name: 'Retry design revision' })
      expect(f3AcceptButton().disabled).toBe(true)
      f3Reason(SOLAREDGE_WORKSPACE_REASONS.revision_unavailable)
      act(() => { f3Handler(f3AcceptButton())() })
      expect(p.onStageSolarEdgeAccept).not.toHaveBeenCalled()
      cleanup(); vi.restoreAllMocks()
    }
    for (const drawingVersion of [undefined, -1, 0.5]) {
      const { p } = f3Props({ drawingVersion })
      render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Upload(); await f3Build()
      f3Reason(SOLAREDGE_WORKSPACE_REASONS.revision_unavailable)
      expect(p.transport.readIntake).not.toHaveBeenCalled()
      expect(f3AcceptButton().disabled).toBe(true)
      cleanup(); vi.restoreAllMocks()
    }
  })

  it('F3 12 ignores late intake across scope and version changes', async () => {
    const { p } = f3Props()
    const helds = [workspaceDeferred(), workspaceDeferred(), workspaceDeferred()]
    p.transport.readIntake.mockReturnValueOnce(helds[0].promise)
      .mockReturnValueOnce(helds[1].promise).mockReturnValueOnce(helds[2].promise)
    const view = render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Upload(); await f3Build()
    view.rerender(<SolarWorkspaceTools {...p} drawingVersion={4} />)
    expect(p.transport.readIntake).toHaveBeenLastCalledWith('solar', 4)
    view.rerender(<SolarWorkspaceTools {...p} drawingId="new" projectId="new-project" drawingVersion={4} />)
    expect(screen.queryByRole('region', { name: 'SolarEdge PDF import' })).toBeNull()
    view.rerender(<SolarWorkspaceTools {...p} />); f3Open(); await f3Upload(); await f3Build()
    await act(async () => {
      helds[0].resolve({ version: 3, intake: { solar_design_graph: { rev: 100 } } })
      helds[1].resolve({ version: 4, intake: { solar_design_graph: { rev: 200 } } })
    })
    expect(f3AcceptButton().disabled).toBe(true)
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.revision_loading)
    await act(async () => helds[2].resolve({ version: 3, intake: { solar_design_graph: { rev: 12 } } }))
    fireEvent.click(f3AcceptButton())
    expect(p.onStageSolarEdgeAccept).toHaveBeenCalledExactlyOnceWith({ expected_rev: 12, report_artifact_id: F3_REPORT_ID })
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    view.rerender(<SolarWorkspaceTools {...p} drawingVersion={5} />)
    expect(p.transport.readIntake).toHaveBeenCalledTimes(3)
    cleanup(); vi.restoreAllMocks()
    const next = f3Props()
    const oldRead = workspaceDeferred()
    const currentRead = workspaceDeferred()
    next.p.transport.readIntake.mockReturnValueOnce(oldRead.promise).mockReturnValueOnce(currentRead.promise)
      .mockResolvedValue({ version: 4, intake: { solar_design_graph: { rev: 44 } } })
    let panelProps
    const Panel = solarEdgePanels.default
    vi.spyOn(solarEdgePanels, 'default').mockImplementation((props) => {
      panelProps = props
      return <Panel {...props} />
    })
    const sameScope = render(<SolarWorkspaceTools {...next.p} />); f3Open(); await f3Upload(); await f3Build()
    sameScope.rerender(<SolarWorkspaceTools {...next.p} drawingVersion={4} />)
    expect(next.p.transport.readIntake).toHaveBeenLastCalledWith('solar', 4)
    await act(async () => currentRead.resolve({ version: 4, intake: { solar_design_graph: { rev: 44 } } }))
    expect(panelProps.graphRev).toBe(44)
    await act(async () => oldRead.resolve({ version: 3, intake: { solar_design_graph: { rev: 100 } } }))
    expect(panelProps.graphRev).toBe(44)
    expect(f3AcceptButton().disabled).toBe(true)
    const value = f3Report()
    value.source_version = 4
    value.report.source_version = 4
    next.client.requestReport.mockResolvedValueOnce(f3Success(value))
    await f3Build()
    await waitFor(() => expect(f3AcceptButton().disabled).toBe(false))
    expect(panelProps.graphRev).toBe(44)
    fireEvent.click(f3AcceptButton())
    expect(next.p.onStageSolarEdgeAccept).toHaveBeenCalledExactlyOnceWith({ expected_rev: 44, report_artifact_id: F3_REPORT_ID })
  })

  it('F3 13 refreshes revision and invalidates old report on version change', async () => {
    const { p, client } = f3Props()
    const view = render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Ready()
    const file = f3File().files[0]
    fireEvent.change(screen.getByLabelText('Selection order'), { target: { value: 'recorded' } })
    const review = screen.getByRole('region', { name: 'Report review' })
    view.rerender(<SolarWorkspaceTools {...p} drawingVersion={4} />)
    await waitFor(() => expect(p.transport.readIntake).toHaveBeenCalledTimes(2))
    expect(p.transport.readIntake).toHaveBeenLastCalledWith('solar', 4)
    expect(screen.getByRole('region', { name: 'Report review' })).toBe(review)
    expect(f3File().files[0]).toBe(file)
    expect(screen.getByLabelText('Alignment tolerance').value).toBe('0.5')
    expect(screen.getByLabelText('Selection order').value).toBe('recorded')
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.report_stale)
    expect(f3AcceptButton().disabled).toBe(true)
    const held = workspaceDeferred()
    client.requestReport.mockReturnValueOnce(held.promise)
    fireEvent.click(f3BuildButton())
    const signal = client.requestReport.mock.calls.at(-1)[0].signal
    view.rerender(<SolarWorkspaceTools {...p} drawingVersion={5} />)
    expect(signal.aborted).toBe(true)
    expect(p.transport.readIntake).toHaveBeenLastCalledWith('solar', 5)
    await act(async () => held.resolve(f3Success(f3Report())))
    expect(screen.getByRole('region', { name: 'Report review' })).toBeTruthy()
    f3Reason(SOLAREDGE_WORKSPACE_REASONS.report_stale)
    expect(f3AcceptButton().disabled).toBe(true)
    const currentReport = f3Report()
    currentReport.source_version = 5
    currentReport.report.source_version = 5
    client.requestReport.mockResolvedValueOnce(f3Success(currentReport))
    await f3Build()
    await waitFor(() => expect(f3AcceptButton().disabled).toBe(false))
    fireEvent.click(f3AcceptButton())
    expect(p.onStageSolarEdgeAccept).toHaveBeenCalledTimes(1)
  })

  it('F3 14 retries revision reads without repeating writes', async () => {
    const { p, client } = f3Props()
    p.transport.readIntake.mockRejectedValueOnce(new Error('read'))
    render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Upload(); await f3Build()
    fireEvent.click(await screen.findByRole('button', { name: 'Retry design revision' }))
    await waitFor(() => expect(f3AcceptButton().disabled).toBe(false))
    expect(p.transport.readIntake.mock.calls).toEqual([['solar', 3], ['solar', 3]])
    expect(client.uploadPdf).toHaveBeenCalledTimes(1)
    expect(client.requestReport).toHaveBeenCalledTimes(1)
    expect(p.onStageSolarEdgeAccept).not.toHaveBeenCalled()
  })

  it('F3 15 never refreshes drawing or physical head for artifacts', async () => {
    const { p } = f3Props()
    render(<SolarWorkspaceTools {...p} />); f3Open(); await f3Ready()
    expect(p.onDrawingVersionChanged).not.toHaveBeenCalled()
    expect(p.onPhysicalHeadChanged).not.toHaveBeenCalled()
    expect(screen.getByRole('region', { name: 'Report review' })).toBeTruthy()
    fireEvent.click(f3AcceptButton())
    expect(p.onDrawingVersionChanged).not.toHaveBeenCalled()
    expect(p.onPhysicalHeadChanged).not.toHaveBeenCalled()
  })

  it('F3 16 preserves the guest refusal', async () => {
    const { p } = f3Props({}, true)
    const create = vi.spyOn(importClients, 'createSolarImportClient')
    const view = render(<SolarWorkspaceTools {...p} />); f3Open()
    const guest = () => terrainResponse({ error: { error_code: 'FORBIDDEN' }, degraded_mode: false }, 403)
    p.transport.fetchImpl.mockResolvedValueOnce(guest())
    const file = f3Choose(); fireEvent.click(f3UploadButton())
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe(
      'Upload failed. A guest session cannot use the SolarEdge import, so sign in to an account'))
    expect(f3File().files[0]).toBe(file)
    fireEvent.click(f3UploadButton())
    await screen.findByLabelText('Alignment tolerance')
    p.transport.fetchImpl.mockResolvedValueOnce(guest())
    fireEvent.change(screen.getByLabelText('Selection order'), { target: { value: 'recorded' } })
    fireEvent.change(screen.getByLabelText('Alignment tolerance'), { target: { value: '0.5' } })
    fireEvent.click(f3BuildButton())
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe(
      'Report failed. A guest session cannot use the SolarEdge import, so sign in to an account'))
    expect(screen.getByLabelText('Alignment tolerance').value).toBe('0.5')
    expect(screen.getByLabelText('Selection order').value).toBe('recorded')
    expect(f3File().files[0]).toBe(file)
    expect(p.transport.readIntake).not.toHaveBeenCalled()
    expect(p.onStageSolarEdgeAccept).not.toHaveBeenCalled()
    expect(p.onDrawingVersionChanged).not.toHaveBeenCalled()
    expect(p.onPhysicalHeadChanged).not.toHaveBeenCalled()
    const realClient = create.mock.results[0].value
    view.unmount()
    for (const method of ['uploadPdf', 'requestReport']) {
      p.transport.fetchImpl.mockResolvedValueOnce(guest())
      const refused = await realClient[method]({ drawingId: 'solar', projectId: null, file,
        sourceArtifactId: F3_SOURCE_ID, alignmentTolerance: 0.5, selectionOrder: 'recorded' })
      expect(refused).toMatchObject({ ok: false, code: 'FORBIDDEN', retryable: false, status: 403 })
    }
  })
})

const COMBINER_VALUE = {
  schema_version: 'leaf.solar-combiner-intake-import.v1', drawing_id: 'solar',
  project_id: 'leaf:project:00000000-0000-4000-8000-000000000001',
  created: true, version: 2, parent_version: 1, graph_rev: 7,
  graph_sha256: 'a'.repeat(64), bound: { l2_inverters: 2, strings: 4 },
  panel_groups: 1, outline_vertices: 4, intake_sha256: 'b'.repeat(64),
  combiner_intake_sha256: 'c'.repeat(64), panel_groups_sha256: 'd'.repeat(64),
}
function combinerResponse(overrides = {}) {
  return new Response(JSON.stringify({ ...COMBINER_VALUE, ...overrides }), {
    status: 200, headers: { 'content-type': 'application/json' },
  })
}
function combinerProps(overrides = {}) {
  const props = supplied({ flow: 'rooftop', onRunPlacement: vi.fn(() => true), ...overrides })
  props.transport?.fetchImpl.mockImplementation(async () => combinerResponse())
  return props
}
const combinerTrigger = () => screen.getByRole('button', { name: 'Import combiner intake' })
const combinerFile = () => screen.getByLabelText('Combiner intake file')
const combinerImport = () => screen.getByRole('button', { name: 'Import' })
const combinerRun = () => screen.getByRole('button', { name: 'Run placement' })
const combinerReason = () => screen.queryByTestId('solar-combiner-reason')
function chooseCombiner() {
  const file = new File(['{}'], 'intake.json', { type: 'application/json' })
  fireEvent.change(combinerFile(), { target: { files: [file] } })
  return file
}
function combinerHardware() {
  for (const [label, value] of [['Model', 'Combiner'], ['DC voltage', '480'], ['AC power', '10']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
}
function openCombiner() { fireEvent.click(combinerTrigger()) }
async function importCombiner() {
  fireEvent.click(combinerImport())
  await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Combiner intake imported for this drawing.'))
}
function workspaceDeferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function value(created = true) {
  const result = JSON.parse(FIRST_TEXT)
  result.created = created
  delete result.error
  delete result.degraded_mode
  return result
}
function response(created = true) {
  return new Response(JSON.stringify(value(created)), { status: 200, headers: { 'content-type': 'application/json' } })
}
function supplied(overrides = {}) {
  return {
    drawingId: 'solar', projectId: null, drawingVersion: 1, flow: 'ground-physical',
    checkoutHeld: true, busy: false, getCheckoutCapability: vi.fn(),
    onPhysicalHeadChanged: vi.fn(), onDrawingVersionChanged: vi.fn(),
    transport: {
      fetchImpl: vi.fn(async () => response()),
      headers: vi.fn(() => ({ 'X-Tenant-Id': 'workspace-test' })),
      onResponse: vi.fn(),
    },
    ...overrides,
  }
}
const trigger = () => screen.getByRole('button', { name: 'Import LandXML terrain' })
const control = () => screen.queryByTestId('solar-landxml-upload')
const announcement = () => document.querySelector('.solar-workspace-announce')?.textContent ?? ''
function open() { fireEvent.click(trigger()) }
function submit() {
  fireEvent.change(screen.getByLabelText('Drawing units'), { target: { value: 'm' } })
  fireEvent.change(screen.getByLabelText('LandXML file'), {
    target: { files: [new File([new Uint8Array(45305)], 'site.xml', { type: 'application/xml' })] },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Import terrain' }))
}
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.removeItem('leaf.jwt')
})

// Synthetic route-shaped terrain fixtures, validated by the real client and panel.
const TERRAIN_H1 = value().head.state.artifact_id
const TERRAIN_H2 = 'b'.repeat(64)
const TERRAIN_PROJECT = value().project_id
function terrainHead(moved = false) {
  const head = value().head
  if (moved) {
    head.index = 1
    head.parent = TERRAIN_H1
    head.state.artifact_id = TERRAIN_H2
    head.state.download = `/api/drawings/solar/artifacts/${TERRAIN_H2}`
  }
  return head
}
const terrainFrame = () => ({
  coordinate_system: 'world', transform: 'identity', drawing_units: 'm', meters_per_unit: 1, crs: 'none',
  elevation_datum: 'unrecorded', horizontal: 'drawing-units', elevation: 'metres',
})
const terrainGrid = () => ({
  rows: 2, cols: 2, x_min: 0, x_max: 10, y_min: 0, y_max: 10,
  cell_x: 10, cell_y: 10, cell_x_m: 10, cell_y_m: 10,
  elevation_min_m: 0, elevation_max_m: 0, grid_sha256: 'c'.repeat(64),
})
function terrainView(moved = false) {
  return {
    schema: 'leaf.solar-terrain-view-response.v1', stored: true, head: terrainHead(moved),
    terrain: {
      schema: 'leaf.solar-terrain-view.v1', maturity: 'preview', drawing_id: 'solar', project_id: TERRAIN_PROJECT,
      frame: terrainFrame(), grid: terrainGrid(), mesh_faces: 0, slope_markers: 0,
      previews: {
        'terrain-mesh-render': { state: 'absent', record: null },
        'tracker-slope-violations': { state: 'absent', record: null },
      },
    },
    error: null, degraded_mode: false,
  }
}
function terrainResult(created = true) {
  return {
    schema: 'leaf.solar-terrain-operation.v1', maturity: 'preview', operation: 'mesh',
    capability: 'terrain-mesh-render', created, drawing_id: 'solar', project_id: TERRAIN_PROJECT,
    frame: terrainFrame(), grid: terrainGrid(), head: terrainHead(created), replaced: 0,
    record: {
      schema: 'leaf.solar-terrain-preview.v1', capability: 'terrain-mesh-render', maturity: 'preview',
      grid_sha256: 'c'.repeat(64), meters_per_unit: 1, faces: 1,
      buckets: { Green: 1, Yellow: 0, Red: 0 }, max_slope_percent: 0, mesh_sha256: 'e'.repeat(64),
    },
    error: null, degraded_mode: false,
  }
}
function terrainResponse(body = terrainView(), status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}
function terrainProps(overrides = {}) {
  const props = supplied(overrides)
  let moved = false
  props.transport.fetchImpl.mockImplementation(async (url, init) => {
    if (url.includes('/imports/landxml')) return response()
    if (init.method === 'POST') {
      moved = true
      return terrainResponse(terrainResult())
    }
    return terrainResponse(terrainView(moved))
  })
  return props
}
const terrainTrigger = () => screen.getByRole('button', { name: 'Terrain preview', exact: true })
const terrainPanel = () => screen.queryByTestId('solar-terrain-panel')
const terrainMesh = () => screen.getByRole('button', { name: 'Run mesh preview' })
const openTerrain = () => fireEvent.click(terrainTrigger())
const terrainReady = () => waitFor(() => expect(terrainPanel()?.getAttribute('data-phase')).toBe('ready'))
const terrainGets = (props) => props.transport.fetchImpl.mock.calls.filter(([, init]) => init.method === 'GET')
const terrainPosts = (props) => props.transport.fetchImpl.mock.calls.filter(([url, init]) =>
  init.method === 'POST' && url.includes('/terrain/operations'))

const trackerM1 = () => ({ operation: 'manual-create', rows: [
  { axis_start: [0, 0], axis_end: [0, 6], cross_axis_width_du: 2, slots: 3 },
  { axis_start: [4, 0], axis_end: [4, 10], cross_axis_width_du: 1, slots: 2 },
], module_power_watts: 450, expected_head: null })
function trackerAnswer(request = trackerM1(), status = 201, projectId = TERRAIN_PROJECT) {
  const head = terrainHead()
  head.parent = request.expected_head
  head.index = request.expected_head === null ? 0 : 1
  head.project_id = projectId
  return { schema: 'leaf.solar-tracker-rows.v1', operation: 'manual-create', outcome: status === 201 ? 'published' : 'retry',
    created: status === 201, drawing_id: 'solar', project_id: projectId, expected_head: request.expected_head,
    head, frame: terrainFrame(), summary: { rows: request.rows.length,
      slots: request.rows.reduce((sum, row) => sum + row.slots, 0), module_power_watts: request.module_power_watts },
    terrain_standing: { schema: 'leaf.solar-frames-piles-terrain-standing.v1', maturity: 'preview', grid_sha256: null,
      frames: { state: 'absent', checked: 0, stale: 0 }, piles: { state: 'absent', checked: 0, stale: 0 } },
    error: null, degraded_mode: false }
}
const trackerTrigger = () => screen.getByRole('button', { name: 'Create tracker rows', exact: true })
const trackerPanel = () => screen.getByTestId('solar-tracker-rows-panel')
const trackerPublish = () => screen.getByRole('button', { name: 'Publish tracker rows' })
const trackerRefresh = () => screen.getByRole('button', { name: 'Refresh physical state' })
const trackerPosts = (p) => p.transport.fetchImpl.mock.calls.filter(([url, init]) =>
  init.method === 'POST' && url.includes('/tracker-rows'))
function trackerProps(overrides = {}) {
  const p = supplied(overrides)
  let published = false
  p.transport.readIntake = vi.fn(async () => ({ version: p.drawingVersion, head: p.drawingVersion,
    intake: { solar_design_graph: { project: { id: p.projectId ?? TERRAIN_PROJECT,
      units: { drawing_units: 'm', meters_per_unit: 1 } } } } }))
  p.transport.fetchImpl.mockImplementation(async (url, init) => {
    if (url.includes('/tracker-rows')) {
      published = true
      return terrainResponse(trackerAnswer(JSON.parse(init.body), 201, p.projectId ?? TERRAIN_PROJECT), 201)
    }
    if (url.includes('/terrain') && init.method === 'GET') {
      if (!published) return terrainResponse({ schema: 'leaf.solar-terrain-view-response.v1', stored: false,
        head: null, terrain: null, error: null, degraded_mode: false })
      const view = terrainView(); view.terrain.grid = null
      view.head.project_id = p.projectId ?? TERRAIN_PROJECT
      view.terrain.project_id = view.head.project_id
      return terrainResponse(view)
    }
    if (url.includes('/imports/combiner-intake')) return combinerResponse()
    return response()
  })
  return p
}
async function openTracker() {
  fireEvent.click(trackerTrigger())
  await waitFor(() => expect(trackerPanel().getAttribute('data-phase')).toBe('invalid'))
}
function fillTracker() {
  fireEvent.change(screen.getByLabelText('Module power'), { target: { value: '450' } })
  for (let index = 0; index < 2; index += 1) {
    if (index) fireEvent.click(screen.getByRole('button', { name: 'Add row' }))
    const inputs = trackerPanel().querySelectorAll('fieldset fieldset')[index].querySelectorAll('input')
    const row = trackerM1().rows[index]
    const numbers = [...row.axis_start, ...row.axis_end, row.cross_axis_width_du, row.slots]
    inputs.forEach((input, i) => fireEvent.change(input, { target: { value: String(numbers[i]) } }))
  }
}
async function trackerPublished() {
  await waitFor(() => expect(trackerPanel().getAttribute('data-phase')).toBe('published'))
}

describe('tracker workspace integration', () => {
  it('c01_flow_panel_inventory', () => {
    const p = trackerProps()
    const view = render(<SolarWorkspaceTools {...p} />)
    for (const drawingId of [null, 'solar']) for (const projectId of [null, TERRAIN_PROJECT]) {
      for (const flow of ['rooftop', 'ground-physical', 'ground-electrical', 'solaredge-import', 'pvcase-tutorial']) {
        view.rerender(<SolarWorkspaceTools {...p} drawingId={drawingId} projectId={projectId} flow={flow} />)
        expect(!!screen.queryByRole('button', { name: 'Create tracker rows' })).toBe(!!drawingId && flow === 'ground-physical')
        expect(screen.queryByTestId('solar-tracker-rows-panel')).toBeNull()
      }
    }
    expect(p.transport.fetchImpl).not.toHaveBeenCalled()
    expect(p.transport.readIntake).not.toHaveBeenCalled()
  })
  it('c02_open_close_focus', async () => {
    const p = trackerProps()
    render(<SolarWorkspaceTools {...p} />)
    await openTracker()
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Tracker layout' }))
    fillTracker()
    open()
    expect(screen.queryByRole('region', { name: 'Tracker layout' })).toBeNull()
    expect(screen.getByLabelText('LandXML file')).toBe(document.activeElement)
    expect(screen.getByLabelText('Module power').value).toBe('450')
    fireEvent.click(trackerTrigger())
    await waitFor(() => expect(trackerPublish().disabled).toBe(false))
    expect(screen.queryByTestId('solar-landxml-upload')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(document.activeElement).toBe(trackerTrigger())
    expect(screen.queryByRole('region', { name: 'Tracker layout' })).toBeNull()
  })
  it('c03_default_transport', async () => {
    const p = trackerProps()
    delete p.transport.headers
    delete p.transport.onResponse
    const observer = vi.spyOn(api, 'noteUnauthorized').mockImplementation(() => {})
    const view = render(<SolarWorkspaceTools {...p} />)
    localStorage.setItem('leaf.jwt', 'tracker-a')
    await openTracker()
    const firstTenant = api.config.tenant
    const oldTenant = api.config.tenant
    try {
      api.config.tenant = 'tracker-new-tenant'
      localStorage.setItem('leaf.jwt', 'tracker-b')
      view.rerender(<SolarWorkspaceTools {...p} />)
      fillTracker()
      fireEvent.click(trackerPublish())
      await trackerPublished()
      const posts = trackerPosts(p)
      expect(posts).toHaveLength(1)
      expect(p.transport.fetchImpl.mock.calls[0][1].headers).toMatchObject({ 'X-Tenant-Id': firstTenant, Authorization: 'Bearer tracker-a' })
      expect(posts[0][1].headers).toMatchObject({ 'X-Tenant-Id': 'tracker-new-tenant', Authorization: 'Bearer tracker-b' })
      expect(observer.mock.calls.some(([, , sent]) => sent === 'Bearer tracker-b')).toBe(true)
    } finally { api.config.tenant = oldTenant }
  })
  it('c04_capability_replacement', async () => {
    for (const cap of ['current-proof', '']) {
      const p = trackerProps({ getCheckoutCapability: () => 'first-proof' })
      p.transport.headers = () => ({ 'X-Tenant-Id': 'workspace-test', 'x-checkout-capability': 'old-a',
        'X-CHECKOUT-CAPABILITY': 'old-b', 'x-Checkout-Capability': 'old-c' })
      const view = render(<SolarWorkspaceTools {...p} />)
      await openTracker()
      view.rerender(<SolarWorkspaceTools {...p} getCheckoutCapability={() => cap} />)
      fillTracker()
      fireEvent.click(trackerPublish())
      await trackerPublished()
      const [, init] = trackerPosts(p)[0]
      expect(Object.keys(init.headers).filter((key) => key.toLowerCase() === 'x-checkout-capability')).toEqual(cap ? ['X-Checkout-Capability'] : [])
      if (cap) expect(init.headers['X-Checkout-Capability']).toBe(cap)
      expect(JSON.parse(init.body)).toEqual(trackerM1())
      expect(init.body).not.toContain('proof')
      cleanup()
    }
  })
  it('c05_transport_override', async () => {
    const p = trackerProps()
    render(<SolarWorkspaceTools {...p} />)
    await openTracker()
    expect(p.transport.readIntake).toHaveBeenCalledExactlyOnceWith('solar', 'head')
    fillTracker()
    fireEvent.click(trackerPublish())
    await trackerPublished()
    expect(p.transport.headers).toHaveBeenCalledWith('solar')
    expect(p.transport.onResponse).toHaveBeenCalled()
    cleanup()
    for (const headers of [null, [], new Headers(), { Authorization: 42 }]) {
      const invalid = trackerProps()
      vi.spyOn(terrainClients, 'createSolarTerrainClient').mockReturnValue({
        getTerrain: vi.fn(async () => ({ ok: true, value: { schema: 'leaf.solar-terrain-view-response.v1',
          stored: false, head: null, terrain: null } })), runTerrainOperation: vi.fn(),
      })
      invalid.transport.headers = () => headers
      render(<SolarWorkspaceTools {...invalid} />)
      await openTracker()
      fillTracker()
      fireEvent.click(trackerPublish())
      await waitFor(() => expect(screen.getByRole('alert').textContent).toBe(trackerClients.TRACKER_ROWS_CLIENT_REASONS.TRACKER_ROWS_CLIENT_REQUEST_INVALID))
      expect(invalid.transport.fetchImpl).not.toHaveBeenCalled()
      cleanup(); vi.restoreAllMocks()
    }
  })
  it('c06_scope_binding', async () => {
    const p = trackerProps({ projectId: 'scope & project' })
    const view = render(<SolarWorkspaceTools {...p} />)
    await openTracker()
    fillTracker()
    fireEvent.click(trackerPublish())
    await trackerPublished()
    for (const [url] of p.transport.fetchImpl.mock.calls) expect(new URL(url, 'http://local').searchParams.get('project_id')).toBe('scope & project')
    expect(trackerPosts(p)[0][1].body).toBe(JSON.stringify(trackerM1()))
    view.rerender(<SolarWorkspaceTools {...p} drawingId="new" projectId="new-project" />)
    expect(screen.queryByTestId('solar-tracker-rows-panel')).toBeNull()
    expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
  })
  it('c07_landxml_blocks_tracker', async () => {
    const p = trackerProps()
    const held = workspaceDeferred()
    const real = p.transport.fetchImpl.getMockImplementation()
    p.transport.fetchImpl.mockImplementation((url, init) => url.includes('/imports/landxml') ? held.promise : real(url, init))
    render(<SolarWorkspaceTools {...p} />)
    open(); submit()
    await waitFor(() => expect(trackerTrigger().disabled).toBe(true))
    expect(screen.getByTestId('solar-tracker-workspace-reason').textContent).toBe(TRACKER_SENTENCES.landxml_pending)
    expect(trackerPosts(p)).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel import' }))
    await waitFor(() => expect(trackerTrigger().disabled).toBe(false))
    await act(async () => held.resolve(response()))
    expect(p.onPhysicalHeadChanged).not.toHaveBeenCalled()
  })
  it('c08_terrain_blocks_tracker', async () => {
    const p = terrainProps()
    p.transport.readIntake = vi.fn()
    const held = workspaceDeferred()
    const real = p.transport.fetchImpl.getMockImplementation()
    p.transport.fetchImpl.mockImplementation((url, init) => init.method === 'POST' ? held.promise : real(url, init))
    render(<SolarWorkspaceTools {...p} />)
    openTerrain(); await terrainReady()
    fireEvent.click(terrainMesh())
    await waitFor(() => expect(trackerTrigger().disabled).toBe(true))
    expect(screen.getByTestId('solar-tracker-workspace-reason').textContent).toBe(TRACKER_SENTENCES.terrain_pending)
    expect(trackerPosts(p)).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(trackerTrigger().disabled).toBe(false))
    openTerrain(); await terrainReady()
    const newer = workspaceDeferred()
    p.transport.fetchImpl.mockImplementation((url, init) => init.method === 'POST' ? newer.promise : real(url, init))
    fireEvent.click(terrainMesh())
    await waitFor(() => expect(trackerTrigger().disabled).toBe(true))
    await act(async () => held.resolve(terrainResponse(terrainResult())))
    expect(trackerTrigger().disabled).toBe(true)
    await act(async () => newer.resolve(terrainResponse(terrainResult())))
    await waitFor(() => expect(trackerTrigger().disabled).toBe(false))
  })
  it('c09_combiner_blocks_tracker', async () => {
    const p = trackerProps({ flow: 'rooftop' })
    const held = workspaceDeferred()
    const real = p.transport.fetchImpl.getMockImplementation()
    p.transport.fetchImpl.mockImplementation((url, init) => url.includes('/imports/combiner-intake') ? held.promise : real(url, init))
    const view = render(<SolarWorkspaceTools {...p} />)
    openCombiner(); chooseCombiner(); fireEvent.click(combinerImport())
    await waitFor(() => expect(p.transport.fetchImpl).toHaveBeenCalledTimes(1))
    view.rerender(<SolarWorkspaceTools {...p} flow="ground-physical" />)
    await waitFor(() => expect(trackerTrigger().disabled).toBe(false))
    await act(async () => held.resolve(combinerResponse()))
    expect(trackerPosts(p)).toHaveLength(0)
    expect(p.onDrawingVersionChanged).not.toHaveBeenCalled()
  })
  it('c10_combiner_refresh_blocks_tracker', async () => {
    for (const refreshResult of [undefined, false]) {
      const p = trackerProps({ flow: 'rooftop', onDrawingVersionChanged: vi.fn(() => refreshResult) })
      const view = render(<SolarWorkspaceTools {...p} />)
      openCombiner(); chooseCombiner(); await importCombiner()
      view.rerender(<SolarWorkspaceTools {...p} flow="ground-physical" />)
      expect(trackerTrigger().disabled).toBe(true)
      expect(screen.getByTestId('solar-tracker-workspace-reason').textContent).toBe(TRACKER_SENTENCES.drawing_refresh)
      view.rerender(<SolarWorkspaceTools {...p} drawingVersion={2} flow="ground-physical" />)
      await waitFor(() => expect(trackerTrigger().disabled).toBe(false))
      expect(trackerPosts(p)).toHaveLength(0)
      cleanup()
    }
  })
  it('c11_tracker_blocks_others', async () => {
    const p = trackerProps()
    const held = workspaceDeferred()
    const real = p.transport.fetchImpl.getMockImplementation()
    p.transport.fetchImpl.mockImplementation((url, init) => url.includes('/tracker-rows') ? held.promise : real(url, init))
    const view = render(<SolarWorkspaceTools {...p} />)
    await openTracker(); fillTracker(); fireEvent.click(trackerPublish())
    await waitFor(() => expect(trackerPosts(p)).toHaveLength(1))
    expect(trigger().disabled).toBe(true)
    expect(terrainTrigger().disabled).toBe(true)
    expect(screen.getByTestId('solar-tracker-pending-reason').textContent).toBe(TRACKER_SENTENCES.tracker_pending)
    view.rerender(<SolarWorkspaceTools {...p} flow="rooftop" />)
    expect(combinerTrigger().disabled).toBe(true)
    fireEvent.click(combinerTrigger())
    expect(screen.queryByLabelText('Combiner intake file')).toBeNull()
    expect(p.transport.fetchImpl.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1)
    await act(async () => held.resolve(terrainResponse(trackerAnswer(), 201)))
    await waitFor(() => expect(combinerTrigger().disabled).toBe(false))
    expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
  })
  it('c12_synchronous_interlock', async () => {
    const p = trackerProps()
    let uploadHandler
    vi.spyOn(landxmlUpload, 'default').mockImplementation(({ upload }) => {
      uploadHandler = upload
      return <button type="button">Captured import</button>
    })
    const held = workspaceDeferred()
    const real = p.transport.fetchImpl.getMockImplementation()
    p.transport.fetchImpl.mockImplementation((url, init) => url.includes('/imports/landxml') ? held.promise : real(url, init))
    render(<SolarWorkspaceTools {...p} />)
    open()
    await openTracker(); fillTracker()
    let losing
    await act(async () => {
      const controller = new AbortController()
      losing = uploadHandler({ drawingId: 'solar', projectId: null, drawingUnits: 'm',
        file: new File(['xml'], 'site.xml'), signal: controller.signal })
      fireEvent.click(trackerPublish())
      await Promise.resolve()
    })
    expect(trackerPosts(p)).toHaveLength(0)
    expect(trackerPanel().textContent).toContain(trackerClients.TRACKER_ROWS_CLIENT_REASONS.TRACKER_ROWS_CLIENT_REQUEST_INVALID)
    await act(async () => { held.resolve(response()); await losing })
  })
  it('c17_double_landxml_keeps_the_tracker_interlock', async () => {
    const p = trackerProps()
    const helds = [workspaceDeferred(), workspaceDeferred()]
    let calls = 0
    const real = p.transport.fetchImpl.getMockImplementation()
    p.transport.fetchImpl.mockImplementation((url, init) => url.includes('/imports/landxml') ? helds[calls++].promise : real(url, init))
    render(<SolarWorkspaceTools {...p} />)
    open()
    fireEvent.change(screen.getByLabelText('Drawing units'), { target: { value: 'm' } })
    fireEvent.change(screen.getByLabelText('LandXML file'), {
      target: { files: [new File([new Uint8Array(45305)], 'site.xml', { type: 'application/xml' })] },
    })
    const importButton = screen.getByRole('button', { name: 'Import terrain' })
    // Two submissions in one React batch: both reach the transport.
    await act(async () => { fireEvent.click(importButton); fireEvent.click(importButton) })
    await waitFor(() => expect(calls).toBe(2))
    const reason = () => screen.queryByTestId('solar-tracker-workspace-reason')?.textContent ?? ''
    await waitFor(() => expect(trackerTrigger().disabled).toBe(true))
    // The second settles while the first is still unresolved: the interlock must hold.
    await act(async () => { helds[1].resolve(response()); await helds[1].promise })
    expect(trackerTrigger().disabled).toBe(true)
    expect(reason()).toBe(TRACKER_SENTENCES.landxml_pending)
    expect(trackerPosts(p)).toHaveLength(0)
    await act(async () => { helds[0].resolve(response()); await helds[0].promise })
    await waitFor(() => expect(reason()).not.toBe(TRACKER_SENTENCES.landxml_pending))
    expect(trackerPosts(p)).toHaveLength(0)
  })
  it('c13_publication_head_signal', async () => {
    for (const status of [201, 200]) {
      const p = trackerProps()
      const real = p.transport.fetchImpl.getMockImplementation()
      p.transport.fetchImpl.mockImplementation((url, init) => url.includes('/tracker-rows')
        ? Promise.resolve(terrainResponse(trackerAnswer(JSON.parse(init.body), status), status)) : real(url, init))
      render(<SolarWorkspaceTools {...p} />)
      await openTracker(); fillTracker(); fireEvent.click(trackerPublish())
      await waitFor(() => expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
      expect(screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index')).toBe('0')
      expect(p.onDrawingVersionChanged).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: 'Close' }))
      openTerrain()
      await waitFor(() => expect(terrainGets(p)).toHaveLength(3))
      expect(p.onPhysicalHeadChanged.mock.calls[0][0].head.state.artifact_id).toBe(TERRAIN_H1)
      cleanup()
    }
  })
  it('c14_refresh_fanout', async () => {
    const p = trackerProps()
    render(<SolarWorkspaceTools {...p} />)
    await openTracker(); fillTracker(); fireEvent.click(trackerPublish())
    await trackerPublished()
    await waitFor(() => expect(terrainGets(p)).toHaveLength(2))
    expect(terrainPanel()).toBeNull()
    expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    openTerrain(); await terrainReady()
    expect(terrainGets(p)).toHaveLength(3)
    expect(terrainPanel().getAttribute('data-head')).toBe(TERRAIN_H1)
    expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
  })
  it('c15_external_head_and_late_answers', async () => {
    const p = trackerProps()
    const view = render(<SolarWorkspaceTools {...p} />)
    await openTracker(); fillTracker()
    open(); submit()
    await waitFor(() => expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    expect(terrainGets(p)).toHaveLength(1)
    fireEvent.click(trackerTrigger())
    await waitFor(() => expect(terrainGets(p)).toHaveLength(2))
    expect(screen.getByLabelText('Module power').value).toBe('450')
    const late = workspaceDeferred()
    const real = p.transport.fetchImpl.getMockImplementation()
    p.transport.fetchImpl.mockImplementation((url, init) => init.method === 'GET' ? late.promise : real(url, init))
    fireEvent.click(trackerRefresh())
    await waitFor(() => expect(terrainGets(p)).toHaveLength(3))
    view.rerender(<SolarWorkspaceTools {...p} drawingId="other" />)
    view.rerender(<SolarWorkspaceTools {...p} />)
    expect(screen.queryByTestId('solar-tracker-rows-panel')).toBeNull()
    await act(async () => late.resolve(terrainResponse(terrainView(true))))
    expect(screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index')).toBeNull()
    expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
  })
  it('c16_preserved_legacy_and_receipt', async () => {
    for (const outcome of ['published', 'unknown']) {
      const p = trackerProps()
      if (outcome === 'unknown') {
        vi.spyOn(trackerClients, 'createSolarTrackerRowsClient').mockReturnValue({ createTrackerRows:
          vi.fn(async () => ({ ok: false, code: 'TRACKER_ROWS_CLIENT_NETWORK' })) })
      }
      const view = render(<SolarWorkspaceTools {...p} />)
      await openTracker(); fillTracker(); fireEvent.click(trackerPublish())
      await waitFor(() => expect(trackerPanel().getAttribute('data-phase')).toBe(outcome))
      fireEvent.click(screen.getByRole('button', { name: 'Close' }))
      openTerrain()
      await waitFor(() => expect(terrainPanel()).not.toBeNull())
      expect(screen.queryByRole('region', { name: 'Tracker layout' })).toBeNull()
      view.rerender(<SolarWorkspaceTools {...p} flow="rooftop" />)
      openCombiner()
      expect(document.activeElement).toBe(combinerFile())
      view.rerender(<SolarWorkspaceTools {...p} />)
      fireEvent.click(trackerTrigger())
      await waitFor(() => expect(trackerPanel().getAttribute('data-phase')).toBe(outcome === 'unknown' ? 'retry-ready' : 'published'))
      expect(screen.getByLabelText('Module power').value).toBe('450')
      expect(trackerPublish().disabled).toBe(true)
      if (outcome === 'published') expect(screen.getByLabelText('Published tracker rows').textContent).toContain('450 W')
      else expect(screen.getByRole('button', { name: 'Retry original request' })).toBeTruthy()
      cleanup(); vi.restoreAllMocks()
    }
  })
})

describe('terrain workspace integration', () => {
  it('CHECKOUT-GATE CG4 terrain GET and POST replace transport proofs with the current getter value', async () => {
    for (const casing of ['x-checkout-capability', 'X-CHECKOUT-CAPABILITY', 'x-Checkout-Capability']) {
      for (const cap of ['current-proof', '']) {
        const props = terrainProps({ getCheckoutCapability: vi.fn(() => cap) })
        props.transport.headers = () => ({ 'X-Tenant-Id': 'workspace-test', [casing]: 'transport-proof' })
        render(<SolarWorkspaceTools {...props} />)
        openTerrain()
        await terrainReady()
        fireEvent.click(terrainMesh())
        await waitFor(() => expect(terrainGets(props)).toHaveLength(2))
        await terrainReady()
        expect(terrainPosts(props)).toHaveLength(1)
        for (const [, init] of props.transport.fetchImpl.mock.calls) {
          const keys = Object.keys(init.headers).filter((key) => key.toLowerCase() === 'x-checkout-capability')
          expect(keys).toEqual(cap ? ['X-Checkout-Capability'] : [])
          if (cap) expect(init.headers['X-Checkout-Capability']).toBe(cap)
          expect(Object.values(init.headers)).not.toContain('transport-proof')
        }
        expect(props.getCheckoutCapability).toHaveBeenCalledTimes(3)
        cleanup()
      }
    }
  })

  it('CHECKOUT-GATE CG1 a LandXML upload carries the current checkout capability', async () => {
    const props = supplied({ getCheckoutCapability: vi.fn(() => 'landxml-current') })
    props.transport.headers = () => ({ 'X-Tenant-Id': 'workspace-test', 'x-CHECKOUT-capability': 'transport-proof' })
    render(<SolarWorkspaceTools {...props} />)
    open()
    submit()
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    const headers = props.transport.fetchImpl.mock.calls[0][1].headers
    expect(headers['X-Checkout-Capability']).toBe('landxml-current')
    expect(Object.keys(headers).filter((key) => key.toLowerCase() === 'x-checkout-capability'))
      .toEqual(['X-Checkout-Capability'])
    expect(props.getCheckoutCapability).toHaveBeenCalledTimes(1)
  })

  it('CHECKOUT-GATE CG2 a later LandXML upload reads a changed capability getter', async () => {
    const props = supplied({ getCheckoutCapability: vi.fn(() => 'landxml-first') })
    const view = render(<SolarWorkspaceTools {...props} />)
    open()
    submit()
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    const changed = vi.fn(() => 'landxml-second')
    // Keep the same transport and memoized client while replacing the getter.
    view.rerender(<SolarWorkspaceTools {...props} getCheckoutCapability={changed} />)
    fireEvent.click(screen.getByRole('button', { name: 'Import terrain' }))
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(2))
    expect(props.transport.fetchImpl.mock.calls.map(([, init]) => init.headers['X-Checkout-Capability']))
      .toEqual(['landxml-first', 'landxml-second'])
    expect(props.getCheckoutCapability).toHaveBeenCalledTimes(1)
    expect(changed).toHaveBeenCalledTimes(1)
  })

  it('CHECKOUT-GATE CG3 an empty getter sends no LandXML checkout header', async () => {
    const props = supplied({ getCheckoutCapability: vi.fn(() => '') })
    props.transport.headers = () => ({ 'X-Tenant-Id': 'workspace-test', 'X-CHECKOUT-CAPABILITY': 'transport-proof' })
    render(<SolarWorkspaceTools {...props} />)
    open()
    submit()
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    const headers = props.transport.fetchImpl.mock.calls[0][1].headers
    expect(Object.keys(headers).some((key) => key.toLowerCase() === 'x-checkout-capability')).toBe(false)
    expect(props.getCheckoutCapability).toHaveBeenCalledTimes(1)
  })

  it('CHECKOUT-GATE CG7 nonplain LandXML headers refuse before fetch or the capability getter', async () => {
    for (const headers of [null, [], new Headers()]) {
      const props = supplied({ getCheckoutCapability: vi.fn(() => 'unused-proof') })
      props.transport.headers = () => headers
      render(<SolarWorkspaceTools {...props} />)
      open()
      submit()
      await waitFor(() => expect(screen.getByTestId('solar-landxml-refusal').textContent)
        .toBe('This LandXML step was given input it cannot send'))
      expect(props.transport.fetchImpl).not.toHaveBeenCalled()
      expect(props.getCheckoutCapability).not.toHaveBeenCalled()
      cleanup()
    }
  })
  it('W20-06b T1 exposes terrain on Ground Physical for local and platform drawings without eager reads', () => {
    const props = terrainProps()
    const view = render(<SolarWorkspaceTools {...props} />)
    for (const projectId of [null, TERRAIN_PROJECT]) {
      for (const drawingId of [null, 'solar']) {
        for (const flow of ['rooftop', 'ground-physical', 'ground-electrical', 'solaredge-import', 'pvcase-tutorial']) {
          view.rerender(<SolarWorkspaceTools {...props} projectId={projectId} drawingId={drawingId} flow={flow} />)
          expect(!!screen.queryByRole('button', { name: 'Terrain preview', exact: true }))
            .toBe(!!drawingId && flow === 'ground-physical')
          expect(!!screen.queryByRole('button', { name: 'Create tracker rows', exact: true }))
            .toBe(!!drawingId && flow === 'ground-physical')
          expect(!!screen.queryByRole('button', { name: 'Import combiner intake' }))
            .toBe(!!drawingId && projectId === null && flow === 'rooftop')
          expect(terrainPanel()).toBeNull()
        }
      }
    }
    expect(props.transport.fetchImpl).not.toHaveBeenCalled()
  })

  it('W20-06b T2 opens one panel at a time and returns focus on Close', async () => {
    const props = terrainProps()
    render(<SolarWorkspaceTools {...props} />)
    openTerrain()
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Terrain preview' }))
    expect(terrainTrigger().getAttribute('aria-expanded')).toBe('true')
    expect(trigger().getAttribute('aria-expanded')).toBe('false')
    await terrainReady()
    open()
    expect(terrainPanel()).toBeNull()
    expect(document.activeElement).toBe(screen.getByLabelText('LandXML file'))
    expect(terrainTrigger().getAttribute('aria-expanded')).toBe('false')
    expect(trigger().getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Create tracker rows' }))
    expect(control()).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Tracker layout' }))
    openTerrain()
    expect(control()).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Terrain preview' }))
    await terrainReady()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(terrainPanel()).toBeNull()
    expect(document.activeElement).toBe(terrainTrigger())
    expect(terrainTrigger().getAttribute('aria-expanded')).toBe('false')
  })

  it('W20-06b T3 a flow change closes terrain and returning keeps it closed', async () => {
    const props = terrainProps()
    const view = render(<SolarWorkspaceTools {...props} />)
    openTerrain()
    await terrainReady()
    view.rerender(<SolarWorkspaceTools {...props} flow="ground-electrical" />)
    expect(terrainPanel()).toBeNull()
    view.rerender(<SolarWorkspaceTools {...props} />)
    expect(terrainPanel()).toBeNull()
    expect(terrainTrigger().getAttribute('aria-expanded')).toBe('false')
    expect(terrainGets(props)).toHaveLength(1)
  })

  it('W20-06b T4 reads current tenant and bearer per GET and POST and strips checkout overrides', async () => {
    for (const override of [false, true]) {
      const props = terrainProps()
      props.getCheckoutCapability.mockReturnValue('current-a')
      props.transport.headers = override
        ? vi.fn(() => ({ 'X-Tenant-Id': api.config.tenant, ...api.authHeaders(), 'x-CHECKOUT-capability': 'secret' }))
        : undefined
      render(<SolarWorkspaceTools {...props} />)
      localStorage.setItem('leaf.jwt', 'terrain-a')
      openTerrain()
      await terrainReady()
      expect(terrainGets(props)[0][1].headers).toMatchObject({
        'X-Tenant-Id': api.config.tenant, Authorization: 'Bearer terrain-a', 'X-Checkout-Capability': 'current-a',
      })
      localStorage.setItem('leaf.jwt', 'terrain-b')
      props.getCheckoutCapability.mockReturnValue('current-b')
      fireEvent.click(terrainMesh())
      await waitFor(() => expect(terrainGets(props)).toHaveLength(2))
      await terrainReady()
      expect(terrainPosts(props)[0][1].headers).toMatchObject({
        'X-Tenant-Id': api.config.tenant, Authorization: 'Bearer terrain-b', 'Content-Type': 'application/json',
        'X-Checkout-Capability': 'current-b',
      })
      expect(terrainGets(props)[1][1].headers.Authorization).toBe('Bearer terrain-b')
      for (const [, init] of props.transport.fetchImpl.mock.calls) {
        expect(Object.keys(init.headers).filter((key) => key.toLowerCase() === 'x-checkout-capability'))
          .toEqual(['X-Checkout-Capability'])
        expect(Object.values(init.headers)).not.toContain('secret')
      }
      expect(terrainGets(props)[1][1].headers['X-Checkout-Capability']).toBe('current-b')
      if (override) expect(props.transport.headers).toHaveBeenNthCalledWith(3, 'solar')
      expect(props.getCheckoutCapability).toHaveBeenCalledTimes(3)
      cleanup()
    }
  })

  it('W20-06b T4 malformed header overrides retain the terrain client refusal', async () => {
    for (const headers of [null, [], new Headers(), { Authorization: 42 }, { [Symbol('header')]: 'value' }]) {
      const props = terrainProps()
      props.transport.headers = () => headers
      render(<SolarWorkspaceTools {...props} />)
      openTerrain()
      await waitFor(() => expect(terrainPanel()?.getAttribute('data-phase')).toBe('refused'))
      expect(props.transport.fetchImpl).not.toHaveBeenCalled()
      expect(props.getCheckoutCapability).not.toHaveBeenCalled()
      cleanup()
    }
  })

  it('W20-06b T4 platform GET and POST carry the project scope without a checkout capability', async () => {
    const props = terrainProps({ projectId: TERRAIN_PROJECT })
    props.getCheckoutCapability.mockReturnValue('platform-proof')
    render(<SolarWorkspaceTools {...props} />)
    openTerrain()
    await terrainReady()
    fireEvent.click(terrainMesh())
    await waitFor(() => expect(terrainGets(props)).toHaveLength(2))
    await terrainReady()
    expect(terrainPosts(props)).toHaveLength(1)
    for (const [url, init] of props.transport.fetchImpl.mock.calls) {
      expect(new URL(url, 'https://workspace.test').searchParams.get('project_id')).toBe(TERRAIN_PROJECT)
      expect(init.headers['X-Checkout-Capability']).toBe('platform-proof')
    }
    expect(props.getCheckoutCapability).toHaveBeenCalledTimes(3)
    expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
  })

  it('W20-06b T5 GET and POST unauthorized answers report the sent bearer without outward callbacks', async () => {
    for (const method of ['GET', 'POST']) {
      const props = terrainProps()
      props.transport.headers = () => ({ 'X-Tenant-Id': 'workspace-test', ...api.authHeaders() })
      const refused = terrainResponse(JSON.parse(UNAUTH_TEXT), 401)
      props.transport.fetchImpl.mockImplementation(async (url, init) =>
        init.method === method ? refused : terrainResponse())
      render(<SolarWorkspaceTools {...props} />)
      localStorage.setItem('leaf.jwt', 'tr-old')
      openTerrain()
      if (method === 'POST') {
        await terrainReady()
        localStorage.setItem('leaf.jwt', 'tr-new')
        fireEvent.click(terrainMesh())
      }
      await waitFor(() => expect(screen.getByTestId('solar-terrain-refusal')).toBeTruthy())
      const sent = props.transport.fetchImpl.mock.calls.find(([, init]) => init.method === method)
      expect(props.transport.onResponse).toHaveBeenCalledWith(refused, sent[0],
        method === 'GET' ? 'Bearer tr-old' : 'Bearer tr-new')
      expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
      expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
      expect(terrainGets(props)).toHaveLength(1)
      cleanup()
    }
  })

  it.each([true, false])('W20-06b T6 import created=%s signals a mounted terrain panel to read the new head', async (created) => {
    const props = terrainProps()
    const pending = workspaceDeferred()
    const imported = value(created)
    imported.head = terrainHead(true)
    let deliverImport
    let moved = false
    const Upload = landxmlUpload.default
    // Delay only callback delivery so the real import can notify an already mounted panel.
    vi.spyOn(landxmlUpload, 'default').mockImplementation((uploadProps) => Upload({
      ...uploadProps, onImported: (result) => { deliverImport = () => uploadProps.onImported(result) },
    }))
    props.transport.fetchImpl.mockImplementation(async (url, init) => {
      if (url.includes('/imports/landxml')) return terrainResponse(imported)
      return init.method === 'POST' ? pending.promise : terrainResponse(terrainView(moved))
    })
    render(<SolarWorkspaceTools {...props} />)
    openTerrain()
    await terrainReady()
    expect(terrainPanel().getAttribute('data-head')).toBe(TERRAIN_H1)
    open(); submit()
    await waitFor(() => expect(deliverImport).toBeTypeOf('function'))
    await waitFor(() => expect(terrainTrigger().disabled).toBe(false))
    openTerrain()
    await terrainReady()
    const beforeImport = terrainGets(props).length
    expect(beforeImport).toBe(2)
    moved = true
    act(() => deliverImport())
    await waitFor(() => expect(terrainGets(props)).toHaveLength(beforeImport + 1))
    await terrainReady()
    expect(terrainPanel().getAttribute('data-head')).toBe(TERRAIN_H2)
    expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
    expect(props.onPhysicalHeadChanged).toHaveBeenCalledWith(imported)
    fireEvent.click(terrainMesh())
    await waitFor(() => expect(terrainPosts(props)).toHaveLength(1))
    expect(JSON.parse(terrainPosts(props)[0][1].body).expected_head).toBe(TERRAIN_H2)
    expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
  })

  it('W20-06b T7 a created mesh updates the container head with one POST and one follow-up GET', async () => {
    const props = terrainProps()
    const view = render(<SolarWorkspaceTools {...props} />)
    openTerrain()
    await terrainReady()
    fireEvent.click(terrainMesh())
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    await terrainReady()
    expect(terrainPosts(props)).toHaveLength(1)
    expect(terrainGets(props)).toHaveLength(2)
    const expected = terrainResult()
    delete expected.error
    delete expected.degraded_mode
    expect(props.onPhysicalHeadChanged).toHaveBeenCalledWith(expected)
    expect(screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index')).toBe('1')
    expect(announcement()).toBe('')
    expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
    view.rerender(<SolarWorkspaceTools {...props} />)
    await act(async () => {})
    expect(terrainGets(props)).toHaveLength(2)
  })

  it('W20-06b T8 operation then LandXML remounts a fresh focused upload and retains the latest head', async () => {
    const props = terrainProps()
    render(<SolarWorkspaceTools {...props} />)
    open()
    fireEvent.change(screen.getByLabelText('LandXML file'), {
      target: { files: [new File(['old'], 'old.xml', { type: 'application/xml' })] },
    })
    openTerrain()
    await terrainReady()
    fireEvent.click(terrainMesh())
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    await terrainReady()
    const count = props.transport.fetchImpl.mock.calls.length
    open()
    expect(terrainPanel()).toBeNull()
    expect(control().getAttribute('data-phase')).toBe('idle')
    expect(screen.getByLabelText('LandXML file').files).toHaveLength(0)
    expect(document.activeElement).toBe(screen.getByLabelText('LandXML file'))
    expect(screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index')).toBe('1')
    expect(props.transport.fetchImpl).toHaveBeenCalledTimes(count)
  })

  it.each(['existing', 'TERRAIN_HEAD_MOVED', 'PHYSICAL_HEAD_CONFLICT'])
    ('W20-06b T9 %s reads once afterward without retrying or notifying the mount', async (outcome) => {
      const props = terrainProps()
      props.transport.fetchImpl.mockImplementation(async (url, init) => {
        if (init.method === 'GET') return terrainResponse(terrainView(terrainGets(props).length > 1))
        return outcome === 'existing' ? terrainResponse(terrainResult(false))
          : terrainResponse({ error: { error_code: 'CONFLICT', reason_code: outcome, retryable: false } }, 409)
      })
      render(<SolarWorkspaceTools {...props} />)
      openTerrain()
      await terrainReady()
      fireEvent.click(terrainMesh())
      await waitFor(() => expect(terrainGets(props)).toHaveLength(2))
      await terrainReady()
      expect(screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index')).toBe('1')
      expect(terrainPanel().getAttribute('data-head')).toBe(TERRAIN_H2)
      expect(terrainPosts(props)).toHaveLength(1)
      expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
      expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
    })

  it('W20-06b T13 a late older read cannot move the container head backwards', async () => {
    const props = terrainProps()
    const older = workspaceDeferred()
    const createClient = terrainClients.createSolarTerrainClient
    let heldRead
    vi.spyOn(terrainClients, 'createSolarTerrainClient').mockImplementation((options) => {
      const client = createClient(options)
      let reads = 0
      return {
        ...client,
        getTerrain: async (request) => {
          const number = ++reads
          const result = await client.getTerrain(request)
          if (number === 2) {
            heldRead = result
            return older.promise
          }
          return result
        },
      }
    })
    props.transport.fetchImpl.mockImplementation(async () =>
      terrainResponse(terrainView(terrainGets(props).length >= 3)))
    render(<SolarWorkspaceTools {...props} />)
    openTerrain()
    await terrainReady()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh terrain preview' }))
    await waitFor(() => expect(heldRead?.ok).toBe(true))
    expect(heldRead.value.head.index).toBe(0)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    openTerrain()
    await terrainReady()
    const region = screen.getByRole('region', { name: 'Solar workspace tools' })
    expect(region.getAttribute('data-physical-head-index')).toBe('1')
    await act(async () => { older.resolve(heldRead) })
    expect(region.getAttribute('data-physical-head-index')).toBe('1')
    expect(terrainPanel().getAttribute('data-head')).toBe(TERRAIN_H2)
    expect(terrainGets(props)).toHaveLength(3)
    expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
  })

  it('W20-06b T14 a successful read naming another drawing changes no container head', async () => {
    const props = terrainProps()
    const createClient = terrainClients.createSolarTerrainClient
    vi.spyOn(terrainClients, 'createSolarTerrainClient').mockImplementation((options) => {
      const client = createClient(options)
      let reads = 0
      return {
        ...client,
        getTerrain: async (request) => {
          const number = ++reads
          const result = await client.getTerrain(request)
          // Exercise the container boundary after the real transport and validation.
          if (number === 2 && result.ok) {
            result.value.head.drawing_id = 'other'
            result.value.terrain.drawing_id = 'other'
          }
          return result
        },
      }
    })
    props.transport.fetchImpl.mockImplementation(async () =>
      terrainResponse(terrainView(terrainGets(props).length > 1)))
    render(<SolarWorkspaceTools {...props} />)
    openTerrain()
    await terrainReady()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh terrain preview' }))
    await waitFor(() => expect(terrainPanel().getAttribute('data-phase')).toBe('refused'))
    expect(screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index')).toBe('0')
    expect(terrainPanel().getAttribute('data-head')).toBe(TERRAIN_H1)
    expect(screen.getByTestId('solar-terrain-refusal').textContent)
      .toBe(terrainClients.terrainReason('TERRAIN_CLIENT_RESPONSE_INVALID'))
    expect(terrainGets(props)).toHaveLength(2)
    expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
  })

  it('W20-06b T15 a failed read retains the container head and reaches the panel unchanged', async () => {
    const props = terrainProps()
    props.transport.fetchImpl.mockImplementation(async () => terrainGets(props).length === 1
      ? terrainResponse() : terrainResponse(JSON.parse(UNAUTH_TEXT), 401))
    render(<SolarWorkspaceTools {...props} />)
    openTerrain()
    await terrainReady()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh terrain preview' }))
    await waitFor(() => expect(terrainPanel().getAttribute('data-phase')).toBe('refused'))
    expect(screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index')).toBe('0')
    expect(terrainPanel().getAttribute('data-head')).toBe(TERRAIN_H1)
    expect(screen.getByTestId('solar-terrain-refusal').textContent)
      .toBe(terrainClients.terrainReason('UNAUTHENTICATED'))
    expect(terrainGets(props)).toHaveLength(2)
    expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
  })

  it('W20-06b T10 reads and Refresh stay available behind the busy and checkout gates', async () => {
    const props = terrainProps({ busy: true })
    const view = render(<SolarWorkspaceTools {...props} />)
    openTerrain()
    await terrainReady()
    for (const [busy, checkoutHeld, reason] of [
      [true, true, TERRAIN_WORKSPACE_REASONS.run_in_progress],
      [false, false, TERRAIN_WORKSPACE_REASONS.checkout_required],
      [true, false, TERRAIN_WORKSPACE_REASONS.run_in_progress],
    ]) {
      view.rerender(<SolarWorkspaceTools {...props} busy={busy} checkoutHeld={checkoutHeld} />)
      expect(screen.getByTestId('solar-terrain-reason').textContent).toBe(reason)
      for (const name of ['Run mesh preview', 'Run slope preview', 'Clear slope preview']) {
        const button = screen.getByRole('button', { name })
        expect(button.disabled).toBe(true)
        fireEvent.click(button)
      }
      const count = terrainGets(props).length
      const refresh = screen.getByRole('button', { name: 'Refresh terrain preview' })
      expect(refresh.disabled).toBe(false)
      fireEvent.click(refresh)
      await waitFor(() => expect(terrainGets(props)).toHaveLength(count + 1))
      await terrainReady()
    }
    expect(terrainPosts(props)).toHaveLength(0)
    view.rerender(<SolarWorkspaceTools {...props} busy={false} checkoutHeld={true} />)
    expect(screen.queryByTestId('solar-terrain-reason')).toBeNull()
    expect(terrainMesh().disabled).toBe(false)
    fireEvent.click(terrainMesh())
    await waitFor(() => expect(terrainPosts(props)).toHaveLength(1))
    await terrainReady()
  })

  it.each(['success', 'refusal', 'rejection', 'Cancel', 'Close', 'flow'])
    ('W20-06b T11 a pending import interlocks terrain until %s', async (outcome) => {
      const props = terrainProps()
      const pending = workspaceDeferred()
      props.transport.fetchImpl.mockImplementationOnce(() => pending.promise)
      const view = render(<SolarWorkspaceTools {...props} />)
      open(); submit()
      await waitFor(() => expect(props.transport.fetchImpl).toHaveBeenCalledTimes(1))
      expect(terrainTrigger().disabled).toBe(true)
      expect(terrainTrigger().textContent).toBe('Terrain preview')
      expect(screen.getByTestId('solar-terrain-reason').textContent).toBe(TERRAIN_WORKSPACE_REASONS.landxml_importing)
      fireEvent.click(terrainTrigger())
      expect(terrainPanel()).toBeNull()
      const signal = props.transport.fetchImpl.mock.calls[0][1].signal
      if (outcome === 'success') await act(async () => { pending.resolve(response()) })
      if (outcome === 'refusal') await act(async () => { pending.resolve(terrainResponse(JSON.parse(UNAUTH_TEXT), 401)) })
      if (outcome === 'rejection') await act(async () => { pending.reject(new Error('network')) })
      if (outcome === 'Cancel') fireEvent.click(screen.getByRole('button', { name: 'Cancel import' }))
      if (outcome === 'Close') fireEvent.click(screen.getByRole('button', { name: 'Close' }))
      if (outcome === 'flow') {
        view.rerender(<SolarWorkspaceTools {...props} flow="ground-electrical" />)
        view.rerender(<SolarWorkspaceTools {...props} />)
      }
      await waitFor(() => expect(terrainTrigger().disabled).toBe(false))
      expect(screen.queryByTestId('solar-terrain-reason')).toBeNull()
      if (['Cancel', 'Close', 'flow'].includes(outcome)) {
        expect(signal.aborted).toBe(true)
        await act(async () => { pending.resolve(response()) })
      }
      if (outcome === 'success') expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
      else expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
      expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
    })

  it('W20-06b T11 an older upload cannot settle a newer upload interlock', async () => {
    const props = terrainProps()
    const older = workspaceDeferred()
    const newer = workspaceDeferred()
    props.transport.fetchImpl.mockImplementationOnce(() => older.promise).mockImplementationOnce(() => newer.promise)
    render(<SolarWorkspaceTools {...props} />)
    open(); submit()
    await waitFor(() => expect(props.transport.fetchImpl).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel import' }))
    fireEvent.click(screen.getByRole('button', { name: 'Import terrain' }))
    await waitFor(() => expect(props.transport.fetchImpl).toHaveBeenCalledTimes(2))
    await act(async () => { older.resolve(response()) })
    expect(terrainTrigger().disabled).toBe(true)
    expect(screen.getByTestId('solar-terrain-reason').textContent).toBe(TERRAIN_WORKSPACE_REASONS.landxml_importing)
    expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
    await act(async () => { newer.resolve(response()) })
    await waitFor(() => expect(terrainTrigger().disabled).toBe(false))
    expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
  })

  it('W20-06b T12 completed terrain heads reset on drawing and project changes', async () => {
    for (const next of [{ drawingId: 'other' }, { projectId: TERRAIN_PROJECT }]) {
      const props = terrainProps()
      const view = render(<SolarWorkspaceTools {...props} />)
      openTerrain()
      await terrainReady()
      fireEvent.click(terrainMesh())
      await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
      await terrainReady()
      expect(screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index')).toBe('1')
      view.rerender(<SolarWorkspaceTools {...props} {...next} />)
      expect(terrainPanel()).toBeNull()
      expect(screen.getByRole('region', { name: 'Solar workspace tools' }).hasAttribute('data-physical-head-index')).toBe(false)
      view.rerender(<SolarWorkspaceTools {...props} />)
      expect(terrainPanel()).toBeNull()
      expect(terrainTrigger().getAttribute('aria-expanded')).toBe('false')
      view.unmount()
    }
  })

  it.each(['GET', 'POST'])('W20-06b T12 deferred %s is aborted and forgotten at every scope boundary', async (method) => {
    for (const boundary of ['drawing', 'project', 'A to B to A', 'Close', 'flow']) {
      const props = terrainProps()
      const pending = workspaceDeferred()
      props.transport.fetchImpl.mockImplementation(async (url, init) =>
        init.method === method ? pending.promise : terrainResponse())
      const view = render(<SolarWorkspaceTools {...props} />)
      openTerrain()
      if (method === 'POST') {
        await terrainReady()
        fireEvent.click(terrainMesh())
      }
      await waitFor(() => expect(props.transport.fetchImpl.mock.calls.some(([, init]) => init.method === method)).toBe(true))
      const request = props.transport.fetchImpl.mock.calls.find(([, init]) => init.method === method)
      const count = props.transport.fetchImpl.mock.calls.length
      const headIndexBeforeBoundary = screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index')
      if (boundary === 'drawing' || boundary === 'A to B to A') view.rerender(<SolarWorkspaceTools {...props} drawingId="other" />)
      if (boundary === 'project') view.rerender(<SolarWorkspaceTools {...props} projectId={TERRAIN_PROJECT} />)
      if (boundary === 'Close') fireEvent.click(screen.getByRole('button', { name: 'Close' }))
      if (boundary === 'flow') view.rerender(<SolarWorkspaceTools {...props} flow="ground-electrical" />)
      expect(request[1].signal.aborted).toBe(true)
      expect(terrainPanel()).toBeNull()
      if (boundary === 'A to B to A' || boundary === 'flow') view.rerender(<SolarWorkspaceTools {...props} />)
      await act(async () => { pending.resolve(terrainResponse(method === 'POST' ? terrainResult() : terrainView())) })
      expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
      expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
      expect(announcement()).toBe('')
      expect(screen.queryByTestId('solar-terrain-announce')).toBeNull()
      if (boundary === 'Close' || boundary === 'flow') {
        expect(screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index'))
          .toBe(headIndexBeforeBoundary)
      } else {
        expect(screen.getByRole('region', { name: 'Solar workspace tools' }).hasAttribute('data-physical-head-index')).toBe(false)
      }
      expect(props.transport.fetchImpl).toHaveBeenCalledTimes(count)
      view.rerender(<SolarWorkspaceTools {...props} />)
      props.transport.fetchImpl.mockImplementation(async () => terrainResponse())
      openTerrain()
      await terrainReady()
      expect(props.transport.fetchImpl).toHaveBeenCalledTimes(count + 1)
      expect(terrainPanel().getAttribute('data-head')).toBe(TERRAIN_H1)
      expect(terrainMesh().disabled).toBe(false)
      view.unmount()
    }
  })

  it.each(['Close', 'flow'])('W20-06b T16 a successful read delivered after %s retains the last observed head', async (boundary) => {
    const props = terrainProps()
    const pending = workspaceDeferred()
    const createClient = terrainClients.createSolarTerrainClient
    let heldRead, heldSignal
    vi.spyOn(terrainClients, 'createSolarTerrainClient').mockImplementation((options) => {
      const client = createClient(options)
      let reads = 0
      return {
        ...client,
        getTerrain: async (request) => {
          const number = ++reads
          const result = await client.getTerrain(request)
          if (number === 2) {
            heldRead = result
            heldSignal = request.signal
            return pending.promise
          }
          return result
        },
      }
    })
    props.transport.fetchImpl.mockImplementation(async () =>
      terrainResponse(terrainView(terrainGets(props).length > 1)))
    const view = render(<SolarWorkspaceTools {...props} />)
    openTerrain()
    await terrainReady()
    const region = screen.getByRole('region', { name: 'Solar workspace tools' })
    expect(region.getAttribute('data-physical-head-index')).toBe('0')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh terrain preview' }))
    await waitFor(() => expect(heldRead?.ok).toBe(true))
    expect(heldRead.value.head.index).toBe(1)
    if (boundary === 'Close') fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    else {
      view.rerender(<SolarWorkspaceTools {...props} flow="ground-electrical" />)
      view.rerender(<SolarWorkspaceTools {...props} />)
    }
    expect(heldSignal.aborted).toBe(true)
    expect(terrainPanel()).toBeNull()
    const currentRegion = screen.getByRole('region', { name: 'Solar workspace tools' })
    expect(currentRegion.getAttribute('data-physical-head-index')).toBe('0')
    await act(async () => { pending.resolve(heldRead) })
    expect(currentRegion.getAttribute('data-physical-head-index')).toBe('0')
    expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
    expect(terrainGets(props)).toHaveLength(2)
  })

  it('W20-06b T17 Close and opening LandXML retain the mesh head', async () => {
    const props = terrainProps()
    render(<SolarWorkspaceTools {...props} />)
    openTerrain()
    await terrainReady()
    fireEvent.click(terrainMesh())
    await waitFor(() => expect(terrainGets(props)).toHaveLength(2))
    await terrainReady()
    const region = screen.getByRole('region', { name: 'Solar workspace tools' })
    await waitFor(() => expect(region.getAttribute('data-physical-head-index')).toBe('1'))
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(region.getAttribute('data-physical-head-index')).toBe('1')
    open()
    expect(control()).not.toBeNull()
    expect(region.getAttribute('data-physical-head-index')).toBe('1')
  })
})

describe('combiner workspace integration', () => {
  it('W20-07b B1 exposes the local combiner and terrain panels only on their flows', () => {
    const props = combinerProps()
    const view = render(<SolarWorkspaceTools {...props} />)
    for (const flow of ['rooftop', 'ground-physical', 'ground-electrical', 'solaredge-import', 'pvcase-tutorial', 'rooftop']) {
      view.rerender(<SolarWorkspaceTools {...props} flow={flow} />)
      expect(!!screen.queryByRole('button', { name: 'Import combiner intake' })).toBe(flow === 'rooftop')
      expect(!!screen.queryByRole('button', { name: 'Import LandXML terrain' })).toBe(flow === 'ground-physical')
      expect(!!screen.queryByRole('button', { name: 'Create tracker rows' })).toBe(flow === 'ground-physical')
      expect(document.querySelectorAll('section[aria-label="Solar workspace tools"]').length)
          .toBe(['rooftop', 'ground-physical', 'solaredge-import'].includes(flow) ? 1 : 0)
    }
    view.rerender(<SolarWorkspaceTools {...props} projectId="project" />)
    expect(screen.queryByRole('region', { name: 'Solar workspace tools' })).toBeNull()
    expect(props.transport.fetchImpl).not.toHaveBeenCalled()
  })

  it('W20-07b B2 focuses the file and closes with trigger focus or a flow change', () => {
    const props = combinerProps()
    const view = render(<SolarWorkspaceTools {...props} />)
    openCombiner()
    expect(document.activeElement).toBe(combinerFile())
    expect(combinerTrigger().getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByLabelText('Combiner intake file')).toBeNull()
    expect(document.activeElement).toBe(combinerTrigger())
    expect(combinerTrigger().getAttribute('aria-expanded')).toBe('false')
    openCombiner()
    view.rerender(<SolarWorkspaceTools {...props} flow="ground-physical" />)
    view.rerender(<SolarWorkspaceTools {...props} />)
    expect(screen.queryByLabelText('Combiner intake file')).toBeNull()
    expect(combinerTrigger().getAttribute('aria-expanded')).toBe('false')
  })

  it('W20-07b B3 reads default authentication and the latest capability for every request', async () => {
    const fetchImpl = vi.fn(async () => combinerResponse())
    const props = combinerProps({ drawingVersion: 2, transport: undefined, getCheckoutCapability: () => 'cap-a' })
    const transport = { fetchImpl }
    const view = render(<SolarWorkspaceTools {...props} transport={transport} />)
    openCombiner()
    chooseCombiner()
    localStorage.setItem('leaf.jwt', 'jwt-a')
    await importCombiner()
    localStorage.setItem('leaf.jwt', 'jwt-b')
    // Preserve transport identity while changing the getter; the memoized client must see it.
    view.rerender(<SolarWorkspaceTools {...props} transport={transport} getCheckoutCapability={() => 'cap-b'} />)
    await importCombiner()
    for (const [index, cap, bearer] of [[0, 'cap-a', 'jwt-a'], [1, 'cap-b', 'jwt-b']]) {
      const [url, init] = fetchImpl.mock.calls[index]
      expect(url).toBe(api.config.apiBase.replace(/\/$/, '') + '/api/drawings/solar/imports/combiner-intake')
      expect(init.headers).toMatchObject({ 'Content-Type': 'application/json', 'X-Tenant-Id': api.config.tenant,
        Authorization: `Bearer ${bearer}`, 'X-Checkout-Capability': cap })
    }
    for (const cap of ['', null, 42]) {
      view.rerender(<SolarWorkspaceTools {...props} transport={transport} getCheckoutCapability={() => cap} />)
      const count = fetchImpl.mock.calls.length
      fireEvent.click(combinerImport())
      await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(count + 1))
      await waitFor(() => expect(combinerImport().disabled).toBe(false))
      expect(fetchImpl.mock.calls[count][1].headers).not.toHaveProperty('X-Checkout-Capability')
    }
  })

  it('W20-07b B4 reports unauthorized with the sent bearer and never stores or stages', async () => {
    const refused = new Response(UNAUTH_TEXT, { status: 401, headers: { 'content-type': 'application/json' } })
    const fetchImpl = vi.fn(async () => refused)
    const note = vi.spyOn(api, 'noteUnauthorized')
    const props = combinerProps({ transport: undefined })
    render(<SolarWorkspaceTools {...props} transport={{ fetchImpl }} />)
    localStorage.setItem('leaf.jwt', 'jwt-cmb')
    openCombiner()
    chooseCombiner()
    fireEvent.click(combinerImport())
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe(COMBINER_INTAKE_REASONS.UNAUTHENTICATED))
    expect(note).toHaveBeenCalledWith(refused, fetchImpl.mock.calls[0][0], 'Bearer jwt-cmb')
    expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
    expect(props.onRunPlacement).not.toHaveBeenCalled()
  })

  it('W20-07b B5 checkout and busy disable import with busy taking precedence', () => {
    const props = combinerProps()
    const view = render(<SolarWorkspaceTools {...props} />)
    openCombiner()
    chooseCombiner()
    view.rerender(<SolarWorkspaceTools {...props} checkoutHeld={false} />)
    expect(combinerReason().textContent).toBe('Take the drawing checkout before importing the combiner intake')
    expect(combinerFile().disabled).toBe(true)
    expect(document.querySelector('fieldset').disabled).toBe(true)
    fireEvent.click(combinerImport())
    view.rerender(<SolarWorkspaceTools {...props} checkoutHeld={false} busy={true} />)
    expect(combinerReason().textContent).toBe('A run is in progress, so wait for it to finish')
    fireEvent.click(combinerImport())
    expect(props.transport.fetchImpl).not.toHaveBeenCalled()
    view.rerender(<SolarWorkspaceTools {...props} />)
    expect(combinerReason()).toBeNull()
    expect(combinerImport().disabled).toBe(false)
    expect(document.querySelector('fieldset').disabled).toBe(false)
  })

  it('W20-07b B6 waits for the version prop even after the refresh promise settles', async () => {
    const pending = workspaceDeferred()
    const props = combinerProps({ onDrawingVersionChanged: vi.fn(() => pending.promise) })
    const view = render(<SolarWorkspaceTools {...props} />)
    openCombiner(); chooseCombiner(); combinerHardware()
    await importCombiner()
    expect(props.onDrawingVersionChanged).toHaveBeenCalledTimes(1)
    expect(props.onDrawingVersionChanged).toHaveBeenCalledWith({ drawing_id: 'solar', version: 2, parent: 1 },
      undefined, { isCurrent: expect.any(Function), announce: true })
    expect(props.onDrawingVersionChanged.mock.calls[0][2].isCurrent()).toBe(true)
    expect(combinerReason().textContent).toBe('The drawing is refreshing to the imported version')
    expect(combinerRun().disabled).toBe(true)
    await act(async () => { pending.resolve(true) })
    expect(combinerRun().disabled).toBe(true)
    expect(combinerReason().textContent).toBe('The drawing is refreshing to the imported version')
    view.rerender(<SolarWorkspaceTools {...props} drawingVersion={2} />)
    expect(combinerReason()).toBeNull()
    expect(combinerRun().disabled).toBe(false)
    expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
  })

  it('W20-07b B7 existing intake seats only a behind browser and suppresses the creation announcement', async () => {
    const props = combinerProps({ drawingVersion: 2 })
    props.transport.fetchImpl.mockImplementation(async () => combinerResponse({ created: false }))
    const view = render(<SolarWorkspaceTools {...props} />)
    openCombiner(); chooseCombiner(); combinerHardware()
    await importCombiner()
    expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
    expect(combinerRun().disabled).toBe(false)
    view.unmount()
    render(<SolarWorkspaceTools {...props} drawingVersion={1} />)
    openCombiner(); chooseCombiner()
    await importCombiner()
    expect(props.onDrawingVersionChanged).toHaveBeenCalledTimes(1)
    expect(props.onDrawingVersionChanged).toHaveBeenCalledWith({ drawing_id: 'solar', version: 2, parent: 1 },
      undefined, { isCurrent: expect.any(Function), announce: false })
  })

  it('W20-07b B8 forwards imported graph revision and numeric hardware only when valid', async () => {
    const props = combinerProps({ drawingVersion: 2 })
    render(<SolarWorkspaceTools {...props} />)
    openCombiner(); chooseCombiner(); combinerHardware()
    await importCombiner()
    fireEvent.click(combinerRun())
    expect(props.onRunPlacement).toHaveBeenCalledTimes(1)
    expect(props.onRunPlacement).toHaveBeenCalledWith({ expected_rev: 7,
      hardware: { model: 'Combiner', max_dc_voltage: 480, max_ac_power_kw: 10 } })
    fireEvent.change(screen.getByLabelText('DC voltage'), { target: { value: '0' } })
    fireEvent.click(combinerRun())
    expect(props.onRunPlacement).toHaveBeenCalledTimes(1)
    combinerHardware()
    props.onRunPlacement.mockReturnValueOnce(false)
    fireEvent.click(combinerRun())
    expect(combinerReason().textContent).toBe('Combiner placement was not staged')
    expect(combinerRun().disabled).toBe(false)
    fireEvent.click(combinerRun())
    expect(combinerReason()).toBeNull()
    props.onRunPlacement.mockReturnValueOnce(false)
    fireEvent.click(combinerRun())
    expect(combinerReason().textContent).toBe('Combiner placement was not staged')
    props.transport.fetchImpl.mockImplementationOnce(async () => new Response(UNAUTH_TEXT, { status: 401 }))
    fireEvent.click(combinerImport())
    expect(combinerReason()).toBeNull()
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe(COMBINER_INTAKE_REASONS.UNAUTHENTICATED))
  })

  it('W20-07b B9 retains file and hardware through a retryable refusal then imports', async () => {
    const props = combinerProps()
    props.transport.fetchImpl.mockImplementationOnce(async () => new Response(JSON.stringify({
      error: { error_code: 'INTERNAL', reason_code: 'COMBINER_IMPORT_STORE_UNAVAILABLE', retryable: true },
    }), { status: 503, headers: { 'content-type': 'application/json' } }))
    render(<SolarWorkspaceTools {...props} />)
    openCombiner()
    const file = chooseCombiner()
    combinerHardware()
    fireEvent.click(combinerImport())
    await waitFor(() => expect(screen.getByRole('status').textContent)
      .toBe(COMBINER_INTAKE_REASONS.COMBINER_IMPORT_STORE_UNAVAILABLE + '. This import can be retried.'))
    expect(combinerFile().files[0]).toBe(file)
    expect(screen.getByLabelText('Model').value).toBe('Combiner')
    expect(screen.getByLabelText('DC voltage').value).toBe('480')
    expect(screen.getByLabelText('AC power').value).toBe('10')
    expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
    await importCombiner()
    expect(props.transport.fetchImpl).toHaveBeenCalledTimes(2)
    expect(props.onDrawingVersionChanged).toHaveBeenCalledTimes(1)
    expect(combinerFile().files[0]).toBe(file)
    expect(screen.getByLabelText('Model').value).toBe('Combiner')
  })

  it('W20-07b B10 aborts and drops old scopes including A to B to A and project changes', async () => {
    for (const next of [{ drawingId: 'other' }, { projectId: 'project' }]) {
      const pending = workspaceDeferred()
      const props = combinerProps()
      const view = render(<SolarWorkspaceTools {...props} />)
      openCombiner(); chooseCombiner()
      await importCombiner()
      const isCurrent = props.onDrawingVersionChanged.mock.calls[0][2].isCurrent
      view.rerender(<SolarWorkspaceTools {...props} {...next} />)
      expect(isCurrent()).toBe(false)
      view.rerender(<SolarWorkspaceTools {...props} />)
      props.onDrawingVersionChanged.mockClear()
      props.transport.fetchImpl.mockImplementationOnce(() => pending.promise)
      openCombiner(); chooseCombiner(); combinerHardware()
      fireEvent.click(combinerImport())
      await waitFor(() => expect(props.transport.fetchImpl).toHaveBeenCalledTimes(2))
      const signal = props.transport.fetchImpl.mock.calls[1][1].signal
      view.rerender(<SolarWorkspaceTools {...props} {...next} />)
      expect(signal.aborted).toBe(true)
      view.rerender(<SolarWorkspaceTools {...props} />)
      await act(async () => { pending.resolve(combinerResponse()) })
      expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
      openCombiner()
      expect(screen.getByText('Import the combiner intake for this drawing first')).toBeTruthy()
      expect(combinerRun().disabled).toBe(true)
      fireEvent.click(combinerRun())
      expect(props.onRunPlacement).not.toHaveBeenCalled()
      view.unmount()
    }
  })

  it('W20-07b B11 fails on false or rejection and retries the same version with fresh guards', async () => {
    const props = combinerProps({ onDrawingVersionChanged: vi.fn()
      .mockResolvedValueOnce(false).mockRejectedValueOnce(new Error('read failed')).mockResolvedValueOnce(true) })
    const view = render(<SolarWorkspaceTools {...props} />)
    openCombiner(); chooseCombiner(); combinerHardware()
    await importCombiner()
    await waitFor(() => expect(combinerReason().textContent).toBe('The drawing did not refresh to the imported version'))
    expect(combinerRun().disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh drawing' }))
    expect(combinerReason().textContent).toBe('The drawing is refreshing to the imported version')
    await waitFor(() => expect(combinerReason().textContent).toBe('The drawing did not refresh to the imported version'))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh drawing' }))
    expect(combinerReason().textContent).toBe('The drawing is refreshing to the imported version')
    expect(props.onDrawingVersionChanged).toHaveBeenCalledTimes(3)
    const calls = props.onDrawingVersionChanged.mock.calls
    for (const call of calls) {
      expect(call[0]).toEqual({ drawing_id: 'solar', version: 2, parent: 1 })
      expect(call[1]).toBeUndefined()
      expect(call[2].announce).toBe(true)
    }
    expect(calls[0][2].isCurrent).not.toBe(calls[1][2].isCurrent)
    view.rerender(<SolarWorkspaceTools {...props} drawingVersion={2} />)
    expect(combinerReason()).toBeNull()
    expect(combinerRun().disabled).toBe(false)
  })

  it('W20-07b B12 remounts intake after idle movement or a refreshing version is overtaken', async () => {
    for (const drawingVersion of [2, 1]) {
      const props = combinerProps({ drawingVersion })
      const view = render(<SolarWorkspaceTools {...props} />)
      openCombiner(); chooseCombiner(); combinerHardware()
      await importCombiner()
      view.rerender(<SolarWorkspaceTools {...props} drawingVersion={3} />)
      expect(screen.getByText('Import the combiner intake for this drawing first')).toBeTruthy()
      expect(combinerRun().disabled).toBe(true)
      expect(combinerReason()).toBeNull()
      expect(screen.getByLabelText('Model').value).toBe('')
      fireEvent.click(combinerRun())
      expect(props.onRunPlacement).not.toHaveBeenCalled()
      view.unmount()
    }
  })
})

describe('SolarWorkspaceTools', () => {
  it('W1 ground physical opens the terrain control and focuses its file input', () => {
    render(<SolarWorkspaceTools {...supplied()} />)
    expect(control()).toBeNull()
    open()
    expect(control()).not.toBeNull()
    expect(document.activeElement).toBe(screen.getByLabelText('LandXML file'))
    expect(trigger().getAttribute('aria-expanded')).toBe('true')
  })

  it('W2 other flows expose no LandXML action and never fetch', () => {
    const props = supplied()
    const view = render(<SolarWorkspaceTools {...props} flow="rooftop" />)
    for (const flow of ['rooftop', 'ground-electrical', 'solaredge-import', 'pvcase-tutorial']) {
      view.rerender(<SolarWorkspaceTools {...props} flow={flow} />)
      expect(screen.queryByRole('button', { name: 'Import LandXML terrain' })).toBeNull()
      expect(control()).toBeNull()
    }
    expect(props.transport.fetchImpl).not.toHaveBeenCalled()
  })

  it('W3 without a drawing there is no actionable control or request', () => {
    const props = supplied({ drawingId: null })
    render(<SolarWorkspaceTools {...props} />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(control()).toBeNull()
    expect(props.transport.fetchImpl).not.toHaveBeenCalled()
  })

  it('W4 default transport reads tenant and bearer headers afresh for each upload', async () => {
    const props = supplied({ transport: undefined })
    render(<SolarWorkspaceTools {...props} />)
    // Install fetch after mounting: the default transport must read it at request time.
    const fetchImpl = vi.fn(async () => response())
    vi.stubGlobal('fetch', fetchImpl)
    localStorage.setItem('leaf.jwt', 'jwt-a')
    open()
    submit()
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url.startsWith(api.config.apiBase.replace(/\/$/, '') + '/api/drawings/solar/imports/landxml?')).toBe(true)
    expect(init.method).toBe('POST')
    expect(init.headers['X-Tenant-Id']).toBe(api.config.tenant)
    expect(init.headers.Authorization).toBe('Bearer jwt-a')
    expect(init.headers['Content-Type']).toBe('application/xml')
    expect(Object.keys(init.headers).some((name) => /checkout|guest/i.test(name))).toBe(false)

    localStorage.setItem('leaf.jwt', 'jwt-b')
    fireEvent.click(screen.getByRole('button', { name: 'Import terrain' }))
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(2))
    expect(fetchImpl.mock.calls[1][1].headers.Authorization).toBe('Bearer jwt-b')
    expect(fetchImpl.mock.calls[1][1].headers['X-Tenant-Id']).toBe(api.config.tenant)
  })

  it('W5 default transport reports a 401 with the bearer actually sent', async () => {
    const refused = new Response(UNAUTH_TEXT, { status: 401, headers: { 'content-type': 'application/json' } })
    const fetchImpl = vi.fn(async () => refused)
    vi.stubGlobal('fetch', fetchImpl)
    const note = vi.spyOn(api, 'noteUnauthorized')
    localStorage.setItem('leaf.jwt', 'jwt-old')
    const props = supplied({ transport: undefined })
    render(<SolarWorkspaceTools {...props} />)
    open()
    submit()
    await waitFor(() => expect(note).toHaveBeenCalledTimes(1))
    expect(note).toHaveBeenCalledWith(refused, fetchImpl.mock.calls[0][0], 'Bearer jwt-old')
    await waitFor(() => expect(screen.getByTestId('solar-landxml-refusal').textContent).toBe('Sign in again to import a LandXML file'))
    expect(localStorage.getItem('leaf.jwt')).toBeNull()
    expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
  })

  it('W6 a created terrain stores its head, announces it and calls only the physical callback once', async () => {
    const props = supplied()
    render(<SolarWorkspaceTools {...props} />)
    open()
    submit()
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    expect(props.onPhysicalHeadChanged).toHaveBeenCalledWith(value())
    expect(announcement()).toBe(CREATED)
    expect(document.querySelector('.solar-workspace-announce').getAttribute('aria-live')).toBe('polite')
    expect(screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index')).toBe('0')
    expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
    expect(props.getCheckoutCapability).toHaveBeenCalledTimes(1)
  })

  it('W7 an existing terrain announces nothing changed and still calls the physical callback once', async () => {
    const props = supplied()
    props.transport.fetchImpl.mockImplementation(async () => response(false))
    render(<SolarWorkspaceTools {...props} />)
    open()
    submit()
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    expect(props.onPhysicalHeadChanged).toHaveBeenCalledWith(value(false))
    expect(announcement()).toBe(EXISTING)
    expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
  })

  it('W8 scope changes close the panel, clear terrain state and drop a late result even after returning', async () => {
    for (const nextScope of [{ drawingId: 'other' }, { projectId: 'other-project' }]) {
      let resolve
      const pending = new Promise((done) => { resolve = done })
      const props = supplied()
      props.transport.fetchImpl.mockImplementation(() => pending)
      const view = render(<SolarWorkspaceTools {...props} />)
      open()
      submit()
      await waitFor(() => expect(props.transport.fetchImpl).toHaveBeenCalledTimes(1))
      const signal = props.transport.fetchImpl.mock.calls[0][1].signal
      view.rerender(<SolarWorkspaceTools {...props} {...nextScope} />)
      expect(control()).toBeNull()
      expect(announcement()).toBe('')
      expect(signal.aborted).toBe(true)
      view.rerender(<SolarWorkspaceTools {...props} />)
      await act(async () => { resolve(response()) })
      expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
      expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
      expect(announcement()).toBe('')
      expect(control()).toBeNull()

      // A completed import's stored head and announcement also reset on either scope change.
      props.transport.fetchImpl.mockImplementation(async () => response())
      open()
      submit()
      await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
      expect(announcement()).toBe(CREATED)
      view.rerender(<SolarWorkspaceTools {...props} {...nextScope} />)
      expect(announcement()).toBe('')
      expect(control()).toBeNull()
      expect(screen.getByRole('region', { name: 'Solar workspace tools' }).hasAttribute('data-physical-head-index')).toBe(false)
      view.unmount()
    }
  })

  it('W9 Close removes the control and returns focus to the trigger', () => {
    render(<SolarWorkspaceTools {...supplied()} />)
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(control()).toBeNull()
    expect(document.activeElement).toBe(trigger())
    expect(trigger().getAttribute('aria-expanded')).toBe('false')
  })

  it('W10 the upload control supplies checkout and busy disabled reasons', () => {
    const props = supplied({ checkoutHeld: false })
    const view = render(<SolarWorkspaceTools {...props} />)
    open()
    expect(screen.getByTestId('solar-landxml-reason').textContent).toBe('Take the drawing checkout before importing terrain')
    expect(screen.getByRole('button', { name: 'Import terrain' }).disabled).toBe(true)
    view.rerender(<SolarWorkspaceTools {...props} checkoutHeld={true} busy={true} />)
    expect(screen.getByTestId('solar-landxml-reason').textContent).toBe('A run is in progress, so wait for it to finish')
    expect(screen.getByRole('button', { name: 'Import terrain' }).disabled).toBe(true)
    expect(props.transport.fetchImpl).not.toHaveBeenCalled()
  })
})
