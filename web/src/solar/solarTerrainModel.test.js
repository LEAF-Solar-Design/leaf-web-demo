// @vitest-environment node
// sf-w5-terrain-panel-files (W20-06a): the pure model of the Ground Physical terrain panel.
// Every fixture below is SYNTHETIC: built here in the shape server/routers/solar_terrain.py and
// server/solar_ground_terrain_adapter.py document, never captured from a running server. A
// success fixture is one the real validator accepts, and each row proves that before it changes
// one thing about it.
import { describe, expect, it } from 'vitest'
import {
  TERRAIN_CAPABILITIES, TERRAIN_EMPTY_SENTENCE, TERRAIN_LIMIT_DEFAULTS, TERRAIN_LIMIT_FIELDS, TERRAIN_LIMIT_KEYS,
  TERRAIN_MAX_DRAFT_CHARS, TERRAIN_MESH_CAPABILITY, TERRAIN_NO_GRID_SENTENCE, TERRAIN_OPERATIONS,
  TERRAIN_SLOPE_CAPABILITY, buildTerrainOperation, parseTerrainLimitDrafts, resolveTerrainLimits,
  terrainPreviewSummary, terrainSummary, validateTerrainOperation, validateTerrainView,
} from './solarTerrainModel.js'

const H = 'a'.repeat(64)
const H2 = 'b'.repeat(64)
const D = 'c'.repeat(64)
const SCOPE = Object.freeze({ drawingId: 'solar', projectId: 'p' })
const DEFAULTS = Object.freeze({
  MaxNsSlopePct: 8.5, MaxRowToRowEwSlopePct: 10, MaxAxialSlopePct: 8.5, MaxCrossAxisSlopePct: 10,
  MaxRowToRowSlopeDeg: 4, MaxSlopePercent: 15, Columns: 0,
})
// The kernel's own status wording for a run with nothing over its limits. It must never be shown.
const KERNEL_STATUS = 'Tracker slope: 0/2 axial / 0/1 cross - within ASCE 7-16 budget.'

const clone = (value) => JSON.parse(JSON.stringify(value))
const envelope = (body) => ({ ...body, error: null, degraded_mode: false })
const normalize = (body) => {
  const copy = clone(body)
  delete copy.error
  delete copy.degraded_mode
  return copy
}
const headOf = (artifact = H, index = 0, parent = null) => ({
  schema: 'leaf.solar-physical-head.v1', drawing_id: 'solar', project_id: 'p', index, parent,
  state: {
    artifact_id: artifact, media_type: 'application/json', filename: 'physical-state.json', byte_length: 1024,
    content_sha256: 'd'.repeat(64), source_version: 1, schema: 'leaf.solar-artifact-ref.v1',
    download: `/api/drawings/solar/artifacts/${artifact}`,
  },
})
const frameOf = (units = 'm') => ({
  coordinate_system: 'world', transform: 'identity', drawing_units: units,
  meters_per_unit: units === 'm' ? 1 : 0.3048, crs: 'none', elevation_datum: 'unrecorded',
  horizontal: 'drawing-units', elevation: 'metres',
})
// A grid summary whose cells follow the adapter's own formulas.
function gridOf({ rows = 2, cols = 2, x_min = 0, x_max = 10, y_min = 0, y_max = 10, mpu = 1, lo = 0, hi = 0 } = {}) {
  const cell_x = (x_max - x_min) / (cols - 1)
  const cell_y = (y_max - y_min) / (rows - 1)
  return {
    rows, cols, x_min, x_max, y_min, y_max, cell_x, cell_y, cell_x_m: cell_x * mpu, cell_y_m: cell_y * mpu,
    elevation_min_m: lo, elevation_max_m: hi, grid_sha256: D,
  }
}
const previewsOf = ({ mesh, slope } = {}) => ({
  [TERRAIN_MESH_CAPABILITY]: mesh ?? { state: 'absent', record: null },
  [TERRAIN_SLOPE_CAPABILITY]: slope ?? { state: 'absent', record: null },
})
const viewOf = ({ head = headOf(), frame = frameOf(), grid = null, mesh_faces = 0, slope_markers = 0, previews } = {}) => envelope({
  schema: 'leaf.solar-terrain-view-response.v1', stored: true, head,
  terrain: {
    schema: 'leaf.solar-terrain-view.v1', maturity: 'preview', frame, grid, mesh_faces, slope_markers,
    previews: previews ?? previewsOf(), drawing_id: 'solar', project_id: 'p',
  },
})
const V0 = () => envelope({ schema: 'leaf.solar-terrain-view-response.v1', stored: false, head: null, terrain: null })
const V1 = () => viewOf()
const V2 = () => viewOf({ grid: gridOf() })
const meshRecord = (overrides = {}) => ({
  schema: 'leaf.solar-terrain-preview.v1', capability: TERRAIN_MESH_CAPABILITY, maturity: 'preview',
  grid_sha256: D, meters_per_unit: 1, faces: 1, buckets: { Green: 1, Yellow: 0, Red: 0 }, max_slope_percent: 0,
  mesh_sha256: 'e'.repeat(64), ...overrides,
})
const slopeRecord = (overrides = {}) => ({
  schema: 'leaf.solar-terrain-preview.v1', capability: TERRAIN_SLOPE_CAPABILITY, maturity: 'preview',
  grid_sha256: D, meters_per_unit: 1, limits: { ...DEFAULTS }, frames: 2, markers: 0, status: KERNEL_STATUS,
  report_sha256: 'f'.repeat(64), ...overrides,
})
const slopeReport = (overrides = {}) => ({
  tracker_count: 2, axial_rows_checked: 2, axial_violation_rows: 0, cross_axis_pairs_checked: 1,
  cross_axis_violation_pairs: 0, row_to_row_pairs_checked: 1, row_to_row_angle_violation_pairs: 0,
  trackers_needing_terrain_following: 0, has_violations: false, status: KERNEL_STATUS, ...overrides,
})
const CAPABILITY = { mesh: TERRAIN_MESH_CAPABILITY, slope: TERRAIN_SLOPE_CAPABILITY, 'slope-clear': TERRAIN_SLOPE_CAPABILITY }
// An operation result; by default a new child of the head the request named.
function resultOf(operation, { created = true, head, replaced = 0, report, ...rest } = {}) {
  const body = {
    schema: 'leaf.solar-terrain-operation.v1', maturity: 'preview', operation, capability: CAPABILITY[operation],
    created, drawing_id: 'solar', project_id: 'p', frame: frameOf(),
    grid: operation === 'slope-clear' ? null : gridOf(),
    record: operation === 'mesh' ? meshRecord() : operation === 'slope' ? slopeRecord() : null,
    replaced, ...rest,
  }
  if (operation === 'slope') body.report = report ?? slopeReport()
  body.head = head ?? (created ? headOf(H2, 1, H) : headOf(H))
  return envelope(body)
}
const requestOf = (operation, extra = {}) => ({ ...SCOPE, operation, expectedHead: H, ...extra })
const edited = (body, edit) => {
  const copy = clone(body)
  edit(copy)
  return copy
}

