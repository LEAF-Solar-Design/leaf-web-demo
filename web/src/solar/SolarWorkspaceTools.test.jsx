import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api.js'
import * as landxmlUpload from './SolarLandxmlUpload.jsx'
import * as terrainClients from './solarTerrainClient.js'
import SolarWorkspaceTools, { TERRAIN_WORKSPACE_REASONS } from './SolarWorkspaceTools.jsx'
import { COMBINER_INTAKE_REASONS } from './solarCombinerIntakeClient.js'

// The route capture also used by SolarLandxmlUpload.test.jsx; the real client validates it.
const FIRST_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8","media_type":"application/json","filename":"physical-state.json","byte_length":18283,"content_sha256":"0de2afeb7ae692538ce33fd011b043438cd65cc426fd1cead1933eb4046d22f0","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8"}},"error":null,"degraded_mode":false}`
const UNAUTH_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"UNAUTHENTICATED","message":"missing bearer token (Authorization header)","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Sign in, then repeat the request."},"degraded_mode":false}`
const CREATED = 'Terrain imported for this drawing.'
const EXISTING = 'This terrain was already imported, so nothing changed.'

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
      expect(document.querySelectorAll('section[aria-label="Solar workspace tools"]').length)
        .toBe(['rooftop', 'ground-physical'].includes(flow) ? 1 : 0)
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
