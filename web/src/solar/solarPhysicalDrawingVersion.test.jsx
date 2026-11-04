// @vitest-environment jsdom
import React, { useEffect } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { validateLandxmlImport } from './solarLandxmlClient.js'
import * as landxmlClients from './solarLandxmlClient.js'
import { validateTerrainOperation } from './solarTerrainModel.js'
import { validateCivilOperation } from './solarCivilModel.js'
import { validateTrackerRowsResult } from './solarTrackerRowsModel.js'
import * as terrainClients from './solarTerrainClient.js'
import * as civilClients from './solarCivilClient.js'
import * as landxmlPanel from './SolarLandxmlUpload.jsx'
import * as terrainPanel from './SolarTerrainPanel.jsx'
import * as civilPanel from './SolarCivilPanel.jsx'
import * as trackerPanel from './SolarTrackerRowsPanel.jsx'
import * as readPanel from './SolarPhysicalReadPanel.jsx'
import SolarWorkspaceTools from './SolarWorkspaceTools.jsx'

const H = 'a'.repeat(64)
const B = 'b'.repeat(64)
const D = 'c'.repeat(64)
const reference = (artifact = B, media = 'application/json', filename = 'physical-state.json') => ({
  schema: 'leaf.solar-artifact-ref.v1', artifact_id: artifact, content_sha256: D, media_type: media,
  filename, byte_length: 32, source_version: 1, download: `/api/drawings/solar/artifacts/${artifact}`,
})
const head = (index = 3, parent = H, artifact = B) => ({
  schema: 'leaf.solar-physical-head.v1', drawing_id: 'solar', project_id: 'p', index, parent,
  state: reference(artifact),
})
const frame = () => ({ coordinate_system: 'world', transform: 'identity', drawing_units: 'm',
  meters_per_unit: 1, crs: 'none', elevation_datum: 'unrecorded', horizontal: 'drawing-units', elevation: 'metres' })
const standing = () => ({ schema: 'leaf.solar-frames-piles-terrain-standing.v1', maturity: 'preview',
  grid_sha256: null, frames: { state: 'absent', checked: 0, stale: 0 }, piles: { state: 'absent', checked: 0, stale: 0 } })
const row = () => ({ axis_start: [0, 0], axis_end: [0, 6], cross_axis_width_du: 2, slots: 3 })
const version = (created = true) => ({ version: 4, parent_version: created ? 2 : 4, created })

