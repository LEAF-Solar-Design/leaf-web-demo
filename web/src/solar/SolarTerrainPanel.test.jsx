// sf-w5-terrain-panel-files (W20-06a): the Ground Physical terrain panel, unmounted.
// Every fixture below is SYNTHETIC: built here in the shape server/routers/solar_terrain.py and
// server/solar_ground_terrain_adapter.py document, never captured from a running server. A
// success fixture is one the real validator accepts: the panel validates every answer it is
// given, so a fixture it would refuse could not reach the screen as a success.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  TERRAIN_CLIENT_REASONS, TERRAIN_ROUTE_REASONS, createSolarTerrainClient,
} from './solarTerrainClient.js'
import SolarTerrainPanel from './SolarTerrainPanel.jsx'

const H = 'a'.repeat(64)
const H2 = 'b'.repeat(64)
const D = 'c'.repeat(64)
const MESH_CAPABILITY = 'terrain-mesh-render'
const SLOPE_CAPABILITY = 'tracker-slope-violations'
const DEFAULTS = Object.freeze({
  MaxNsSlopePct: 8.5, MaxRowToRowEwSlopePct: 10, MaxAxialSlopePct: 8.5, MaxCrossAxisSlopePct: 10,
  MaxRowToRowSlopeDeg: 4, MaxSlopePercent: 15, Columns: 0,
})
// The kernel's own status wording. It must never reach the page.
const KERNEL_STATUS = 'Tracker slope: 0/2 axial / 0/1 cross - within ASCE 7-16 budget.'
const MESH = 'Run mesh preview'
const SLOPE = 'Run slope preview'
const CLEAR = 'Clear slope preview'
const REFRESH = 'Refresh terrain preview'
const ACTIONS = [[MESH, 'mesh'], [SLOPE, 'slope'], [CLEAR, 'slope-clear']]
const NO_CHANGE = 'No terrain change was created by this preview'
const BUSY = 'Terrain operations are unavailable while this workspace is busy'
const UNREADABLE = 'The server answer could not be read, so refresh the terrain state'