describe('solar terrain model', () => {
  it('W20-06a CORRECTION 2 C1 a current mesh record may carry a boolean face count without displaying metrics', () => {
    const body = viewOf({
      grid: gridOf(), mesh_faces: 1,
      previews: previewsOf({ mesh: { state: 'current', record: meshRecord({ faces: true }) } }),
    })
    const value = validateTerrainView(body, SCOPE)
    expect(value).toEqual(normalize(body))
    const record = value.terrain.previews[TERRAIN_MESH_CAPABILITY].record
    expect(record.faces).toBe(true)
    expect(record).not.toBe(body.terrain.previews[TERRAIN_MESH_CAPABILITY].record)
    const summary = terrainPreviewSummary(value, TERRAIN_MESH_CAPABILITY)
    expect(summary.state).toBe('current')
    expect(summary.metrics).toBeNull()
    expect(summary.details).toEqual([])
    expect(summary.standing).toBe('Current, details unavailable')
  })

  it('W20-06a CORRECTION 2 C1 a boolean unit scale matches metres for standing but supplies no metrics', () => {
    const body = viewOf({
      grid: gridOf(), mesh_faces: 1,
      previews: previewsOf({ mesh: { state: 'current', record: meshRecord({ meters_per_unit: true }) } }),
    })
    const value = validateTerrainView(body, SCOPE)
    expect(value).toEqual(normalize(body))
    expect(value.terrain.previews[TERRAIN_MESH_CAPABILITY].record.meters_per_unit).toBe(true)
    const summary = terrainPreviewSummary(value, TERRAIN_MESH_CAPABILITY)
    expect(summary.state).toBe('current')
    expect(summary.metrics).toBeNull()
    expect(summary.details).toEqual([])
  })

  it('W20-06a CORRECTION 2 C1 a different digest still refuses a claimed current preview', () => {
    const body = viewOf({
      grid: gridOf(), mesh_faces: 1,
      previews: previewsOf({ mesh: { state: 'current', record: meshRecord({ grid_sha256: 'e'.repeat(64) }) } }),
    })
    expect(validateTerrainView(body, SCOPE)).toBeNull()
  })

  it('W20-06a CORRECTION 2 C2 a slope result with zero frames and all numeric report counts zero is refused', () => {
    const body = resultOf('slope')
    body.record.frames = 0
    for (const key of Object.keys(body.report)) {
      if (typeof body.report[key] === 'number') body.report[key] = 0
    }
    expect(validateTerrainOperation(body, requestOf('slope'))).toBeNull()
  })

  it('W20-06a CORRECTION 2 C2 trackers cannot outnumber the frames a slope preview read', () => {
    const body = resultOf('slope')
    body.record.frames = 0
    expect(body.report.tracker_count).toBe(2)
    expect(body.report.axial_rows_checked).toBeGreaterThan(0)
    expect(validateTerrainOperation(body, requestOf('slope'))).toBeNull()
  })

  it('W20-06a CORRECTION 2 C2 the unchanged synthetic slope result remains a valid success', () => {
    const body = resultOf('slope')
    expect(validateTerrainOperation(body, requestOf('slope'))).toEqual(normalize(body))
  })

  it('W20-06a CORRECTION 2 C3 a degraded empty view is refused', () => {
    const body = V0()
    body.degraded_mode = true
    expect(validateTerrainView(body, SCOPE)).toBeNull()
  })

  it('W20-06a CORRECTION 2 C3 a degraded slope result is refused', () => {
    const body = resultOf('slope')
    body.degraded_mode = true
    expect(validateTerrainOperation(body, requestOf('slope'))).toBeNull()
  })

  it('W20-06a M1 a view with no head, with a head and no grid, and with a grid each read back', () => {
    for (const body of [V0(), V1(), V2()]) {
      const value = validateTerrainView(body, SCOPE)
      expect(value).toEqual(normalize(body))
      expect(Object.keys(value)).toEqual(['schema', 'stored', 'head', 'terrain'])
      // No project named: the head's own project is accepted.
      expect(validateTerrainView(body, { drawingId: 'solar' })).toEqual(normalize(body))
      expect(validateTerrainView(normalize(body), SCOPE)).toEqual(normalize(body))
    }
    const body = V2()
    const value = validateTerrainView(body, SCOPE)
    expect(value.head).not.toBe(body.head)
    expect(value.head.state).not.toBe(body.head.state)
    expect(value.terrain.grid).not.toBe(body.terrain.grid)
    value.terrain.grid.rows = 99
    expect(body.terrain.grid.rows).toBe(2)
    const refused = [
      edited(V0(), (b) => { b.head = headOf() }),
      edited(V0(), (b) => { b.terrain = V1().terrain }),
      edited(V1(), (b) => { b.terrain = null }),
      edited(V1(), (b) => { b.head = null }),
      edited(V1(), (b) => { b.stored = 'yes' }),
      edited(V1(), (b) => { b.schema = 'leaf.solar-terrain-view-response.v2' }),
      edited(V1(), (b) => { b.ok = true }),
      edited(V1(), (b) => { b.error = { reason_code: 'X' } }),
      edited(V1(), (b) => { b.degraded_mode = 'no' }),
      edited(V1(), (b) => { b.terrain.maturity = 'production' }),
      edited(V1(), (b) => { b.terrain.drawing_id = 'other' }),
      edited(V1(), (b) => { b.terrain.project_id = 'q' }),
      edited(V1(), (b) => { b.terrain.mesh_faces = -1 }),
      edited(V1(), (b) => { b.terrain.slope_markers = '0' }),
      edited(V1(), (b) => { delete b.terrain.previews }),
      edited(V1(), (b) => { b.terrain.extra = 1 }),
    ]
    for (const bad of refused) expect(validateTerrainView(bad, SCOPE)).toBeNull()
    expect(validateTerrainView(V1(), { drawingId: 'other', projectId: 'p' })).toBeNull()
    expect(validateTerrainView(V1(), { drawingId: 'solar', projectId: 'q' })).toBeNull()
    expect(validateTerrainView(V1(), { drawingId: 'Bad!' })).toBeNull()
    for (const bad of [null, undefined, [], 'text', 7]) {
      expect(validateTerrainView(bad, SCOPE)).toBeNull()
      expect(validateTerrainView(V1(), bad)).toBeNull()
    }
    expect(terrainSummary(validateTerrainView(V0(), SCOPE))).toEqual([
      { key: 'terrain', label: 'Terrain', text: TERRAIN_EMPTY_SENTENCE },
    ])
    expect(terrainSummary(validateTerrainView(V1(), SCOPE)).map((line) => [line.key, line.text])).toEqual([
      ['head', H], ['change', '1 of this drawing'], ['units', 'Meters'], ['crs', 'None'], ['datum', 'Not recorded'],
      ['grid', TERRAIN_NO_GRID_SENTENCE], ['mesh', 'Not recorded'], ['slope', 'Not recorded'],
    ])
    expect(terrainSummary(validateTerrainView(V2(), SCOPE)).map((line) => [line.key, line.label, line.text])).toEqual([
      ['head', 'Terrain state', H], ['change', 'Terrain change', '1 of this drawing'],
      ['units', 'Drawing units', 'Meters'], ['crs', 'Coordinate system', 'None'],
      ['datum', 'Elevation datum', 'Not recorded'], ['grid', 'Terrain grid', '2 by 2 nodes'],
      ['cell', 'Grid cell', 'X 10 by Y 10 meters'], ['mesh', 'Mesh preview', 'Not recorded'],
      ['slope', 'Slope preview', 'Not recorded'],
    ])
    for (const bad of [null, {}, { stored: true }, { stored: false, head: headOf(), terrain: null }]) {
      expect(terrainSummary(bad)).toBeNull()
    }
  })

  it('W20-06a M2 a grid is bounded at 300 nodes a side, typed, and consistent with its own cells', () => {
    const largest = viewOf({ grid: gridOf({ rows: 300, cols: 300, x_max: 299, y_max: 299, lo: -5, hi: 5 }) })
    expect(validateTerrainView(largest, SCOPE)).toEqual(normalize(largest))
    // A grid past the LandXML import's 200 nodes is still a terrain grid.
    expect(validateTerrainView(viewOf({ grid: gridOf({ rows: 201, cols: 250, x_max: 249, y_max: 200 }) }), SCOPE))
      .not.toBeNull()
    // The positive control of the rounding case below: the same origin with cells that still advance.
    expect(validateTerrainView(viewOf({ grid: gridOf({ cols: 3, x_min: 1e14, x_max: 1e14 + 1 }) }), SCOPE)).not.toBeNull()
    const collapsed = gridOf({ cols: 3, x_min: 1e14, x_max: 100000000000000.02 })
    expect(collapsed.x_max).toBeGreaterThan(collapsed.x_min)
    expect(collapsed.x_min + collapsed.cell_x).toBe(collapsed.x_min)
    const refused = [
      gridOf({ rows: 301, y_max: 300 }),
      gridOf({ cols: 301, x_max: 300 }),
      { ...gridOf(), rows: true },
      { ...gridOf(), cols: '2' },
      { ...gridOf(), rows: 2.5 },
      { ...gridOf({ rows: 3 }), rows: 1 },
      gridOf({ x_max: 1e15 + 1 }),
      gridOf({ y_min: -(1e15 + 1) }),
      gridOf({ hi: 1e15 + 1 }),
      { ...gridOf(), cell_x: 9 },
      { ...gridOf(), cell_y_m: 3.048 },
      collapsed,
      gridOf({ x_min: 10, x_max: 10 }),
      gridOf({ y_min: 10, y_max: 0 }),
      gridOf({ lo: 1, hi: 0 }),
      { ...gridOf(), x_min: '0' },
      { ...gridOf(), elevation_min_m: null },
      { ...gridOf(), grid_sha256: 'C'.repeat(64) },
      { ...gridOf(), elevations: [] },
      (() => { const grid = gridOf(); delete grid.cell_x_m; return grid })(),
    ]
    for (const grid of refused) expect(validateTerrainView(viewOf({ grid }), SCOPE)).toBeNull()
    // A value JSON cannot carry never validates either.
    expect(validateTerrainView({ ...V2(), terrain: { ...V2().terrain, grid: { ...gridOf(), x_max: Infinity } } }, SCOPE))
      .toBeNull()
    expect(validateTerrainView({ ...V2(), terrain: { ...V2().terrain, grid: { ...gridOf(), y_max: NaN } } }, SCOPE))
      .toBeNull()
  })

  it('W20-06a M3 units, references and scope are checked, and feet keep their metre cells', () => {
    const feet = viewOf({ frame: frameOf('ft'), grid: gridOf({ mpu: 0.3048 }) })
    const value = validateTerrainView(feet, SCOPE)
    expect(value).toEqual(normalize(feet))
    expect(value.terrain.grid.cell_x).toBe(10)
    expect(value.terrain.grid.cell_x_m).toBeCloseTo(3.048, 12)
    expect(terrainSummary(value).map((line) => [line.key, line.text]).slice(2, 7)).toEqual([
      ['units', 'Feet'], ['crs', 'None'], ['datum', 'Not recorded'], ['grid', '2 by 2 nodes'],
      ['cell', 'X 10 by Y 10 feet'],
    ])
    const named = viewOf({ frame: { ...frameOf(), crs: 'EPSG:2229', elevation_datum: 'EPSG:5703' }, grid: gridOf() })
    expect(validateTerrainView(named, SCOPE)).toEqual(normalize(named))
    expect(terrainSummary(validateTerrainView(named, SCOPE)).map((line) => line.text).slice(3, 5))
      .toEqual(['EPSG:2229', 'EPSG:5703'])
    const later = viewOf({ head: headOf(H2, 4095, H) })
    expect(validateTerrainView(later, SCOPE)).toEqual(normalize(later))
    expect(terrainSummary(validateTerrainView(later, SCOPE))[1].text).toBe('4096 of this drawing')
    const upper = 'A'.repeat(64)
    const refused = [
      // Feet with a metre scale: the grid is consistent with the frame, so only the units disagree.
      viewOf({ frame: { ...frameOf('ft'), meters_per_unit: 1 }, grid: gridOf() }),
      viewOf({ frame: { ...frameOf(), drawing_units: 'in' } }),
      viewOf({ frame: { ...frameOf(), crs: 'EPSG:0' } }),
      viewOf({ frame: { ...frameOf(), crs: 'epsg:4326' } }),
      viewOf({ frame: { ...frameOf(), crs: 'EPSG:1234567' } }),
      viewOf({ frame: { ...frameOf(), elevation_datum: 'NAVD88' } }),
      viewOf({ frame: { ...frameOf(), transform: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1] } }),
      viewOf({ frame: { ...frameOf(), coordinate_system: 'local' } }),
      viewOf({ frame: { ...frameOf(), horizontal: 'metres' } }),
      viewOf({ frame: { ...frameOf(), elevation: 'feet' } }),
      viewOf({ frame: { ...frameOf(), extra: 1 } }),
      viewOf({ head: { ...headOf(H2, 4096, H) } }),
      viewOf({ head: { ...headOf(), index: -1 } }),
      viewOf({ head: headOf(H, 0, H2) }),
      viewOf({ head: headOf(H, 1, null) }),
      viewOf({ head: headOf(H, 1, upper) }),
      viewOf({ head: headOf(upper) }),
      viewOf({ head: edited(headOf(), (h) => { h.state.download = `/api/drawings/other/artifacts/${H}` }) }),
      viewOf({ head: edited(headOf(), (h) => { h.state.download = `/api/drawings/solar/artifacts/${H2}` }) }),
      viewOf({ head: edited(headOf(), (h) => { h.state.content_sha256 = upper }) }),
      viewOf({ head: edited(headOf(), (h) => { h.state.media_type = 'application/xml' }) }),
      viewOf({ head: edited(headOf(), (h) => { h.state.filename = 'landxml-source.xml' }) }),
      viewOf({ head: edited(headOf(), (h) => { h.state.byte_length = 0 }) }),
      viewOf({ head: edited(headOf(), (h) => { h.state.byte_length = 16_777_217 }) }),
      viewOf({ head: edited(headOf(), (h) => { h.state.source_version = 0 }) }),
      viewOf({ head: edited(headOf(), (h) => { h.state.source_version = 2 ** 53 }) }),
      viewOf({ head: edited(headOf(), (h) => { h.state.schema = 'leaf.solar-artifact-ref.v2' }) }),
      viewOf({ head: edited(headOf(), (h) => { h.state.extra = true }) }),
      viewOf({ head: edited(headOf(), (h) => { h.schema = 'leaf.solar-physical-head.v2' }) }),
      viewOf({ head: edited(headOf(), (h) => { h.drawing_id = 'other' }) }),
      viewOf({ head: edited(headOf(), (h) => { h.project_id = 'q' }) }),
      viewOf({ head: edited(headOf(), (h) => { h.project_id = 'p'.repeat(101) }) }),
    ]
    for (const bad of refused) expect(validateTerrainView(bad, SCOPE)).toBeNull()
    // A revision above the LandXML client's 2,147,483,647 is not a terrain refusal.
    const revised = viewOf({ head: edited(headOf(), (h) => { h.state.source_version = 2_147_483_648 }) })
    expect(validateTerrainView(revised, SCOPE)).toEqual(normalize(revised))
  })

  it('W20-06a M4 slope limits resolve to the seven defaults plus overrides and are never coerced', () => {
    expect(TERRAIN_LIMIT_DEFAULTS).toEqual(DEFAULTS)
    expect(Object.isFrozen(TERRAIN_LIMIT_DEFAULTS)).toBe(true)
    expect(TERRAIN_LIMIT_KEYS).toEqual(Object.keys(DEFAULTS))
    expect(TERRAIN_LIMIT_FIELDS.map((field) => field.key)).toEqual(TERRAIN_LIMIT_KEYS)
    for (const none of [null, undefined, {}]) {
      const resolved = resolveTerrainLimits(none)
      expect(resolved).toEqual(DEFAULTS)
      expect(Object.keys(resolved)).toEqual(TERRAIN_LIMIT_KEYS)
      expect(resolved).not.toBe(TERRAIN_LIMIT_DEFAULTS)
    }
    expect(resolveTerrainLimits({ MaxAxialSlopePct: 1000, MaxRowToRowSlopeDeg: 90, Columns: 10000 }))
      .toEqual({ ...DEFAULTS, MaxAxialSlopePct: 1000, MaxRowToRowSlopeDeg: 90, Columns: 10000 })
    expect(resolveTerrainLimits({ MaxSlopePercent: 12.25 })).toEqual({ ...DEFAULTS, MaxSlopePercent: 12.25 })
    const zeros = resolveTerrainLimits({ MaxNsSlopePct: 0, MaxAxialSlopePct: -0, MaxRowToRowSlopeDeg: -0, Columns: -0 })
    for (const key of ['MaxNsSlopePct', 'MaxAxialSlopePct', 'MaxRowToRowSlopeDeg', 'Columns']) {
      expect(Object.is(zeros[key], 0)).toBe(true)
    }
    expect(JSON.stringify(zeros)).toBe(JSON.stringify({ ...DEFAULTS, MaxNsSlopePct: 0, MaxAxialSlopePct: 0, MaxRowToRowSlopeDeg: 0 }))
    for (const key of TERRAIN_LIMIT_KEYS) {
      const top = key === 'Columns' ? 10000 : key === 'MaxRowToRowSlopeDeg' ? 90 : 1000
      expect(resolveTerrainLimits({ [key]: top })[key]).toBe(top)
      for (const bad of [true, false, '8.5', '', ' ', null, undefined, NaN, Infinity, -Infinity, -0.001, -1, top + 1, [1], {}]) {
        expect(resolveTerrainLimits({ [key]: bad })).toBeNull()
      }
    }
    expect(resolveTerrainLimits({ MaxSlopePercent: 1000.0001 })).toBeNull()
    expect(resolveTerrainLimits({ MaxRowToRowSlopeDeg: 90.0001 })).toBeNull()
    expect(resolveTerrainLimits({ Columns: 1.5 })).toBeNull()
    expect(resolveTerrainLimits({ Columns: 1.0 })).toEqual({ ...DEFAULTS, Columns: 1 })
    for (const bad of [{ Rows: 1 }, { ...DEFAULTS, columns: 0 }, [], 'text', 7, true, new Map()]) {
      expect(resolveTerrainLimits(bad)).toBeNull()
    }
    // Drafts are text and are parsed apart from the numeric check: blank means the default.
    expect(parseTerrainLimitDrafts({})).toEqual({ ok: true, limits: DEFAULTS })
    expect(parseTerrainLimitDrafts(undefined)).toEqual({ ok: true, limits: DEFAULTS })
    expect(parseTerrainLimitDrafts({ MaxNsSlopePct: '', MaxAxialSlopePct: '   ' })).toEqual({ ok: true, limits: DEFAULTS })
    expect(parseTerrainLimitDrafts({ MaxAxialSlopePct: '12.5', MaxRowToRowSlopeDeg: ' 90 ', Columns: '10000', MaxSlopePercent: '1000' }))
      .toEqual({ ok: true, limits: { ...DEFAULTS, MaxAxialSlopePct: 12.5, MaxRowToRowSlopeDeg: 90, Columns: 10000, MaxSlopePercent: 1000 } })
    expect(parseTerrainLimitDrafts({ MaxNsSlopePct: '0', Columns: '0' }).limits).toEqual({ ...DEFAULTS, MaxNsSlopePct: 0 })
    const percent = 'Enter a decimal number from 0 to 1000'
    const degrees = 'Enter a decimal number from 0 to 90'
    const columns = 'Enter a whole number from 0 to 10000'
    const drafts = [
      ['MaxNsSlopePct', '1000.1', percent], ['MaxRowToRowSlopeDeg', '90.1', degrees], ['Columns', '1.5', columns],
      ['MaxAxialSlopePct', '-1', percent], ['MaxAxialSlopePct', '+1', percent], ['MaxAxialSlopePct', '1e2', percent],
      ['MaxAxialSlopePct', '0x10', percent], ['MaxAxialSlopePct', '1,5', percent], ['MaxAxialSlopePct', '.5', percent],
      ['MaxAxialSlopePct', '5.', percent], ['MaxAxialSlopePct', 'eight', percent], ['MaxAxialSlopePct', 8.5, percent],
      ['MaxAxialSlopePct', null, percent], ['MaxAxialSlopePct', '1'.repeat(TERRAIN_MAX_DRAFT_CHARS + 1), percent],
      ['MaxAxialSlopePct', ' '.repeat(TERRAIN_MAX_DRAFT_CHARS + 1), percent], ['Columns', '10001', columns],
      ['Columns', '-0', columns], ['Columns', '1.0', columns],
    ]
    for (const [field, draft, reason] of drafts) {
      expect(parseTerrainLimitDrafts({ [field]: draft })).toEqual({ ok: false, field, reason, invalid: { [field]: reason } })
    }
    expect(TERRAIN_MAX_DRAFT_CHARS).toBe(64)
    expect(parseTerrainLimitDrafts({ MaxAxialSlopePct: `${'0'.repeat(63)}1` }).limits.MaxAxialSlopePct).toBe(1)
    // Every unreadable field is named, the first in the form's order first.
    expect(parseTerrainLimitDrafts({ Columns: '1.5', MaxRowToRowSlopeDeg: '91' })).toEqual({
      ok: false, field: 'MaxRowToRowSlopeDeg', reason: degrees, invalid: { MaxRowToRowSlopeDeg: degrees, Columns: columns },
    })
    for (const bad of [{ Rows: '1' }, [], 'text', 7]) {
      const refusal = parseTerrainLimitDrafts(bad)
      expect(refusal.ok).toBe(false)
      expect(refusal.field).toBeNull()
      expect(refusal.reason.length).toBeGreaterThanOrEqual(12)
    }
  })

  it('W20-06a M5 a preview keeps the standing the server gave it, and stale never reads as current', () => {
    const current = viewOf({ grid: gridOf(), mesh_faces: 1, previews: previewsOf({ mesh: { state: 'current', record: meshRecord() } }) })
    const value = validateTerrainView(current, SCOPE)
    expect(value).toEqual(normalize(current))
    expect(value.terrain.previews[TERRAIN_MESH_CAPABILITY].record).not.toBe(current.terrain.previews[TERRAIN_MESH_CAPABILITY].record)
    expect(terrainPreviewSummary(value, TERRAIN_MESH_CAPABILITY)).toEqual({
      capability: TERRAIN_MESH_CAPABILITY, state: 'current', label: 'Mesh preview', text: 'Mesh preview: current',
      standing: 'Current: 1 face, 1 green, 0 yellow, 0 red, steepest 0 percent',
      details: ['1 face', '1 green', '0 yellow', '0 red', 'steepest 0 percent'],
      metrics: { faces: 1, green: 1, yellow: 0, red: 0, maxSlopePercent: 0 },
    })
    expect(terrainPreviewSummary(value, TERRAIN_SLOPE_CAPABILITY)).toEqual({
      capability: TERRAIN_SLOPE_CAPABILITY, state: 'absent', label: 'Slope preview',
      text: 'No slope preview has been recorded', standing: 'Not recorded', details: [], metrics: null,
    })
    expect(terrainSummary(value).filter((line) => line.state !== undefined)).toEqual([
      { key: 'mesh', label: 'Mesh preview', text: 'Current: 1 face, 1 green, 0 yellow, 0 red, steepest 0 percent', state: 'current' },
      { key: 'slope', label: 'Slope preview', text: 'Not recorded', state: 'absent' },
    ])
    // The server says stale: the same record is accepted, and it is shown as stale, never promoted.
    const stale = edited(current, (b) => { b.terrain.previews[TERRAIN_MESH_CAPABILITY].state = 'stale' })
    const staleValue = validateTerrainView(stale, SCOPE)
    expect(staleValue).toEqual(normalize(stale))
    const staleSummary = terrainPreviewSummary(staleValue, TERRAIN_MESH_CAPABILITY)
    expect(staleSummary.state).toBe('stale')
    expect(staleSummary.text).toBe('Mesh preview: stale')
    expect(staleSummary.standing).toBe('Stale, earlier results: 1 face, 1 green, 0 yellow, 0 red, steepest 0 percent')
    // A view that claims current against the adapter's own three comparisons is not a view.
    const inconsistent = [
      edited(current, (b) => { b.terrain.previews[TERRAIN_MESH_CAPABILITY].record.grid_sha256 = 'e'.repeat(64) }),
      edited(current, (b) => { b.terrain.previews[TERRAIN_MESH_CAPABILITY].record.meters_per_unit = 0.3048 }),
      edited(current, (b) => { b.terrain.mesh_faces = 2 }),
      edited(current, (b) => { b.terrain.previews[TERRAIN_MESH_CAPABILITY].record.faces = 2 }),
      edited(current, (b) => { b.terrain.grid = null }),
      edited(current, (b) => { b.terrain.previews[TERRAIN_MESH_CAPABILITY].record = 'old' }),
      edited(current, (b) => { b.terrain.previews[TERRAIN_MESH_CAPABILITY].record = null }),
      edited(current, (b) => { b.terrain.previews[TERRAIN_SLOPE_CAPABILITY].record = slopeRecord() }),
      edited(current, (b) => { b.terrain.previews[TERRAIN_MESH_CAPABILITY].state = 'fresh' }),
      edited(current, (b) => { b.terrain.previews[TERRAIN_MESH_CAPABILITY].extra = 1 }),
      edited(current, (b) => { delete b.terrain.previews[TERRAIN_SLOPE_CAPABILITY] }),
      edited(current, (b) => { b.terrain.previews.other = { state: 'absent', record: null } }),
    ]
    for (const bad of inconsistent) expect(validateTerrainView(bad, SCOPE)).toBeNull()
    // Each of those three, labelled stale by the server, stays visible as stale.
    for (const bad of inconsistent.slice(0, 4)) {
      const relabelled = edited(bad, (b) => { b.terrain.previews[TERRAIN_MESH_CAPABILITY].state = 'stale' })
      expect(terrainPreviewSummary(validateTerrainView(relabelled, SCOPE), TERRAIN_MESH_CAPABILITY).state).toBe('stale')
    }
    // An old malformed record keeps the head on screen: stale, with no numbers.
    for (const record of ['old', 7, [1, 2], { faces: 'many' }, { ...meshRecord(), buckets: { Green: 1, Yellow: 1, Red: 0 } }]) {
      const malformed = viewOf({ grid: gridOf(), mesh_faces: 1, previews: previewsOf({ mesh: { state: 'stale', record } }) })
      const kept = validateTerrainView(malformed, SCOPE)
      expect(kept).toEqual(normalize(malformed))
      expect(kept.head.state.artifact_id).toBe(H)
      const summary = terrainPreviewSummary(kept, TERRAIN_MESH_CAPABILITY)
      expect([summary.state, summary.text, summary.standing, summary.details, summary.metrics])
        .toEqual(['stale', 'Mesh preview: stale', 'Stale, details unavailable', [], null])
      expect(terrainSummary(kept).find((line) => line.key === 'mesh').text).toBe('Stale, details unavailable')
    }
    // A record the three comparisons pass but that is not a mesh record shows no numbers either.
    const thin = viewOf({ grid: gridOf(), mesh_faces: 1, previews: previewsOf({ mesh: { state: 'current', record: { grid_sha256: D, meters_per_unit: 1, faces: 1 } } }) })
    const thinSummary = terrainPreviewSummary(validateTerrainView(thin, SCOPE), TERRAIN_MESH_CAPABILITY)
    expect([thinSummary.state, thinSummary.standing, thinSummary.metrics]).toEqual(['current', 'Current, details unavailable', null])
    // The slope record: its numbers are shown and the kernel's status wording is not.
    const slope = viewOf({ grid: gridOf(), slope_markers: 1, previews: previewsOf({ slope: { state: 'current', record: slopeRecord({ markers: 1 }) } }) })
    const slopeValue = validateTerrainView(slope, SCOPE)
    expect(slopeValue).toEqual(normalize(slope))
    const slopeSummary = terrainPreviewSummary(slopeValue, TERRAIN_SLOPE_CAPABILITY)
    expect(slopeSummary.standing).toBe('Current: 2 tracker frames read, 1 row over the limits of this preview')
    expect(slopeSummary.metrics).toEqual({ frames: 2, markers: 1 })
    for (const shown of [JSON.stringify(slopeSummary), JSON.stringify(terrainSummary(slopeValue))]) {
      expect(shown).not.toContain('ASCE')
      expect(shown).not.toContain('budget')
      expect(shown).not.toContain('Tracker slope:')
    }
    expect(TERRAIN_CAPABILITIES).toEqual([TERRAIN_MESH_CAPABILITY, TERRAIN_SLOPE_CAPABILITY])
    expect(terrainPreviewSummary(value, 'piles')).toBeNull()
    expect(terrainPreviewSummary(validateTerrainView(V0(), SCOPE), TERRAIN_MESH_CAPABILITY)).toBeNull()
    expect(terrainPreviewSummary(null, TERRAIN_MESH_CAPABILITY)).toBeNull()
  })

  it('W20-06a M6 an operation result answers its own request, a clear needs no grid, and a no-op is a success', () => {
    expect(TERRAIN_OPERATIONS).toEqual(['mesh', 'slope', 'slope-clear'])
    const mesh = resultOf('mesh', { replaced: 4 })
    const slope = resultOf('slope', { replaced: 3 })
    const clear = resultOf('slope-clear', { replaced: 2 })
    expect(clear.grid).toBeNull()
    expect(clear.record).toBeNull()
    const cases = [['mesh', mesh, {}], ['slope', slope, { limits: DEFAULTS }], ['slope', slope, {}], ['slope-clear', clear, {}]]
    for (const [operation, body, extra] of cases) {
      const value = validateTerrainOperation(body, requestOf(operation, extra))
      expect(value).toEqual(normalize(body))
      expect(Object.keys(value).at(-1)).toBe('head')
      expect('report' in value).toBe(operation === 'slope')
      // The request may leave the project out; the result still names one.
      expect(validateTerrainOperation(body, { drawingId: 'solar', operation, expectedHead: H, ...extra })).toEqual(normalize(body))
      // Nothing published by this call: the head the request named, or the identical child another writer published.
      const same = resultOf(operation, { created: false })
      expect(same.head.state.artifact_id).toBe(H)
      expect(validateTerrainOperation(same, requestOf(operation, extra))).toEqual(normalize(same))
      const raced = resultOf(operation, { created: false, head: headOf(H2, 1, H) })
      expect(validateTerrainOperation(raced, requestOf(operation, extra))).toEqual(normalize(raced))
      // A result for another request is not this request's result.
      expect(validateTerrainOperation(body, requestOf(operation, { ...extra, expectedHead: H2 }))).toBeNull()
      expect(validateTerrainOperation(body, requestOf(operation, { ...extra, drawingId: 'other' }))).toBeNull()
      expect(validateTerrainOperation(body, requestOf(operation, { ...extra, projectId: 'q' }))).toBeNull()
      expect(validateTerrainOperation(edited(body, (b) => { b.created = 'yes' }), requestOf(operation, extra))).toBeNull()
      expect(validateTerrainOperation(edited(body, (b) => { b.maturity = 'production' }), requestOf(operation, extra))).toBeNull()
      expect(validateTerrainOperation(edited(body, (b) => { b.capability = 'pile-layout' }), requestOf(operation, extra))).toBeNull()
      expect(validateTerrainOperation(edited(body, (b) => { b.ok = true }), requestOf(operation, extra))).toBeNull()
      expect(validateTerrainOperation(edited(body, (b) => { b.replaced = -1 }), requestOf(operation, extra))).toBeNull()
      // A new child that is not a child of the head the request named.
      expect(validateTerrainOperation(resultOf(operation, { head: headOf(H2, 1, 'e'.repeat(64)) }), requestOf(operation, extra))).toBeNull()
      expect(validateTerrainOperation(resultOf(operation, { created: false, head: headOf(H2) }), requestOf(operation, extra))).toBeNull()
    }
    // The wrong returned operation, in every pairing.
    expect(validateTerrainOperation(mesh, requestOf('slope'))).toBeNull()
    expect(validateTerrainOperation(slope, requestOf('mesh'))).toBeNull()
    expect(validateTerrainOperation(clear, requestOf('slope'))).toBeNull()
    expect(validateTerrainOperation(slope, requestOf('slope-clear'))).toBeNull()
    expect(validateTerrainOperation(edited(slope, (b) => { b.operation = 'mesh' }), requestOf('slope'))).toBeNull()
    const refusedSlope = [
      (b) => { b.report.axial_violation_rows = 3 },
      (b) => { b.report.cross_axis_violation_pairs = 2 },
      (b) => { b.report.row_to_row_angle_violation_pairs = 2 },
      (b) => { b.report.axial_rows_checked = 3 },
      (b) => { b.report.cross_axis_pairs_checked = 3 },
      (b) => { b.report.trackers_needing_terrain_following = 3 },
      (b) => { b.report.has_violations = true },
      (b) => { b.report.axial_violation_rows = 1 },
      (b) => { b.report.tracker_count = 2.5 },
      (b) => { b.report.status = 7 },
      (b) => { b.report.extra = 1 },
      (b) => { delete b.report },
      (b) => { b.record.markers = 3 },
      (b) => { b.record.frames = 20_001 },
      (b) => { b.record.status = null },
      (b) => { delete b.record.limits.Columns },
      (b) => { b.record.limits.Columns = 1.5 },
      (b) => { b.record.limits.MaxAxialSlopePct = '8.5' },
      (b) => { b.record.grid_sha256 = 'e'.repeat(64) },
      (b) => { b.record.meters_per_unit = 0.3048 },
      (b) => { b.record.capability = TERRAIN_MESH_CAPABILITY },
      (b) => { b.record = null },
      (b) => { b.grid = null },
    ]
    for (const edit of refusedSlope) expect(validateTerrainOperation(edited(slope, edit), requestOf('slope'))).toBeNull()
    // A real violation is consistent, and the record must carry the limits the request sent.
    const violating = resultOf('slope', {
      record: slopeRecord({ markers: 1, limits: { ...DEFAULTS, MaxAxialSlopePct: 2 } }),
      report: slopeReport({ axial_violation_rows: 1, trackers_needing_terrain_following: 1, has_violations: true }),
    })
    const sent = { MaxAxialSlopePct: 2 }
    expect(validateTerrainOperation(violating, requestOf('slope', { limits: sent }))).toEqual(normalize(violating))
    expect(validateTerrainOperation(violating, requestOf('slope', { limits: DEFAULTS }))).toBeNull()
    expect(validateTerrainOperation(slope, requestOf('slope', { limits: sent }))).toBeNull()
    expect(validateTerrainOperation(slope, requestOf('slope', { limits: { Columns: true } }))).toBeNull()
    const refusedMesh = [
      (b) => { b.record.faces = 2 },
      (b) => { b.record.buckets.Green = 0 },
      (b) => { b.record.buckets.Blue = 0 },
      (b) => { b.record.max_slope_percent = -1 },
      (b) => { b.record.mesh_sha256 = 'short' },
      (b) => { b.record.grid_sha256 = 'e'.repeat(64) },
      (b) => { b.record.maturity = 'production' },
      (b) => { b.report = slopeReport() },
      (b) => { b.grid = null },
      (b) => { b.grid.rows = 3 },
      (b) => { b.replaced = 1_000_000_001 },
      (b) => { b.frame.transform = 'affine' },
    ]
    for (const edit of refusedMesh) expect(validateTerrainOperation(edited(mesh, edit), requestOf('mesh'))).toBeNull()
    expect(validateTerrainOperation(mesh, requestOf('mesh', { limits: DEFAULTS }))).toBeNull()
    const refusedClear = [
      (b) => { b.grid = gridOf() },
      (b) => { b.record = slopeRecord() },
      (b) => { b.report = slopeReport() },
      (b) => { b.replaced = 1_000_001 },
    ]
    for (const edit of refusedClear) expect(validateTerrainOperation(edited(clear, edit), requestOf('slope-clear'))).toBeNull()
    for (const bad of [null, {}, { ...requestOf('mesh'), operation: 'piles' }, { ...requestOf('mesh'), expectedHead: 'A'.repeat(64) }]) {
      expect(validateTerrainOperation(mesh, bad)).toBeNull()
    }
    // What the panel sends for a displayed view: the head on screen, limits for slope only.
    const gridless = validateTerrainView(V1(), SCOPE)
    const gridded = validateTerrainView(V2(), SCOPE)
    expect(buildTerrainOperation({ view: gridless, operation: 'slope-clear' })).toEqual({ ok: true, request: { operation: 'slope-clear', expectedHead: H } })
    expect(buildTerrainOperation({ view: gridless, operation: 'mesh' })).toEqual({ ok: false, reason: 'TERRAIN_GRID_MISSING' })
    expect(buildTerrainOperation({ view: gridless, operation: 'slope' })).toEqual({ ok: false, reason: 'TERRAIN_GRID_MISSING' })
    expect(buildTerrainOperation({ view: gridded, operation: 'mesh' })).toEqual({ ok: true, request: { operation: 'mesh', expectedHead: H } })
    expect(buildTerrainOperation({ view: gridded, operation: 'slope' })).toEqual({ ok: true, request: { operation: 'slope', expectedHead: H, limits: DEFAULTS } })
    expect(buildTerrainOperation({ view: gridded, operation: 'slope', limits: { Columns: 3 } }).request.limits).toEqual({ ...DEFAULTS, Columns: 3 })
    const moved = validateTerrainView(viewOf({ head: headOf(H2, 1, H), grid: gridOf() }), SCOPE)
    expect(buildTerrainOperation({ view: moved, operation: 'mesh' }).request.expectedHead).toBe(H2)
    const refusedBuilds = [
      [{ view: validateTerrainView(V0(), SCOPE), operation: 'mesh' }, 'TERRAIN_STATE_NOT_FOUND'],
      [{ view: validateTerrainView(V0(), SCOPE), operation: 'slope-clear' }, 'TERRAIN_STATE_NOT_FOUND'],
      [{ view: gridded, operation: 'piles' }, 'TERRAIN_OPERATION_INVALID'],
      [{ view: gridded, operation: 'mesh', limits: DEFAULTS }, 'TERRAIN_BODY_INVALID'],
      [{ view: gridded, operation: 'slope-clear', limits: null }, 'TERRAIN_BODY_INVALID'],
      [{ view: gridded, operation: 'slope', limits: { Columns: true } }, 'TERRAIN_LIMITS_INVALID'],
      [{ view: null, operation: 'mesh' }, 'TERRAIN_CLIENT_REQUEST_INVALID'],
      [null, 'TERRAIN_CLIENT_REQUEST_INVALID'],
    ]
    for (const [input, reason] of refusedBuilds) expect(buildTerrainOperation(input)).toEqual({ ok: false, reason })
  })
})