function fixture(kind, parent = H) {
  if (kind === 'landxml') {
    const body = { schema: 'leaf.solar-landxml-import.v1', created: true, drawing_id: 'solar', project_id: 'p',
      source: reference(D, 'application/xml', 'landxml-source.xml'),
      interpretation: { point_order: 'northing-easting-elevation', drawing_x: 'easting', drawing_y: 'northing',
        linear_unit: 'meter', meters_per_source_unit: 1, drawing_units: 'm', meters_per_drawing_unit: 1,
        horizontal_scale: 1, elevation_scale: 1, crs: 'none', crs_source: 'declared', elevation_datum: 'unrecorded' },
      points: { declared: 3, accepted: 3, skipped: 0 },
      grid: { rows: 2, cols: 2, target_cells: 2, x_min: 0, x_max: 10, y_min: 0, y_max: 10 }, head: head(3, parent) }
    return { body, validate: (b) => validateLandxmlImport(b, { drawingId: 'solar', projectId: 'p', drawingUnits: 'm',
      crs: 'none', targetCells: 2, byteLength: 32 }) }
  }
  if (kind === 'terrain') {
    const body = { schema: 'leaf.solar-terrain-operation.v1', maturity: 'preview', operation: 'slope-clear',
      capability: 'tracker-slope-violations', created: parent !== null, drawing_id: 'solar', project_id: 'p',
      frame: frame(), grid: null, record: null, replaced: 0, head: head(3, parent, parent === null ? H : B) }
    return { body, validate: (b) => validateTerrainOperation(b,
      { drawingId: 'solar', projectId: 'p', operation: 'slope-clear', expectedHead: H }) }
  }
  if (kind === 'civil') {
    const body = { schema: 'leaf.solar-frames-piles.v1', operation: 'frame-generate', maturity: 'preview',
      outcome: 'published', created: true, drawing_id: 'solar', project_id: 'p', base: parent,
      units: { drawing_units: 'm', meters_per_unit: 1 },
      terrain: { present: false, sampled: false, rows: null, cols: null, grid_sha256: null },
      summary: { frames_added: 0, frames_off_terrain: 0, preset_name: 'Tiny' },
      preview: { schema: 'leaf.solar-frames-piles-preview.v1', maturity: 'preview', frames: 0, piles: 0,
        collision_markers: 0, range_markers: 0, terrain: false }, standing: standing(), head: head(3, parent) }
    return { body, validate: (b) => validateCivilOperation(b,
      { drawingId: 'solar', projectId: 'p', body: { operation: 'frame-generate', expected_head: parent } }) }
  }
  const body = { schema: 'leaf.solar-tracker-rows.v1', operation: 'manual-create', outcome: 'published',
    created: true, drawing_id: 'solar', project_id: 'p', expected_head: parent, head: head(3, parent), frame: frame(),
    summary: { rows: 1, slots: 3, module_power_watts: 450 }, terrain_standing: standing(), error: null, degraded_mode: false }
  return { body, validate: (b) => validateTrackerRowsResult(b, { drawingId: 'solar', projectId: 'p', drawingUnits: 'm',
    request: { operation: 'manual-create', rows: [row()], module_power_watts: 450, expected_head: parent }, status: 201 }) }
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe.each(['landxml', 'terrain', 'civil', 'tracker'])('%s optional physical version receipt', (kind) => {
  it('accepts old responses and copies either valid receipt', () => {
    const { body, validate } = fixture(kind)
    expect(validate(body)).not.toBeNull()
    expect(validate(body)).not.toHaveProperty('drawing_version')
    for (const created of [true, false]) {
      body.drawing_version = version(created)
      const result = validate(body)
      expect(result?.drawing_version).toEqual(body.drawing_version)
      expect(result?.drawing_version).not.toBe(body.drawing_version)
    }
  })
  it.each([
    { version: 4, parent_version: 2, created: false },
    { version: 2, parent_version: 2, created: true },
    { version: 1, parent_version: 2, created: true },
    { version: 2.5, parent_version: 2, created: true },
    { version: 0, parent_version: 0, created: false },
    { version: 4, parent_version: 2, created: true, extra: true },
    null,
  ])('rejects malformed receipts %j', (bad) => {
    const { body, validate } = fixture(kind)
    expect(validate({ ...body, drawing_version: bad })).toBeNull()
  })
  it('accepts an orphan-branch view with null document parent, but never index zero with a parent', () => {
    const empty = fixture(kind, null)
    expect(empty.validate(empty.body)).not.toBeNull()
    const nonempty = fixture(kind)
    nonempty.body.head.index = 0
    expect(nonempty.validate(nonempty.body)).toBeNull()
  })
  it('accepts edit-after-undo document parents independently of log predecessors', () => {
    const { body, validate } = fixture(kind)
    body.head.index = 7
    body.drawing_version = version()
    expect(validate(body)?.head.parent).toBe(H)
  })
})

function workspaceMocks() {
  const props = {}
  const terrain = { getTerrain: vi.fn(async () => ({ ok: true, value: terrainView(head(7)) })), runTerrainOperation: vi.fn() }
  const civil = { getCivil: vi.fn(async () => ({ ok: true, value: { head: head(7) } })), runCivilOperation: vi.fn() }
  vi.spyOn(terrainClients, 'createSolarTerrainClient').mockReturnValue(terrain)
  vi.spyOn(civilClients, 'createSolarCivilClient').mockReturnValue(civil)
  vi.spyOn(landxmlClients, 'createSolarLandxmlClient').mockReturnValue({ uploadLandxml: vi.fn(async () => ({ ok: true })) })
  vi.spyOn(landxmlPanel, 'default').mockImplementation((p) => { props.landxml = p; return null })
  for (const [name, module] of [['terrain', terrainPanel], ['civil', civilPanel], ['tracker', trackerPanel], ['read', readPanel]]) {
    vi.spyOn(module, 'default').mockImplementation((p) => {
      props[name] = p
      useEffect(() => {
        if (name === 'civil') p.client.getCivil({ drawingId: 'solar', projectId: 'p' })
        else if (name !== 'tracker' || p.active) (p.terrainClient ?? p.client).getTerrain({ drawingId: 'solar', projectId: 'p' })
      }, [p.headSignal])
      return null
    })
  }
  return { props, terrain, civil }
}

function terrainView(view) {
  return { schema: 'leaf.solar-terrain-view-response.v1', stored: true, head: view,
    terrain: { schema: 'leaf.solar-terrain-view.v1', maturity: 'preview', drawing_id: 'solar', project_id: 'p',
      frame: frame(), grid: null, mesh_faces: 0, slope_markers: 0,
      previews: { 'terrain-mesh-render': { state: 'absent', record: null },
        'tracker-slope-violations': { state: 'absent', record: null } } } }
}

const openText = { landxml: 'Import LandXML terrain', terrain: 'Terrain preview', civil: 'Civil operations',
  tracker: 'Create tracker rows', read: 'Physical reads' }
const workspaceProps = () => ({ drawingId: 'solar', projectId: 'p', drawingVersion: 2, flow: 'ground-physical',
  checkoutHeld: true, busy: false, onDrawingVersionChanged: vi.fn(async () => true) })

describe('workspace physical version seating', () => {
  it.each(['landxml', 'terrain', 'civil', 'tracker'])('seats %s once without entering refreshing', async (kind) => {
    const mocks = workspaceMocks()
    const p = workspaceProps()
    render(<SolarWorkspaceTools {...p} />)
    fireEvent.click(screen.getByRole('button', { name: openText[kind] }))
    await waitFor(() => expect(mocks.props[kind]).toBeDefined())
    const notify = async (value) => {
      if (kind === 'landxml') {
        // Import observation requires the same request-start scope as the real uploader.
        await mocks.props.landxml.upload({ drawingId: 'solar', projectId: 'p' })
        mocks.props.landxml.onImported(value)
      } else mocks.props[kind].onPhysicalHeadChanged(value)
    }
    const value = { drawing_id: 'solar', created: true, head: head(), drawing_version: version() }
    await act(async () => notify(value))
    expect(p.onDrawingVersionChanged).toHaveBeenCalledTimes(1)
    const [receipt, unused, options] = p.onDrawingVersionChanged.mock.calls[0]
    expect(receipt).toEqual({ drawing_id: 'solar', version: 4, parent: 2 })
    expect(unused).toBeUndefined()
    expect(options.announce).toBe(true)
    expect(options.isCurrent()).toBe(true)
    await act(async () => notify({ ...value, created: false, drawing_version: version(false) }))
    await act(async () => notify({ ...value, drawing_version: { version: 2, parent_version: 1, created: true } }))
    expect(p.onDrawingVersionChanged).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Civil operations' }))
    await waitFor(() => expect(mocks.props.civil.disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Create tracker rows' }))
    await waitFor(() => expect(mocks.props.tracker.blockedReason).not.toBe('drawing_refresh'))
  })

  it.each(['terrain', 'civil', 'tracker', 'read'])('re-reads %s once on undo and accepts a lower log index', async (kind) => {
    const mocks = workspaceMocks()
    const p = workspaceProps()
    const { rerender, container } = render(<SolarWorkspaceTools {...p} />)
    fireEvent.click(screen.getByRole('button', { name: openText[kind] }))
    const get = kind === 'civil' ? mocks.civil.getCivil : mocks.terrain.getTerrain
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(container.querySelector('[data-physical-head-index]')?.getAttribute('data-physical-head-index')).toBe('7'))
    if (kind === 'civil') get.mockResolvedValue({ ok: true, value: { head: head(1, null, H) } })
    else get.mockResolvedValue({ ok: true, value: terrainView(head(1, null, H)) })
    rerender(<SolarWorkspaceTools {...p} drawingVersion={1} />)
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(container.querySelector('[data-physical-head-index]')?.getAttribute('data-physical-head-index')).toBe('1'))
  })

  it('ignores a terrain read started before the version changed', async () => {
    const mocks = workspaceMocks()
    let resolve
    mocks.terrain.getTerrain.mockReturnValueOnce(new Promise((done) => { resolve = done }))
    const p = workspaceProps()
    const { rerender, container } = render(<SolarWorkspaceTools {...p} />)
    fireEvent.click(screen.getByRole('button', { name: 'Terrain preview' }))
    await waitFor(() => expect(mocks.terrain.getTerrain).toHaveBeenCalledTimes(1))
    mocks.terrain.getTerrain.mockResolvedValue({ ok: true, value: terrainView(head(1, null, H)) })
    rerender(<SolarWorkspaceTools {...p} drawingVersion={1} />)
    await waitFor(() => expect(mocks.terrain.getTerrain).toHaveBeenCalledTimes(2))
    await act(async () => resolve({ ok: true, value: terrainView(head(99)) }))
    expect(container.querySelector('[data-physical-head-index]')?.getAttribute('data-physical-head-index')).toBe('1')
  })
})

function fillTrackerRow() {
  fireEvent.change(screen.getByLabelText('Module power'), { target: { value: '450' } })
  const group = within(screen.getByRole('group', { name: 'Row 1', exact: true }))
  for (const [label, value] of [['Axis start X', '0'], ['Axis start Y', '0'], ['Axis end X', '0'],
    ['Axis end Y', '6'], ['Cross axis width', '2'], ['Slots', '3']]) {
    fireEvent.change(group.getByLabelText(label), { target: { value } })
  }
}

describe('tracker context ordering across drawing versions', () => {
  it('uses the lower-index re-read head for the next expected_head', async () => {
    const p = { drawingId: 'solar', projectId: 'p', drawingVersion: 4, active: true, checkoutHeld: true, busy: false,
      terrainClient: { getTerrain: vi.fn(async () => ({ ok: true, value: terrainView(head(7, null, H)) })) },
      client: { createTrackerRows: vi.fn(async () => ({ ok: false, code: 'TRACKER_ROWS_CLIENT_NETWORK' })) } }
    const view = render(<trackerPanel.default {...p} />)
    await waitFor(() => expect(screen.getByTestId('solar-tracker-rows-panel').getAttribute('data-head')).toBe(H))
    p.terrainClient.getTerrain.mockResolvedValue({ ok: true, value: terrainView(head(1, null, D)) })
    view.rerender(<trackerPanel.default {...p} drawingVersion={2} />)
    await waitFor(() => expect(screen.getByTestId('solar-tracker-rows-panel').getAttribute('data-head')).toBe(D))
    expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(2)
    fillTrackerRow()
    fireEvent.click(screen.getByRole('button', { name: 'Publish tracker rows' }))
    await waitFor(() => expect(p.client.createTrackerRows).toHaveBeenCalledTimes(1))
    expect(p.client.createTrackerRows.mock.calls[0][0].request.expected_head).toBe(D)
  })

  it('marks a matching edit-after-undo read retry-ready', async () => {
    const p = { drawingId: 'solar', projectId: 'p', drawingVersion: 4, active: true, checkoutHeld: true, busy: false,
      terrainClient: { getTerrain: vi.fn(async () => ({ ok: true, value: terrainView(head(7, null, H)) })) },
      client: { createTrackerRows: vi.fn(async () => ({ ok: false, code: 'TRACKER_ROWS_CLIENT_NETWORK' })) } }
    render(<trackerPanel.default {...p} />)
    await waitFor(() => expect(screen.getByTestId('solar-tracker-rows-panel').getAttribute('data-head')).toBe(H))
    fillTrackerRow()
    fireEvent.click(screen.getByRole('button', { name: 'Publish tracker rows' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh physical state' }).disabled).toBe(false))
    // The document replaced H, while the intervening log predecessor is unrelated.
    p.terrainClient.getTerrain.mockResolvedValue({ ok: true, value: terrainView(head(9, H, D)) })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh physical state' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry original request' }).disabled).toBe(false))
    expect(screen.getByTestId('solar-tracker-rows-panel').getAttribute('data-phase')).toBe('retry-ready')
  })
})