const clone = (value) => JSON.parse(JSON.stringify(value))
const envelope = (body) => ({ ...body, error: null, degraded_mode: false })
const normalize = (body) => {
  const copy = clone(body)
  delete copy.error
  delete copy.degraded_mode
  return copy
}
const headOf = ({ artifact = H, index = 0, parent = null, drawing = 'solar', project = 'p' } = {}) => ({
  schema: 'leaf.solar-physical-head.v1', drawing_id: drawing, project_id: project, index, parent,
  state: {
    artifact_id: artifact, media_type: 'application/json', filename: 'physical-state.json', byte_length: 1024,
    content_sha256: 'd'.repeat(64), source_version: 1, schema: 'leaf.solar-artifact-ref.v1',
    download: `/api/drawings/${drawing}/artifacts/${artifact}`,
  },
})
const frameOf = () => ({
  coordinate_system: 'world', transform: 'identity', drawing_units: 'm', meters_per_unit: 1, crs: 'none',
  elevation_datum: 'unrecorded', horizontal: 'drawing-units', elevation: 'metres',
})
// A 2 by 2 grid summary whose cells follow the adapter's own formulas.
const gridOf = () => ({
  rows: 2, cols: 2, x_min: 0, x_max: 10, y_min: 0, y_max: 10, cell_x: 10, cell_y: 10, cell_x_m: 10, cell_y_m: 10,
  elevation_min_m: 0, elevation_max_m: 0, grid_sha256: D,
})
const meshRecord = () => ({
  schema: 'leaf.solar-terrain-preview.v1', capability: MESH_CAPABILITY, maturity: 'preview', grid_sha256: D,
  meters_per_unit: 1, faces: 1, buckets: { Green: 1, Yellow: 0, Red: 0 }, max_slope_percent: 0,
  mesh_sha256: 'e'.repeat(64),
})
const slopeRecord = () => ({
  schema: 'leaf.solar-terrain-preview.v1', capability: SLOPE_CAPABILITY, maturity: 'preview', grid_sha256: D,
  meters_per_unit: 1, limits: { ...DEFAULTS }, frames: 2, markers: 0, status: KERNEL_STATUS,
  report_sha256: 'f'.repeat(64),
})
const slopeReport = () => ({
  tracker_count: 2, axial_rows_checked: 2, axial_violation_rows: 0, cross_axis_pairs_checked: 1,
  cross_axis_violation_pairs: 0, row_to_row_pairs_checked: 1, row_to_row_angle_violation_pairs: 0,
  trackers_needing_terrain_following: 0, has_violations: false, status: KERNEL_STATUS,
})
const ABSENT = Object.freeze({ state: 'absent', record: null })
function viewOf({ drawing = 'solar', project = 'p', head, grid = gridOf(), mesh_faces = 0, mesh = ABSENT, slope = ABSENT } = {}) {
  return envelope({
    schema: 'leaf.solar-terrain-view-response.v1', stored: true, head: head ?? headOf({ drawing, project }),
    terrain: {
      schema: 'leaf.solar-terrain-view.v1', maturity: 'preview', frame: frameOf(), grid, mesh_faces, slope_markers: 0,
      previews: { [MESH_CAPABILITY]: mesh, [SLOPE_CAPABILITY]: slope }, drawing_id: drawing, project_id: project,
    },
  })
}
const V0 = () => envelope({ schema: 'leaf.solar-terrain-view-response.v1', stored: false, head: null, terrain: null })
const V1 = () => viewOf({ grid: null })
const V2 = () => viewOf()
// The view after a head moved to H2, a child of H.
const MOVED = () => viewOf({ head: headOf({ artifact: H2, index: 1, parent: H }) })
// An operation result for the head H: a new child H2 when created, the same head when not.
function resultOf(operation, { created = true, project = 'p' } = {}) {
  const body = {
    schema: 'leaf.solar-terrain-operation.v1', maturity: 'preview', operation,
    capability: operation === 'mesh' ? MESH_CAPABILITY : SLOPE_CAPABILITY, created, drawing_id: 'solar',
    project_id: project, frame: frameOf(), grid: operation === 'slope-clear' ? null : gridOf(),
    record: operation === 'mesh' ? meshRecord() : operation === 'slope' ? slopeRecord() : null, replaced: 0,
  }
  if (operation === 'slope') body.report = slopeReport()
  body.head = created ? headOf({ artifact: H2, index: 1, parent: H, project }) : headOf({ project })
  return envelope(body)
}
const ok = (body) => ({ ok: true, status: 200, value: normalize(body) })
const refusedWith = (status, code, retryable) => ({ ok: false, status, code, retryable })
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
// An injected client: reads answer `view`, and operations stay pending unless `run` is given.
function fakeClient({ view = V2, run } = {}) {
  return {
    getTerrain: vi.fn(async () => ok(view())),
    runTerrainOperation: vi.fn(run ?? (() => new Promise(() => {}))),
  }
}
const panel = () => screen.getByTestId('solar-terrain-panel')
const phase = () => panel().getAttribute('data-phase')
const button = (name) => screen.getByRole('button', { name })
const announcement = () => screen.getByTestId('solar-terrain-announce').textContent
const refusalText = () => screen.queryByTestId('solar-terrain-refusal')?.textContent ?? null
const reasonText = () => screen.queryByTestId('solar-terrain-reason')?.textContent ?? null
function summary() {
  const list = screen.queryByTestId('solar-terrain-summary')
  if (!list) return null
  return Object.fromEntries([...list.querySelectorAll('div')]
    .map((row) => [row.getAttribute('data-line'), row.querySelector('dd').textContent]))
}
const ready = () => waitFor(() => expect(phase()).toBe('ready'))
const settle = () => act(async () => { await new Promise((resolve) => { setTimeout(resolve, 20) }) })

afterEach(cleanup)

