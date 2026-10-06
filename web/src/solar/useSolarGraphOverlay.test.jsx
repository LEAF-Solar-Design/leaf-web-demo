// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, renderHook } from '@testing-library/react'
import { solarGraphOverlay } from './solarGraphOverlay.js'
import { decideSolarGraphOverlay, SOLAR_OVERLAY_REASONS, useSolarGraphOverlay } from './useSolarGraphOverlay.js'

vi.mock('./solarGraphOverlay.js', async importOriginal => {
  const actual = await importOriginal()
  return { ...actual, solarGraphOverlay: vi.fn(actual.solarGraphOverlay) }
})
const actual = await vi.importActual('./solarGraphOverlay.js')
beforeEach(() => {
  solarGraphOverlay.mockReset()
  solarGraphOverlay.mockImplementation(actual.solarGraphOverlay)
})
afterEach(() => {
  cleanup()
  solarGraphOverlay.mockImplementation(actual.solarGraphOverlay)
})

const U = (k, n = 1) => `leaf:${k}:00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
const E = (k, n = 1) => ({
  id: U(k, n), kind: k, rev: 0,
  provenance: { created_by: 'test', created_at: '2026-01-01T00:00:00Z', last_writer: 'test', source_rev: 0 },
  extra: {}, validity: { state: 'valid', reasons: [] },
})
const units = m => ({
  drawing_units: m === .0254 ? 'in' : m === .3048 ? 'ft' : 'm',
  meters_per_unit: m, source: 'explicit', compute_units: 'm',
  wcs_to_ucs: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
  elevation_datum: 'local', crs: null, drawing_unit_is_feet: m === .3048, warnings: [],
})
const G = (patch = {}) => ({
  graph_schema_version: 1, rev: 0, parent_rev: null, source_hash: 'a'.repeat(64), catalog_versions: {},
  project: { ...E('project'), name: '', zip_code: '', latitude: null, longitude: null,
    installation_design: 'Roof', units: units(1), graph_schema_version: 1, site_revision: 'test' },
  settings: { ...E('settings'), panel_layer_contains: 'Panel', panel_group_layer: 'Group',
    string_layer: 'String', home_run_layer: 'Homerun', panels_in_sequence: 0,
    num_mppt: 0, strings_per_mppt: 0, optimizer_ratio: 1, use_l2_collectors: false,
    panel_group_number: 1, string_number: 1, inverter_number: 1, mppt_letter: 'A',
    global_string_sizing_confirmed: false,
    voc_cold: { passes: null, override_accepted: false, suggested_string_length: null,
      per_module: null, string_voltage: null, max_dc_voltage: null } },
  electrical_zones: [], frames: [], panels: [], strings: [], inverters: [], routes: [],
  schedules: [], opaque_stores: {}, orphaned_xdata: [], extra: {}, ...patch,
})
const P = (n, centre) => ({ ...E('panel', n), centre, frame_ref: null, matrix_cell: null,
  angle: 0, assignment: { string_ref: null, seq: null } })
const S = (n, refs, route = []) => ({ ...E('string', n), circuit_tag: 'S' + n,
  circuit_kind: 'String', ordered_panel_refs: refs.map(n => U('panel', n)), module_count: refs.length,
  from_ref: null, to_ref: null, tag_text_ref: null, wire_gauge: '', length_ft: 0, route, inverter_ref: null })
const I = (n, position, equipment_type = 'string_inverter') => ({ ...E('inverter', n),
  number: n, type_key: 'INV', is_l2: false, position, model: '', mppt_count: 1,
  total_dc_inputs: 1, max_dc_voltage: 1500, max_ac_power_kw: 1, is_solaredge: false,
  input_assignments: [], equipment_type, l2_ref: null })
const R = (n, route_kind, points) => ({ ...E('route', n), route_kind, points,
  point_units: 'm', length_units: 'ft', from_ref: U('inverter', 1), to_ref: U('inverter', 2),
  wire_gauge: '', length_ft: 0,
  ...(route_kind === 'trench' ? { trench: { depth_m: 1, width_m: 1, voltage_class: 'DC-PV' } } : {}) })
const roof = () => G({
  panels: [P(1, [0, 0]), P(2, [1, 0]), P(3, [2, 0]), P(4, [0, 1]), P(5, [1, 1]), P(6, [2, 1])],
  strings: [S(1, [1, 2, 3]), S(2, [6, 5, 4])], inverters: [I(1, [3, 2])],
  routes: [R(1, 'start homerun', [[2, 0], [3, 0], [3, 2]])],
})
const roofAt = m => { const g = roof(); g.project.units = units(m); return g }
const block = () => ({
  codec: 'leaf.solar-ground-slots.v1', count: 2,
  panel_ids: 'AAAAAAAAQACAAAAAAAAAAQAAAAAAAEAAgAAAAAAAAAI=',
  centres: 'AAAAAAAA8D8AAAAAAADwvwAAAAAAAABAAAAAAAAAAMA=', angle: 0,
  panel: { rev: 0, provenance: E('panel').provenance, validity: E('panel').validity, extra: {} },
})
const ground = () => {
  const g = G({ frames: [{ ...E('frame'), name: 'F', insertion_point: [100, 200],
    installation_design: 'Ground', panel_refs: [], module_rows: 1, module_columns: 2,
    module_slots: 2, module_power_watts: 400, module_width_along_row: 1, module_height_across_row: 1,
    electrical_zone_ref: null, matrix: [], sequences: [], panel_assignments: [],
    tracker: { tracker_model: 'single_axis_tracker', axis_start: [0, 0], axis_end: [1, 0],
      axis_units: 'drawing', module_slots: 2, row_index: 0, length_m: 2, rail_overhang_m: 0,
      cross_axis_width_m: null, max_tilt_deg: null,
      source: { entity_kind: 'polyline', handle: null, layer: null, block_name: null, source_command: null } },
    ground_slots: block() }], strings: [S(1, [2, 1])] })
  g.project.installation_design = 'Ground'; g.project.units = units(2)
  return g
}
const lines = n => G({ routes: Array.from({ length: n }, (_, i) => R(i + 1, 'feeder', [[0, 0], [1, 1]])) })

const roofPolylines = [
  { color: '#22c55e', pts: [[0, 0], [1, 0], [2, 0]] },
  { color: '#22c55e', pts: [[2, 1], [1, 1], [0, 1]] },
  { color: '#f59e0b', pts: [[2.75, 1.75], [3.25, 1.75], [3.25, 2.25], [2.75, 2.25], [2.75, 1.75]] },
  { color: '#38bdf8', pts: [[2, 0], [3, 0], [3, 2]] },
]
const groundPolylines = [{ color: '#22c55e', pts: [[1, -1], [.5, -.5]] }]
const freeze = value => {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze)
    Object.freeze(value)
  }
  return value
}
const missingScale = () => {
  const graph = roof()
  delete graph.project.units.meters_per_unit
  return graph
}
const refusals = () => [
  ['invalid_graph', G({ graph_schema_version: 2 })],
  ['invalid_scale', missingScale()],
  ['overlay_limit', lines(4097)],
]

const N = reason => ({ solarOverlay: null, reason })
const O = (polylines, intake, drawingKey) => ({
  solarOverlay: { polylines, intake, drawingKey }, reason: null,
})
const inputFor = (graph = roof()) => ({
  enabled: true, shown: { solar_design_graph: graph }, drawingKey: 'console:D',
  sceneCurrent: true, refreshPending: false, activeIntake: null,
  engineDocument: null, engineDirty: false, activeVersion: 7, requestedDrawingId: 'D',
})
const engineFor = (input = inputFor(), documentId = 'D-v7.dxf') => ({
  ...input, activeIntake: { source: 'engine', documentId },
  engineDocument: { documentId, documentOrigin: 'head', committedVersion: 7, entityCount: 1 },
})
const mount = input => renderHook(props => useSolarGraphOverlay(props), {
  initialProps: input, reactStrictMode: false,
})
const calls = count => expect(solarGraphOverlay).toHaveBeenCalledTimes(count)
const projectionAt = (index = 0) => solarGraphOverlay.mock.results[index].value
const bound = (result, polylines, intake, key) => {
  expect(result).toEqual(O(polylines, intake, key))
  expect(result.solarOverlay.intake).toBe(intake)
  expect(result.solarOverlay.polylines).toBe(polylines)
}
const ordinary = (result, input, index = 0, expected = roofPolylines) => {
  expect(projectionAt(index)).toEqual({ polylines: expected, refusal: null })
  bound(result, projectionAt(index).polylines, input.shown, input.drawingKey)
}
const engine = (result, input, index = 0) => {
  expect(projectionAt(index).polylines).toEqual(roofPolylines)
  bound(result, projectionAt(index).polylines, input.activeIntake, `engine:${input.activeIntake.documentId}`)
}

describe('unmounted Solar graph overlay hook', () => {
  it('A1B1-H01 disabled skips projection', () => {
    const throwing = Object.defineProperty({}, 'solar_design_graph', {
      get() { throw new Error('private graph message') },
    })
    for (const shown of [{ solar_design_graph: roof() }, throwing]) {
      const h = mount({ ...inputFor(), enabled: false, shown, refreshPending: true, sceneCurrent: false })
      expect(h.result.current).toEqual(N(null))
      calls(0)
      h.unmount()
    }
  })

  it('A1B1-H02 absent graph is silent', () => {
    for (const shown of [{}, { solar_design_graph: undefined }, { solar_design_graph: null }]) {
      solarGraphOverlay.mockClear()
      const h = mount({ ...inputFor(), shown })
      expect(h.result.current).toEqual(N(null))
      expect(projectionAt()).toEqual({ polylines: [], refusal: null })
      calls(1)
      h.unmount()
    }
  })

  it('A1B1-H03 empty graph is silent', () => {
    for (const graph of [{}, G()]) {
      solarGraphOverlay.mockClear()
      const h = mount(inputFor(graph))
      expect(h.result.current).toEqual(N(null))
      expect(projectionAt()).toEqual({ polylines: [], refusal: null })
      calls(1)
      h.unmount()
    }
  })

  it('A1B1-H04 roof binds exact projected geometry', () => {
    const input = inputFor()
    const h = mount(input)
    ordinary(h.result.current, input)
    expect(h.result.current.solarOverlay.polylines).toHaveLength(4)
    expect(h.result.current.solarOverlay.polylines.reduce((n, line) => n + line.pts.length, 0)).toBe(14)
    expect(solarGraphOverlay).toHaveBeenCalledWith(input.shown.solar_design_graph, { metersPerUnit: 1 })
    calls(1)
  })

  it('A1B1-H05 schema two refuses', () => {
    const h = mount(inputFor(G({ graph_schema_version: 2 })))
    expect(h.result.current).toEqual(N('invalid_graph'))
    expect(projectionAt()).toEqual({ polylines: [], refusal: 'invalid_graph' })
    calls(1)
  })

  it('A1B1-H06 missing scale refuses', () => {
    const graph = missingScale()
    const h = mount(inputFor(graph))
    expect(h.result.current).toEqual(N('invalid_scale'))
    expect(solarGraphOverlay).toHaveBeenCalledWith(graph, { metersPerUnit: undefined })
    expect(projectionAt().polylines).toEqual([])
    calls(1)
  })

  it('A1B1-H07 route limit refuses wholesale', () => {
    const h = mount(inputFor(lines(4097)))
    expect(h.result.current).toEqual(N('overlay_limit'))
    expect(projectionAt()).toEqual({ polylines: [], refusal: 'overlay_limit' })
    calls(1)
  })

  it('A1B1-H08 compact Ground scales once', () => {
    const input = inputFor(ground())
    const h = mount(input)
    ordinary(h.result.current, input, 0, groundPolylines)
    expect(solarGraphOverlay).toHaveBeenCalledWith(input.shown.solar_design_graph, { metersPerUnit: 2 })
    calls(1)
  })

  it('A1B1-H09 refusal replaces valid output', () => {
    for (const [code, graph] of refusals()) {
      solarGraphOverlay.mockClear()
      const input = inputFor()
      const h = mount(input)
      ordinary(h.result.current, input)
      h.rerender({ ...input, shown: { solar_design_graph: graph } })
      expect(h.result.current).toEqual(N(code))
      expect(projectionAt(1)).toEqual({ polylines: [], refusal: code })
      calls(2)
      h.unmount()
    }
  })

  it('A1B1-H10 guarded reads cannot throw', () => {
    for (const path of [
      ['solar_design_graph'], ['solar_design_graph', 'project'],
      ['solar_design_graph', 'project', 'units'],
      ['solar_design_graph', 'project', 'units', 'meters_per_unit'],
    ]) {
      const input = inputFor()
      let owner = input.shown
      for (const key of path.slice(0, -1)) owner = owner[key]
      const getter = vi.fn(() => { throw new Error('private graph message') })
      Object.defineProperty(owner, path.at(-1), { get: getter })
      // Execute the actual failing adapter expression before mounting.
      expect(() => input.shown?.solar_design_graph?.project?.units?.meters_per_unit).toThrow('private graph message')
      const h = mount(input)
      expect(h.result.current).toEqual(N('invalid_graph'))
      expect(getter).toHaveBeenCalledTimes(2)
      calls(0)
      h.unmount()
    }
  })

  it('A1B1-H11 unknown refusal has safe fallback', () => {
    solarGraphOverlay.mockImplementationOnce(() => ({ polylines: roofPolylines, refusal: 'private_unknown_code' }))
    const h = mount(inputFor())
    expect(h.result.current).toEqual(N('invalid_graph'))
    calls(1)
    solarGraphOverlay.mockImplementation(actual.solarGraphOverlay)
  })

  it('A1B1-H12 stale scope refuses', () => {
    const h = mount({ ...inputFor(), sceneCurrent: false })
    expect(h.result.current).toEqual(N('drawing_mismatch'))
    calls(1)
  })

  it('A1B1-H13 incomplete refresh refuses', () => {
    const input = inputFor()
    const h = mount({ ...input, refreshPending: true })
    expect(h.result.current).toEqual(N('refresh_pending'))
    calls(1)
    h.rerender(input)
    ordinary(h.result.current, input)
    calls(1)
  })

  it('A1B1-H14 decision precedence is fixed', () => {
    const blocked = { ...inputFor(), sceneCurrent: false, refreshPending: true }
    const populated = { polylines: roofPolylines, refusal: null }
    expect(decideSolarGraphOverlay({ ...blocked, enabled: false }, { ...populated, refusal: 'overlay_limit' })).toEqual(N(null))
    expect(decideSolarGraphOverlay(blocked, { polylines: [], refusal: null })).toEqual(N(null))
    for (const refusal of ['invalid_graph', 'invalid_scale', 'overlay_limit']) {
      expect(decideSolarGraphOverlay(blocked, { ...populated, refusal })).toEqual(N(refusal))
    }
    expect(decideSolarGraphOverlay(blocked, populated)).toEqual(N('refresh_pending'))
    calls(0)
  })

  it('A1B1-H15 matching engine head binds engine scene', () => {
    const input = engineFor()
    const h = mount(input)
    engine(h.result.current, input)
    calls(1)
  })

  it('A1B1-H16 imported and starter provenance refuse', () => {
    const input = engineFor()
    const h = mount(input)
    engine(h.result.current, input)
    for (const documentOrigin of ['import', 'starter', null]) {
      h.rerender({ ...input, engineDocument: { ...input.engineDocument, documentOrigin } })
      expect(h.result.current).toEqual(N('drawing_mismatch'))
      calls(1)
    }
  })

  it('A1B1-H17 engine document identity must match', () => {
    const input = engineFor()
    const h = mount(input)
    engine(h.result.current, input)
    for (const engineDocument of [null, { ...input.engineDocument, documentId: '' },
      { ...input.engineDocument, documentId: 'D-v8.dxf' }]) {
      h.rerender({ ...input, engineDocument })
      expect(h.result.current).toEqual(N('drawing_mismatch'))
      calls(1)
    }
  })

  it('A1B1-H18 requested drawing match is exact', () => {
    const base = inputFor()
    const h = mount(engineFor(base))
    for (const [requestedDrawingId, documentId, admitted] of [
      ['D', 'D-v7.dxf', true], ['D', 'Other-v7.dxf', false],
      ['D', 'D-villa-v7.dxf', false], ['D', 'D-v7.dxf.bak', false],
      ['D', 'D-vx.dxf', false], ['D.[x]', 'D.[x]-v7.dxf', true],
      ['D.[x]', 'Dax-v7.dxf', false],
    ]) {
      const input = engineFor({ ...base, requestedDrawingId }, documentId)
      h.rerender(input)
      if (admitted) engine(h.result.current, input)
      else expect(h.result.current).toEqual(N('drawing_mismatch'))
      calls(1)
    }
  })

  it('A1B1-H19 engine versions must be known positive integers', () => {
    const input = engineFor()
    const h = mount(input)
    engine(h.result.current, input)
    for (const value of [null, 0, -1, 1.5, '7', NaN, Infinity]) {
      for (const next of [
        { ...input, activeVersion: value },
        { ...input, engineDocument: { ...input.engineDocument, committedVersion: value } },
      ]) {
        h.rerender(next)
        expect(h.result.current).toEqual(N('drawing_mismatch'))
        calls(1)
      }
    }
    // Equal invalid pairs: equality alone admits these, so only the integer and positive guards refuse.
    for (const value of [0, -1, 1.5, '7', NaN, Infinity, null]) {
      h.rerender({ ...input, activeVersion: value,
        engineDocument: { ...input.engineDocument, committedVersion: value } })
      expect(h.result.current).toEqual(N('drawing_mismatch'))
      calls(1)
    }
    h.rerender({ ...input, engineDocument: { ...input.engineDocument, committedVersion: 6 } })
    expect(h.result.current).toEqual(N('drawing_mismatch'))
    calls(1)
  })

  it('A1B1-H20 engine must be explicitly clean', () => {
    const input = engineFor()
    const h = mount(input)
    engine(h.result.current, input)
    calls(1)
    for (const engineDirty of [true, undefined, null]) {
      h.rerender({ ...input, engineDirty })
      expect(h.result.current).toEqual(N('drawing_mismatch'))
      calls(1)
    }
    h.rerender(input)
    engine(h.result.current, input)
    calls(1)
  })

  it('A1B1-H21 saved version need not match filename suffix', () => {
    const input = engineFor(inputFor(), 'D-v1.dxf')
    const h = mount(input)
    engine(h.result.current, input)
    calls(1)
  })

  it('A1B1-H22 drawing switch cannot reuse old engine', () => {
    const input = engineFor()
    const h = mount(input)
    engine(h.result.current, input)
    calls(1)
    const switched = { ...input, requestedDrawingId: 'E', drawingKey: 'console:E' }
    h.rerender({ ...switched, sceneCurrent: false })
    expect(h.result.current).toEqual(N('drawing_mismatch'))
    calls(1)
    h.rerender(switched)
    expect(h.result.current).toEqual(N('drawing_mismatch'))
    calls(1)
    const next = engineFor(switched, 'E-v7.dxf')
    h.rerender(next)
    engine(h.result.current, next)
    calls(1)
  })

  it('A1B1-H23 shown replacement follows version history', () => {
    const steps = [inputFor(roof()), { ...inputFor(ground()), activeVersion: 6 },
      { ...inputFor(G()), activeVersion: 5 }, inputFor(roof())]
    const h = mount(steps[0])
    ordinary(h.result.current, steps[0])
    calls(1)
    h.rerender(steps[1])
    ordinary(h.result.current, steps[1], 1, groundPolylines)
    calls(2)
    h.rerender(steps[2])
    expect(h.result.current).toEqual(N(null))
    calls(3)
    h.rerender(steps[3])
    ordinary(h.result.current, steps[3], 3)
    calls(4)
  })

  it('A1B1-H24 wrapper replacement updates binding without projection', () => {
    const input = inputFor()
    const h = mount(input)
    ordinary(h.result.current, input)
    const projected = h.result.current.solarOverlay.polylines
    const next = { ...input, shown: { solar_design_graph: input.shown.solar_design_graph } }
    expect(next.shown).not.toBe(input.shown)
    h.rerender(next)
    bound(h.result.current, projected, next.shown, next.drawingKey)
    calls(1)
  })

  it('A1B1-H25 unrelated notifications never reproject', () => {
    const input = inputFor()
    const h = renderHook(({ input: props }) => useSolarGraphOverlay(props), {
      initialProps: { input, selection: 0, camera: 0 }, reactStrictMode: false,
    })
    ordinary(h.result.current, input)
    calls(1)
    h.rerender({ input, selection: 0, camera: 0 })
    ordinary(h.result.current, input)
    calls(1)
    h.rerender({ input, selection: 1, camera: 1 })
    ordinary(h.result.current, input)
    calls(1)
    const seated = engineFor(input)
    h.rerender({ input: seated, selection: 1, camera: 1 })
    engine(h.result.current, seated)
    calls(1)
    const notified = { ...seated, engineDocument: { ...seated.engineDocument, entityCount: 99 } }
    h.rerender({ input: notified, selection: 1, camera: 1 })
    engine(h.result.current, notified)
    calls(1)
    h.rerender({ input: { ...notified, engineDirty: true }, selection: 1, camera: 1 })
    expect(h.result.current).toEqual(N('drawing_mismatch'))
    calls(1)
    h.rerender({ input: { ...notified, engineDirty: true, refreshPending: true }, selection: 1, camera: 1 })
    expect(h.result.current).toEqual(N('refresh_pending'))
    calls(1)
    h.rerender({ input: notified, selection: 1, camera: 1 })
    engine(h.result.current, notified)
    calls(1)
  })

  it('A1B1-H26 projection dependencies are graph scale and enablement', () => {
    const input = inputFor()
    const h = mount(input)
    ordinary(h.result.current, input)
    calls(1)
    input.shown.solar_design_graph.project.units.meters_per_unit = 2
    const scaled = roofPolylines.map(line => ({
      color: line.color, pts: line.pts.map(([x, y]) => [x / 2, y / 2]),
    }))
    h.rerender({ ...input })
    ordinary(h.result.current, input, 1, scaled)
    calls(2)
    const next = { ...input, shown: { solar_design_graph: JSON.parse(JSON.stringify(input.shown.solar_design_graph)) } }
    h.rerender(next)
    ordinary(h.result.current, next, 2, scaled)
    calls(3)
    h.rerender({ ...next, enabled: false })
    expect(h.result.current).toEqual(N(null))
    calls(3)
    h.rerender(next)
    ordinary(h.result.current, next, 3, scaled)
    calls(4)
  })

  it('A1B1-H27 invalid scene bindings refuse', () => {
    const input = inputFor()
    const projected = { polylines: roofPolylines, refusal: null }
    for (const patch of [{ shown: null }, { drawingKey: null }, { drawingKey: '' },
      { requestedDrawingId: null }, { requestedDrawingId: '' }, { activeIntake: {} }]) {
      expect(decideSolarGraphOverlay({ ...input, ...patch }, projected)).toEqual(N('drawing_mismatch'))
    }
    bound(decideSolarGraphOverlay({ ...input, activeIntake: input.shown }, projected),
      roofPolylines, input.shown, input.drawingKey)
    calls(0)
  })

  it('A1B1-H28 reason map and pure decision contract are frozen', () => {
    const expectedCopy = {
      invalid_graph: 'Solar design lines are hidden because the saved design geometry could not be read.',
      invalid_scale: 'Solar design lines are hidden because the drawing scale is missing or invalid.',
      overlay_limit: 'Solar design lines are hidden because this design exceeds the preview size limit.',
      drawing_mismatch: 'Solar design lines are hidden until the canvas shows the matching saved drawing.',
      refresh_pending: 'Solar design lines are hidden while the saved drawing refresh is incomplete.',
    }
    expect(Object.isFrozen(SOLAR_OVERLAY_REASONS)).toBe(true)
    expect(SOLAR_OVERLAY_REASONS).toEqual(expectedCopy)
    expect(Object.keys(SOLAR_OVERLAY_REASONS).sort()).toEqual(Object.keys(expectedCopy).sort())
    const source = readFileSync(`${process.cwd()}/src/solar/useSolarGraphOverlay.js`, 'utf8')
    expect(source).toContain('export const SOLAR_OVERLAY_REASONS = Object.freeze({')
    for (const [key, sentence] of Object.entries(expectedCopy)) {
      expect(source).toContain(`${key}: '${sentence}'`)
    }
    for (const sentence of Object.values(SOLAR_OVERLAY_REASONS)) {
      expect(typeof sentence).toBe('string')
      expect(sentence.length).toBeGreaterThanOrEqual(12)
      expect(sentence).toMatch(/^[A-Z][a-z ]+\.$/i)
    }
    const cases = [
      [inputFor(), null], [engineFor(), null], [inputFor(G()), null],
      ...refusals().map(([reason, graph]) => [inputFor(graph), reason]),
      [{ ...inputFor(), sceneCurrent: false }, 'drawing_mismatch'],
      [{ ...inputFor(), refreshPending: true }, 'refresh_pending'],
    ]
    for (const [raw, reason] of cases) {
      solarGraphOverlay.mockClear()
      const input = freeze(raw)
      const before = JSON.stringify(input)
      const h = mount(input)
      const projection = freeze(projectionAt())
      const projectionBefore = JSON.stringify(projection)
      const decision = decideSolarGraphOverlay(input, projection)
      expect(decision).toEqual(h.result.current)
      expect(decision.reason).toBe(reason)
      if (decision.solarOverlay) {
        const intake = input.activeIntake ?? input.shown
        bound(decision, projection.polylines, intake,
          input.activeIntake ? `engine:${intake.documentId}` : input.drawingKey)
      } else expect(decision).toEqual(N(reason))
      expect(JSON.stringify(input)).toBe(before)
      expect(JSON.stringify(projection)).toBe(projectionBefore)
      calls(1)
      h.unmount()
    }
  })

  it('A1B1-H29 admission reads each caller value once', () => {
    const counter = counts => (target, name) => new Proxy(target, {
      get(owner, key, receiver) {
        const id = `${name}.${String(key)}`
        counts.set(id, (counts.get(id) ?? 0) + 1)
        return Reflect.get(owner, key, receiver)
      },
    })
    const once = reads => {
      for (const [id, n] of reads) expect([id, n]).toEqual([id, 1])
    }
    for (const requestedDrawingId of ['Other', 'D']) {
      solarGraphOverlay.mockClear()
      const counts = new Map()
      const count = counter(counts)
      const base = engineFor({ ...inputFor(), requestedDrawingId, drawingKey: `console:${requestedDrawingId}` })
      const input = count({ ...base, shown: count(base.shown, 'shown'),
        activeIntake: count(base.activeIntake, 'activeIntake'),
        engineDocument: count(base.engineDocument, 'engineDocument') }, 'input')
      counts.clear()
      const h = mount(input)
      const hookReads = new Map(counts)
      counts.clear()
      const decision = decideSolarGraphOverlay(input, { polylines: roofPolylines, refusal: null })
      const decisionReads = new Map(counts)
      once(hookReads)
      once(decisionReads)
      for (const reads of [hookReads, decisionReads]) {
        expect(reads.get('engineDocument.documentId')).toBe(1)
        expect(reads.get('activeIntake.documentId')).toBe(1)
      }
      expect(hookReads.get('input.shown')).toBe(1)
      expect(hookReads.get('shown.solar_design_graph')).toBe(1)
      calls(1)
      if (requestedDrawingId === 'D') {
        engine(h.result.current, input)
        expect(decision).toEqual(O(roofPolylines, input.activeIntake, 'engine:D-v7.dxf'))
      } else {
        expect(h.result.current).toEqual(N('drawing_mismatch'))
        expect(decision).toEqual(N('drawing_mismatch'))
      }
      h.unmount()
    }
    // One object passed as both the active intake and the engine document is still read once.
    solarGraphOverlay.mockClear()
    let reads = 0
    const shared = { source: 'engine', documentOrigin: 'head', committedVersion: 7, entityCount: 1 }
    Object.defineProperty(shared, 'documentId', {
      enumerable: true,
      get() { reads += 1; return 'D-v7.dxf' },
    })
    const input = { ...inputFor(), activeIntake: shared, engineDocument: shared }
    const h = mount(input)
    expect(reads).toBe(1)
    engine(h.result.current, input)
    calls(1)
    reads = 0
    const decision = decideSolarGraphOverlay(input, { polylines: roofPolylines, refusal: null })
    expect(reads).toBe(1)
    expect(decision).toEqual(O(roofPolylines, shared, 'engine:D-v7.dxf'))
    h.unmount()
  })

  it('A1B1-H30 a throwing admission read refuses instead of escaping', () => {
    const thrower = message => () => { throw new Error(message) }
    const sourceThrows = engineFor()
    sourceThrows.activeIntake = { documentId: 'D-v7.dxf' }
    Object.defineProperty(sourceThrows.activeIntake, 'source', { get: thrower('private source message') })
    const proxyThrows = { ...engineFor(),
      engineDocument: new Proxy({}, { get: thrower('private proxy message') }) }
    const enabledThrows = inputFor()
    delete enabledThrows.enabled
    Object.defineProperty(enabledThrows, 'enabled', { get: thrower('private enabled message') })
    const shownThrows = inputFor()
    delete shownThrows.shown
    Object.defineProperty(shownThrows, 'shown', { get: thrower('private shown message') })
    // The projection follows only the caller's enabled flag and the shown drawing's graph, so an
    // unreadable admission value refuses after projecting; an unreadable enabled flag or shown
    // drawing leaves nothing to project.
    for (const [input, projected] of [[sourceThrows, 1], [proxyThrows, 1], [enabledThrows, 0], [shownThrows, 0]]) {
      solarGraphOverlay.mockClear()
      let h
      expect(() => { h = mount(input) }).not.toThrow()
      expect(h.result.current).toEqual(N('drawing_mismatch'))
      calls(projected)
      let decision
      expect(() => { decision = decideSolarGraphOverlay(input, { polylines: roofPolylines, refusal: null }) }).not.toThrow()
      expect(decision).toEqual(N('drawing_mismatch'))
      expect(JSON.stringify(decision)).not.toMatch(/private/)
      h.unmount()
    }
  })

  it('A1B1-H31 unreadable admission values keep the frozen precedence', () => {
    const populated = { polylines: roofPolylines, refusal: null }
    const fields = ['refreshPending', 'sceneCurrent', 'shown', 'drawingKey', 'requestedDrawingId',
      'activeIntake', 'engineDocument', 'activeVersion', 'engineDirty', 'activeIntake.source']
    for (const field of fields) {
      const getter = vi.fn(() => { throw new Error('private admission message') })
      const throwing = (patch = {}) => {
        const input = { ...engineFor(), ...patch }
        if (field === 'activeIntake.source') {
          input.activeIntake = { documentId: 'D-v7.dxf' }
          Object.defineProperty(input.activeIntake, 'source', { get: getter })
        } else {
          delete input[field]
          Object.defineProperty(input, field, { get: getter })
        }
        return input
      }
      expect(decideSolarGraphOverlay(throwing(), { polylines: [], refusal: null })).toEqual(N(null))
      expect(decideSolarGraphOverlay(throwing(), { ...populated, refusal: 'overlay_limit' })).toEqual(N('overlay_limit'))
      if (field !== 'refreshPending') {
        expect(decideSolarGraphOverlay(throwing({ refreshPending: true }), populated)).toEqual(N('refresh_pending'))
      }
      expect([field, getter.mock.calls.length]).toEqual([field, 0])
      const decision = decideSolarGraphOverlay(throwing(), populated)
      expect([field, decision]).toEqual([field, N('drawing_mismatch')])
      expect([field, getter.mock.calls.length]).toEqual([field, 1])
    }
    calls(0)
  })

  it('A1B1-H32 projection fields are read once under a guard', () => {
    const input = inputFor()
    const throwing = { polylines: roofPolylines }
    Object.defineProperty(throwing, 'refusal', { get() { throw new Error('private projection message') } })
    let decision
    expect(() => { decision = decideSolarGraphOverlay(input, throwing) }).not.toThrow()
    expect(decision).toEqual(N('invalid_graph'))
    expect(JSON.stringify(decision)).not.toMatch(/private/)
    const { proxy, revoke } = Proxy.revocable({ polylines: roofPolylines, refusal: null }, {})
    revoke()
    expect(() => { decision = decideSolarGraphOverlay(input, proxy) }).not.toThrow()
    expect(decision).toEqual(N('invalid_graph'))
    let polylineReads = 0
    const shifting = { refusal: null }
    Object.defineProperty(shifting, 'polylines', {
      get() { polylineReads += 1; return polylineReads === 1 ? roofPolylines : [] },
    })
    bound(decideSolarGraphOverlay(input, shifting), roofPolylines, input.shown, input.drawingKey)
    expect(polylineReads).toBe(1)
    let refusalReads = 0
    const firstRefusal = { polylines: roofPolylines }
    Object.defineProperty(firstRefusal, 'refusal', {
      get() { refusalReads += 1; return refusalReads === 1 ? 'overlay_limit' : null },
    })
    expect(decideSolarGraphOverlay(input, firstRefusal)).toEqual(N('overlay_limit'))
    expect(refusalReads).toBe(1)
    for (const polylines of [null, undefined, 'lines', { length: 1 }]) {
      expect(decideSolarGraphOverlay(input, { polylines, refusal: null })).toEqual(N('invalid_graph'))
    }
    expect(decideSolarGraphOverlay(input, null)).toEqual(N('invalid_graph'))
    calls(0)
  })

  it('A1B1-H33 an unreadable admission value keeps the projection', () => {
    const input = engineFor()
    const h = mount(input)
    engine(h.result.current, input)
    calls(1)
    const dirtyThrows = { ...input }
    delete dirtyThrows.engineDirty
    Object.defineProperty(dirtyThrows, 'engineDirty', {
      enumerable: true, get() { throw new Error('private dirty message') },
    })
    h.rerender(dirtyThrows)
    expect(h.result.current).toEqual(N('drawing_mismatch'))
    calls(1)
    const sourceThrows = { ...input, activeIntake: { documentId: 'D-v7.dxf' } }
    Object.defineProperty(sourceThrows.activeIntake, 'source', { get() { throw new Error('private source message') } })
    h.rerender(sourceThrows)
    expect(h.result.current).toEqual(N('drawing_mismatch'))
    calls(1)
    h.rerender(input)
    engine(h.result.current, input)
    calls(1)
    h.unmount()
  })
})