describe('SolarTerrainPanel', () => {
  it('W20-06a P1 no drawing, no terrain, a head without a grid and a grid each show their own state', async () => {
    const idle = fakeClient()
    render(<SolarTerrainPanel drawingId={null} client={idle} headSignal={0} />)
    expect(phase()).toBe('no-drawing')
    expect(panel().textContent).toBe('Open a drawing to view terrain previews')
    expect(screen.queryByRole('button')).toBeNull()
    await settle()
    expect(idle.getTerrain).not.toHaveBeenCalled()
    expect(idle.runTerrainOperation).not.toHaveBeenCalled()
    cleanup()

    // No physical head: a success with nothing stored, and every operation off.
    const empty = fakeClient({ view: V0 })
    render(<SolarTerrainPanel drawingId="solar" client={empty} headSignal={0} />)
    expect(phase()).toBe('loading')
    expect(reasonText()).toBe('Loading terrain preview')
    for (const [name] of ACTIONS) expect(button(name).disabled).toBe(true)
    await ready()
    expect(empty.getTerrain).toHaveBeenCalledTimes(1)
    expect(empty.getTerrain.mock.calls[0][0]).toMatchObject({ drawingId: 'solar', projectId: null })
    expect(summary()).toEqual({ terrain: 'No terrain is stored for this drawing' })
    for (const [name] of ACTIONS) expect(button(name).disabled).toBe(true)
    expect(reasonText()).toBe(TERRAIN_ROUTE_REASONS.TERRAIN_STATE_NOT_FOUND)
    expect(panel().hasAttribute('data-head')).toBe(false)
    expect(refusalText()).toBeNull()
    cleanup()

    // A head without a grid: mesh and slope are off, clearing the slope preview is not.
    const gridless = fakeClient({ view: V1 })
    render(<SolarTerrainPanel drawingId="solar" projectId="p" client={gridless} headSignal={0} />)
    await ready()
    expect(summary()).toEqual({
      head: H, change: '1 of this drawing', units: 'Meters', crs: 'None', datum: 'Not recorded',
      grid: 'This terrain state has no grid', mesh: 'Not recorded', slope: 'Not recorded',
    })
    expect(button(MESH).disabled).toBe(true)
    expect(button(SLOPE).disabled).toBe(true)
    expect(button(CLEAR).disabled).toBe(false)
    expect(reasonText()).toBe(TERRAIN_ROUTE_REASONS.TERRAIN_GRID_MISSING)
    fireEvent.click(button(CLEAR))
    await waitFor(() => expect(gridless.runTerrainOperation).toHaveBeenCalledTimes(1))
    expect(gridless.runTerrainOperation.mock.calls[0][0]).toMatchObject({ operation: 'slope-clear', expectedHead: H })
    expect('limits' in gridless.runTerrainOperation.mock.calls[0][0]).toBe(false)
    cleanup()

    // A head with a grid, read through the real client from a route-shaped answer.
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify(V2()), {
      status: 200, headers: { 'content-type': 'application/json' },
    }))
    const real = createSolarTerrainClient({
      fetchImpl, apiBase: 'https://studio.test', headers: () => ({ 'X-Tenant-Id': 'fixture-tenant' }),
    })
    render(<SolarTerrainPanel drawingId="solar" projectId="p" client={real} headSignal={0} />)
    await ready()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][0]).toBe('https://studio.test/api/drawings/solar/terrain?project_id=p')
    expect(fetchImpl.mock.calls[0][1].method).toBe('GET')
    expect(summary()).toEqual({
      head: H, change: '1 of this drawing', units: 'Meters', crs: 'None', datum: 'Not recorded',
      grid: '2 by 2 nodes', cell: 'X 10 by Y 10 meters', mesh: 'Not recorded', slope: 'Not recorded',
    })
    expect(panel().getAttribute('data-head')).toBe(H)
    expect(panel().getAttribute('aria-label')).toBe('Terrain preview')
    expect(screen.getByTestId('solar-terrain-summary').getAttribute('aria-label')).toBe('Stored terrain preview')
    for (const [name] of ACTIONS) {
      expect(button(name).disabled).toBe(false)
      expect(name).toContain('preview')
    }
    expect(screen.getByText('Limits for the next slope preview')).toBeTruthy()
    expect(reasonText()).toBeNull()
    expect(refusalText()).toBeNull()
    expect(announcement()).toBe('')
  })

  it('W20-06a P2 an action sends one request naming the displayed head, and locks every action while it runs', async () => {
    const running = { mesh: 'Running the mesh preview', slope: 'Running the slope preview', 'slope-clear': 'Clearing the slope preview' }
    for (const [name, operation] of ACTIONS) {
      const client = fakeClient()
      render(<SolarTerrainPanel drawingId="solar" projectId="p" client={client} headSignal={0} />)
      await ready()
      const target = button(name)
      // Two presses inside one batch: the second lands before any render could disable the button.
      act(() => {
        fireEvent.click(target)
        fireEvent.click(target)
      })
      await waitFor(() => expect(client.runTerrainOperation).toHaveBeenCalledTimes(1))
      expect(target.disabled).toBe(true)
      fireEvent.click(target)
      await settle()
      expect(client.runTerrainOperation).toHaveBeenCalledTimes(1)
      const [options] = client.runTerrainOperation.mock.calls[0]
      expect(Object.keys(options)).toEqual(operation === 'slope'
        ? ['drawingId', 'projectId', 'operation', 'expectedHead', 'limits', 'signal']
        : ['drawingId', 'projectId', 'operation', 'expectedHead', 'signal'])
      expect(options.drawingId).toBe('solar')
      expect(options.projectId).toBe('p')
      expect(options.operation).toBe(operation)
      // The head on screen, not a newer one read out of sight.
      expect(options.expectedHead).toBe(H)
      expect(options.expectedHead).toBe(panel().getAttribute('data-head'))
      if (operation === 'slope') {
        expect(options.limits).toEqual(DEFAULTS)
        for (const value of Object.values(options.limits)) expect(typeof value).toBe('number')
      }
      expect(options.signal.aborted).toBe(false)
      for (const [label] of ACTIONS) expect(button(label).disabled).toBe(true)
      expect(button(REFRESH).disabled).toBe(true)
      expect(phase()).toBe('pending')
      expect(announcement()).toBe(running[operation])
      expect(screen.getByTestId('solar-terrain-announce').getAttribute('aria-live')).toBe('polite')
      // Nothing was read between the press and the request, and the summary stays on screen.
      expect(client.getTerrain).toHaveBeenCalledTimes(1)
      expect(summary().head).toBe(H)
      cleanup()
    }
    // The slope request carries the drafts as numbers, all seven limits.
    const client = fakeClient()
    render(<SolarTerrainPanel drawingId="solar" projectId="p" client={client} headSignal={0} />)
    await ready()
    fireEvent.change(screen.getByLabelText('Axial slope limit, percent'), { target: { value: ' 12.5 ' } })
    fireEvent.change(screen.getByLabelText('Columns'), { target: { value: '3' } })
    fireEvent.click(button(SLOPE))
    await waitFor(() => expect(client.runTerrainOperation).toHaveBeenCalledTimes(1))
    expect(client.runTerrainOperation.mock.calls[0][0].limits).toEqual({ ...DEFAULTS, MaxAxialSlopePct: 12.5, Columns: 3 })
  })

  it('W20-06a P3 a moved head is read once and reviewed, and no operation runs again unasked', async () => {
    for (const code of ['TERRAIN_HEAD_MOVED', 'PHYSICAL_HEAD_CONFLICT']) {
      // The first read shows H; every later read shows the moved head H2.
      let reads = 0
      const client = {
        getTerrain: vi.fn(async () => {
          reads += 1
          return ok(reads === 1 ? V2() : MOVED())
        }),
        runTerrainOperation: vi.fn(async () => refusedWith(409, code, true)),
      }
      render(<SolarTerrainPanel drawingId="solar" projectId="p" client={client} headSignal={0} />)
      await ready()
      expect(panel().getAttribute('data-head')).toBe(H)
      fireEvent.click(button(MESH))
      await waitFor(() => expect(panel().getAttribute('data-head')).toBe(H2))
      await ready()
      await settle()
      expect(client.runTerrainOperation).toHaveBeenCalledTimes(1)
      expect(client.runTerrainOperation.mock.calls[0][0].expectedHead).toBe(H)
      expect(client.getTerrain).toHaveBeenCalledTimes(2)
      // The sentence stays while the refreshed head is reviewed.
      expect(refusalText()).toBe(TERRAIN_ROUTE_REASONS[code])
      expect(summary().head).toBe(H2)
      expect(summary().change).toBe('2 of this drawing')
      expect(announcement()).toBe('')
      expect(button(MESH).disabled).toBe(false)
      // The next press names the refreshed head, and only because it was pressed.
      fireEvent.click(button(MESH))
      await waitFor(() => expect(client.runTerrainOperation).toHaveBeenCalledTimes(2))
      expect(client.runTerrainOperation.mock.calls[1][0].expectedHead).toBe(H2)
      cleanup()
    }
  })

  it('W20-06a P4 a head signal reads again, the newest read wins, and a refresh never turns stale into current', async () => {
    const older = deferred()
    const newer = deferred()
    const client = {
      getTerrain: vi.fn().mockImplementationOnce(() => older.promise).mockImplementationOnce(() => newer.promise),
      runTerrainOperation: vi.fn(),
    }
    const element = (headSignal) => (
      <SolarTerrainPanel drawingId="solar" projectId="p" client={client} headSignal={headSignal} />
    )
    const view = render(element(1))
    await waitFor(() => expect(client.getTerrain).toHaveBeenCalledTimes(1))
    view.rerender(element(2))
    await waitFor(() => expect(client.getTerrain).toHaveBeenCalledTimes(2))
    expect(client.getTerrain.mock.calls[0][0].signal.aborted).toBe(true)
    expect(client.getTerrain.mock.calls[1][0].signal.aborted).toBe(false)
    const staleView = viewOf({
      head: headOf({ artifact: H2, index: 1, parent: H }), mesh_faces: 1, mesh: { state: 'stale', record: meshRecord() },
    })
    const currentView = viewOf({ mesh_faces: 1, mesh: { state: 'current', record: meshRecord() } })
    await act(async () => {
      newer.resolve(ok(staleView))
      await newer.promise
    })
    await ready()
    const staleText = 'Stale, earlier results: 1 face, 1 green, 0 yellow, 0 red, steepest 0 percent'
    expect(summary().mesh).toBe(staleText)
    expect(panel().querySelector('[data-line="mesh"]').getAttribute('data-state')).toBe('stale')
    expect(panel().getAttribute('data-head')).toBe(H2)
    // The older read answers last, with a current record: it changes nothing.
    await act(async () => {
      older.resolve(ok(currentView))
      await older.promise
    })
    await settle()
    expect(summary().mesh).toBe(staleText)
    expect(panel().querySelector('[data-line="mesh"]').getAttribute('data-state')).toBe('stale')
    expect(panel().getAttribute('data-head')).toBe(H2)
    expect(phase()).toBe('ready')
    // The same token again reads nothing, and no operation was ever sent.
    view.rerender(element(2))
    await settle()
    expect(client.getTerrain).toHaveBeenCalledTimes(2)
    expect(client.runTerrainOperation).not.toHaveBeenCalled()
  })

  it('W20-06a P5 an answer for a scope that ended changes nothing, even after returning to it', async () => {
    const changed = vi.fn()
    const reads = []
    const runs = []
    const client = {
      getTerrain: vi.fn(() => {
        const pending = deferred()
        reads.push(pending)
        return pending.promise
      }),
      runTerrainOperation: vi.fn(() => {
        const pending = deferred()
        runs.push(pending)
        return pending.promise
      }),
    }
    const element = (scope = {}) => (
      <SolarTerrainPanel drawingId="solar" projectId="p" client={client} headSignal={0}
        onPhysicalHeadChanged={changed} {...scope} />
    )
    const answer = async (pending, value) => {
      await act(async () => {
        pending.resolve(value)
        await pending.promise
      })
      await settle()
    }
    const view = render(element())
    await waitFor(() => expect(reads).toHaveLength(1))
    await answer(reads[0], ok(V2()))
    await ready()
    fireEvent.click(button(MESH))
    await waitFor(() => expect(runs).toHaveLength(1))
    // Drawing A to B: the operation in flight and nothing of A remains.
    view.rerender(element({ drawingId: 'other' }))
    expect(client.runTerrainOperation.mock.calls[0][0].signal.aborted).toBe(true)
    await waitFor(() => expect(reads).toHaveLength(2))
    expect(phase()).toBe('loading')
    expect(summary()).toBeNull()
    expect(announcement()).toBe('')
    // And back to A, with a third read in flight.
    view.rerender(element())
    await waitFor(() => expect(reads).toHaveLength(3))
    expect(client.getTerrain.mock.calls[1][0].signal.aborted).toBe(true)
    // The first A's operation and B's read answer late. Both would be accepted by a live scope.
    await answer(runs[0], ok(resultOf('mesh')))
    await answer(reads[1], ok(viewOf({ drawing: 'other' })))
    expect(changed).not.toHaveBeenCalled()
    expect(announcement()).toBe('')
    expect(refusalText()).toBeNull()
    expect(phase()).toBe('loading')
    expect(summary()).toBeNull()
    expect(reads).toHaveLength(3)
    expect(runs).toHaveLength(1)
    // The live scope still reads normally.
    await answer(reads[2], ok(V1()))
    await ready()
    expect(summary().grid).toBe('This terrain state has no grid')
    // A project switch ends the scope the same way.
    fireEvent.click(button(CLEAR))
    await waitFor(() => expect(runs).toHaveLength(2))
    view.rerender(element({ projectId: 'q' }))
    expect(client.runTerrainOperation.mock.calls[1][0].signal.aborted).toBe(true)
    await waitFor(() => expect(reads).toHaveLength(4))
    await answer(runs[1], ok(resultOf('slope-clear')))
    expect(changed).not.toHaveBeenCalled()
    expect(announcement()).toBe('')
    expect(reads).toHaveLength(4)
    // And so does an unmount, for a read and for an operation.
    await answer(reads[3], ok(viewOf({ project: 'q' })))
    await ready()
    fireEvent.click(button(MESH))
    await waitFor(() => expect(runs).toHaveLength(3))
    fireEvent.click(button(REFRESH))
    expect(reads).toHaveLength(4)
    view.unmount()
    expect(client.runTerrainOperation.mock.calls[2][0].signal.aborted).toBe(true)
    await answer(runs[2], ok(resultOf('mesh', { project: 'q' })))
    expect(changed).not.toHaveBeenCalled()
    expect(reads).toHaveLength(4)
    expect(runs).toHaveLength(3)
  })

  it('W20-06a P6 every refusal shows exactly its sentence, keeps the drafts, and nothing is retried unasked', async () => {
    const sentences = [...Object.entries(TERRAIN_ROUTE_REASONS), ...Object.entries(TERRAIN_CLIENT_REASONS)]
    expect(sentences).toHaveLength(47)
    const draft = () => screen.getByLabelText('Axial slope limit, percent')
    const refuse = async (run) => {
      const client = fakeClient({ run })
      render(<SolarTerrainPanel drawingId="solar" projectId="p" client={client} headSignal={0} />)
      await ready()
      fireEvent.change(draft(), { target: { value: '12.5' } })
      fireEvent.click(button(MESH))
      await waitFor(() => expect(refusalText()).not.toBeNull())
      await settle()
      return client
    }
    for (const [code, sentence] of sentences) {
      const client = await refuse(async () => refusedWith(409, code, false))
      expect(refusalText()).toBe(sentence)
      expect(screen.getByTestId('solar-terrain-refusal').getAttribute('role')).toBe('alert')
      expect(draft().value).toBe('12.5')
      expect(announcement()).toBe('')
      expect(document.body.textContent).not.toContain(code)
      // No automatic retry, and no read unless the head is known to have moved.
      expect(client.runTerrainOperation).toHaveBeenCalledTimes(1)
      const moved = code === 'TERRAIN_HEAD_MOVED' || code === 'PHYSICAL_HEAD_CONFLICT'
      expect(client.getTerrain).toHaveBeenCalledTimes(moved ? 2 : 1)
      cleanup()
    }
    // A code with no sentence shows the bounded fallback, never the server's prose.
    await refuse(async () => ({ ...refusedWith(400, 'TERRAIN_FUTURE_CODE', false), message: 'raw server prose' }))
    expect(refusalText()).toBe('The terrain request stopped (TERRAIN_FUTURE_CODE)')
    expect(document.body.textContent).not.toContain('raw server prose')
    cleanup()
    // Anything that is neither a readable success nor a coded refusal is an unreadable answer.
    const unreadable = [
      () => Promise.reject(new Error('boom')),
      () => { throw new Error('boom') },
      async () => ({ ok: false, status: 400 }),
      async () => ({ ok: false, status: 400, code: 'bad code', retryable: false }),
      async () => ({ ok: true, status: 200, value: {} }),
      async () => ok(resultOf('slope-clear')),
      async () => null,
    ]
    for (const run of unreadable) {
      const client = await refuse(run)
      expect(refusalText()).toBe(UNREADABLE)
      expect(announcement()).toBe('')
      expect(client.runTerrainOperation).toHaveBeenCalledTimes(1)
      cleanup()
    }
    // A retryable refusal is retried only by pressing again, and each press is one request.
    const drained = await refuse(async () => refusedWith(503, 'TERRAIN_WRITES_DRAINED', true))
    expect(refusalText()).toBe(TERRAIN_ROUTE_REASONS.TERRAIN_WRITES_DRAINED)
    expect(drained.runTerrainOperation).toHaveBeenCalledTimes(1)
    expect(button(MESH).disabled).toBe(false)
    fireEvent.click(button(MESH))
    await waitFor(() => expect(drained.runTerrainOperation).toHaveBeenCalledTimes(2))
    await settle()
    expect(drained.runTerrainOperation).toHaveBeenCalledTimes(2)
    expect(drained.getTerrain).toHaveBeenCalledTimes(1)
    expect(draft().value).toBe('12.5')
    cleanup()
    // A lost answer leaves the view unconfirmed: actions stay off until Refresh is pressed.
    const lost = await refuse(async () => refusedWith(null, 'TERRAIN_CLIENT_TIMEOUT', true))
    expect(refusalText()).toBe(TERRAIN_CLIENT_REASONS.TERRAIN_CLIENT_TIMEOUT)
    expect(phase()).toBe('refused')
    for (const [name] of ACTIONS) expect(button(name).disabled).toBe(true)
    expect(lost.getTerrain).toHaveBeenCalledTimes(1)
    expect(summary().head).toBe(H)
    fireEvent.click(button(REFRESH))
    await waitFor(() => expect(lost.getTerrain).toHaveBeenCalledTimes(2))
    await ready()
    for (const [name] of ACTIONS) expect(button(name).disabled).toBe(false)
    expect(lost.runTerrainOperation).toHaveBeenCalledTimes(1)
    expect(draft().value).toBe('12.5')
    cleanup()
    // A read that fails shows its sentence, keeps every action off and offers Refresh.
    const failing = {
      getTerrain: vi.fn()
        .mockImplementationOnce(async () => refusedWith(503, 'TERRAIN_STORE_UNAVAILABLE', true))
        .mockImplementation(async () => ok(V2())),
      runTerrainOperation: vi.fn(),
    }
    render(<SolarTerrainPanel drawingId="solar" projectId="p" client={failing} headSignal={0} />)
    await waitFor(() => expect(refusalText()).toBe(TERRAIN_ROUTE_REASONS.TERRAIN_STORE_UNAVAILABLE))
    await settle()
    expect(phase()).toBe('refused')
    expect(summary()).toBeNull()
    for (const [name] of ACTIONS) expect(button(name).disabled).toBe(true)
    expect(failing.getTerrain).toHaveBeenCalledTimes(1)
    fireEvent.click(button(REFRESH))
    await ready()
    expect(failing.getTerrain).toHaveBeenCalledTimes(2)
    expect(refusalText()).toBeNull()
    expect(summary().head).toBe(H)
    expect(failing.runTerrainOperation).not.toHaveBeenCalled()
  }, 60_000)

  it('W20-06a P7 an unreadable limit shows its reason before any press, and a disabled panel sends nothing', async () => {
    const client = fakeClient()
    const element = (extra = {}) => (
      <SolarTerrainPanel drawingId="solar" projectId="p" client={client} headSignal={0} {...extra} />
    )
    const view = render(element())
    await ready()
    const cases = [
      ['North to south slope limit, percent', '1000.1', 'Enter a decimal number from 0 to 1000'],
      ['Row to row slope limit, degrees', '90.1', 'Enter a decimal number from 0 to 90'],
      ['Columns', '1.5', 'Enter a whole number from 0 to 10000'],
      ['Overall slope limit, percent', '1e2', 'Enter a decimal number from 0 to 1000'],
      ['Axial slope limit, percent', 'x'.repeat(65), 'Enter a decimal number from 0 to 1000'],
    ]
    for (const [label, draft, reason] of cases) {
      const input = screen.getByLabelText(label)
      expect(input.getAttribute('aria-invalid')).toBeNull()
      fireEvent.change(input, { target: { value: draft } })
      expect(input.value).toBe(draft)
      expect(input.getAttribute('aria-invalid')).toBe('true')
      const shown = screen.getByTestId('solar-terrain-draft-reason')
      expect(shown.textContent).toBe(reason)
      expect(input.getAttribute('aria-describedby').split(' ')).toContain(shown.id)
      expect(reasonText()).toBe(`${label}: ${reason}`)
      expect(button(SLOPE).disabled).toBe(true)
      // Only the slope preview reads the limits.
      expect(button(MESH).disabled).toBe(false)
      expect(button(CLEAR).disabled).toBe(false)
      fireEvent.click(button(SLOPE))
      await settle()
      expect(client.runTerrainOperation).not.toHaveBeenCalled()
      fireEvent.change(input, { target: { value: '' } })
      expect(input.getAttribute('aria-invalid')).toBeNull()
      expect(screen.queryByTestId('solar-terrain-draft-reason')).toBeNull()
      expect(reasonText()).toBeNull()
      expect(button(SLOPE).disabled).toBe(false)
    }
    // The limits at their edges are readable.
    fireEvent.change(screen.getByLabelText('North to south slope limit, percent'), { target: { value: '1000' } })
    fireEvent.change(screen.getByLabelText('Row to row slope limit, degrees'), { target: { value: '90' } })
    fireEvent.change(screen.getByLabelText('Columns'), { target: { value: '10000' } })
    expect(reasonText()).toBeNull()
    expect(button(SLOPE).disabled).toBe(false)
    // Valid drafts and a disabled panel: the reason is shown, the view stays, nothing is sent.
    view.rerender(element({ disabled: true }))
    expect(reasonText()).toBe(BUSY)
    for (const [name] of ACTIONS) {
      expect(button(name).disabled).toBe(true)
      fireEvent.click(button(name))
    }
    await settle()
    expect(client.runTerrainOperation).not.toHaveBeenCalled()
    expect(summary().head).toBe(H)
    expect(screen.getByLabelText('Columns').value).toBe('10000')
    view.rerender(element({ disabled: true, disabledReason: 'A run is in progress, so wait for it to finish' }))
    expect(reasonText()).toBe('A run is in progress, so wait for it to finish')
    view.rerender(element({ disabled: true, disabledReason: 'y'.repeat(300) }))
    expect(reasonText()).toBe('y'.repeat(200))
    for (const blank of ['', '   ', 7, null]) {
      view.rerender(element({ disabled: true, disabledReason: blank }))
      expect(reasonText()).toBe(BUSY)
    }
    // Anything but an explicit false keeps the panel off.
    view.rerender(element({ disabled: null }))
    for (const [name] of ACTIONS) expect(button(name).disabled).toBe(true)
    view.rerender(element({ disabled: false, disabledReason: 'ignored while enabled' }))
    expect(reasonText()).toBeNull()
    for (const [name] of ACTIONS) expect(button(name).disabled).toBe(false)
    expect(client.runTerrainOperation).not.toHaveBeenCalled()
    expect(client.getTerrain).toHaveBeenCalledTimes(1)
    cleanup()
    // A client that cannot be called leaves every action off and makes no request.
    render(<SolarTerrainPanel drawingId="solar" projectId="p" client={{ getTerrain: vi.fn() }} headSignal={0} />)
    expect(reasonText()).toBe(TERRAIN_CLIENT_REASONS.TERRAIN_CLIENT_REQUEST_INVALID)
    for (const [name] of ACTIONS) expect(button(name).disabled).toBe(true)
    expect(button(REFRESH).disabled).toBe(true)
  })

  it('W20-06a P8 an outcome is announced as a preview, the view is read again, and the kernel status never reaches the page', async () => {
    const moveFocusToBody = () => {
      const temporary = document.createElement('button')
      document.body.appendChild(temporary)
      temporary.focus()
      temporary.remove()
    }
    const updated = { mesh: 'The mesh preview was updated', slope: 'The slope preview was updated', 'slope-clear': 'The slope preview was cleared' }
    const after = {
      mesh: () => viewOf({
        head: headOf({ artifact: H2, index: 1, parent: H }), mesh_faces: 1, mesh: { state: 'current', record: meshRecord() },
      }),
      slope: () => viewOf({
        head: headOf({ artifact: H2, index: 1, parent: H }), slope: { state: 'current', record: slopeRecord() },
      }),
      'slope-clear': MOVED,
    }
    for (const [name, operation] of ACTIONS) {
      for (const created of [true, false]) {
        const changed = vi.fn()
        const running = deferred()
        const client = {
          getTerrain: vi.fn()
            .mockImplementationOnce(async () => ok(V2()))
            .mockImplementation(async () => ok(created ? after[operation]() : V2())),
          runTerrainOperation: vi.fn(() => running.promise),
        }
        render(<SolarTerrainPanel drawingId="solar" projectId="p" client={client} headSignal={0}
          onPhysicalHeadChanged={changed} />)
        await ready()
        const target = button(name)
        target.focus()
        expect(document.activeElement).toBe(target)
        fireEvent.click(target)
        await waitFor(() => expect(client.runTerrainOperation).toHaveBeenCalledTimes(1))
        // A browser takes focus from a control once it is disabled.
        moveFocusToBody()
        expect(document.activeElement).toBe(document.body)
        const result = resultOf(operation, { created })
        await act(async () => {
          running.resolve(ok(result))
          await running.promise
        })
        // The view is read again after the operation settles.
        await waitFor(() => expect(client.getTerrain).toHaveBeenCalledTimes(2))
        await ready()
        await settle()
        expect(announcement()).toBe(created ? updated[operation] : NO_CHANGE)
        expect(announcement()).toContain('preview')
        expect(refusalText()).toBeNull()
        // A new head is reported once, with the validated result; a no-op is not a new head.
        expect(changed).toHaveBeenCalledTimes(created ? 1 : 0)
        if (created) expect(changed).toHaveBeenCalledWith(normalize(result))
        expect(panel().getAttribute('data-head')).toBe(created ? H2 : H)
        expect(document.activeElement).toBe(button(name))
        expect(client.runTerrainOperation).toHaveBeenCalledTimes(1)
        expect(client.getTerrain).toHaveBeenCalledTimes(2)
        if (created && operation === 'slope') {
          expect(summary().slope).toBe('Current: 2 tracker frames read, 0 rows over the limits of this preview')
        }
        // Text, attributes, titles and live regions alike: the kernel wording is nowhere.
        for (const phrase of ['ASCE', 'budget', 'Tracker slope', 'within']) {
          expect(document.body.innerHTML).not.toContain(phrase)
        }
        cleanup()
      }
    }
    // A callback that throws does not hide the outcome or stop the read.
    const thrower = vi.fn(() => { throw new Error('mount') })
    const client = fakeClient({ run: async () => ok(resultOf('mesh')) })
    render(<SolarTerrainPanel drawingId="solar" projectId="p" client={client} headSignal={0}
      onPhysicalHeadChanged={thrower} />)
    await ready()
    fireEvent.click(button(MESH))
    await waitFor(() => expect(announcement()).toBe('The mesh preview was updated'))
    await waitFor(() => expect(client.getTerrain).toHaveBeenCalledTimes(2))
    expect(thrower).toHaveBeenCalledTimes(1)
  })
})
