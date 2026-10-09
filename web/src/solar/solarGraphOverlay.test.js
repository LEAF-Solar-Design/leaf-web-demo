import { describe, expect, it } from 'vitest'
import { solarGraphOverlay } from './solarGraphOverlay.js'
import { decodeGroundFrameSlots } from './solarGroundSlots.js'

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
const points = n => G({ routes: [
  R(1, 'feeder', Array.from({ length: 50000 }, () => [0, 0])),
  R(2, 'feeder', Array.from({ length: n - 50000 }, () => [1, 1])),
] })
const L = (pts, color) => ({ pts, color })
const OK = polylines => ({ polylines, refusal: null })
const NO = refusal => ({ polylines: [], refusal })
const green = '#22c55e', amber = '#f59e0b', purple = '#a855f7', blue = '#38bdf8', pink = '#f472b6'
const empty = OK([])
const metreLines = [
  L([[0, 0], [1, 0], [2, 0]], green),
  L([[2, 1], [1, 1], [0, 1]], green),
  L([[2.75, 1.75], [3.25, 1.75], [3.25, 2.25], [2.75, 2.25], [2.75, 1.75]], amber),
  L([[2, 0], [3, 0], [3, 2]], blue),
]
const inchLines = [
  L([[0, 0], [39.37007874015748, 0], [78.74015748031496, 0]], green),
  L([[78.74015748031496, 39.37007874015748], [39.37007874015748, 39.37007874015748], [0, 39.37007874015748]], green),
  L([[108.26771653543308, 68.8976377952756], [127.95275590551182, 68.8976377952756],
    [127.95275590551182, 88.58267716535434], [108.26771653543308, 88.58267716535434],
    [108.26771653543308, 68.8976377952756]], amber),
  L([[78.74015748031496, 0], [118.11023622047244, 0], [118.11023622047244, 78.74015748031496]], blue),
]
const footLines = [
  L([[0, 0], [3.280839895013123, 0], [6.561679790026246, 0]], green),
  L([[6.561679790026246, 3.280839895013123], [3.280839895013123, 3.280839895013123], [0, 3.280839895013123]], green),
  L([[9.02230971128609, 5.741469816272965], [10.662729658792651, 5.741469816272965],
    [10.662729658792651, 7.381889763779527], [9.02230971128609, 7.381889763779527],
    [9.02230971128609, 5.741469816272965]], amber),
  L([[6.561679790026246, 0], [9.84251968503937, 0], [9.84251968503937, 6.561679790026246]], blue),
]
const run = graph => solarGraphOverlay(graph, { metersPerUnit: 1 })
const freeze = value => {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value) }
  return value
}

describe('solarGraphOverlay', () => {
  it('SGO01 projects metre geometry in viewer order without mutation or aliases', () => {
    const g = freeze(roof())
    const before = JSON.stringify(g)
    const result = run(g)
    expect(result).toEqual(OK(metreLines))
    expect(Object.keys(result).sort()).toEqual(['polylines', 'refusal'])
    for (const line of result.polylines) expect(Object.keys(line).sort()).toEqual(['color', 'pts'])
    result.polylines[0].pts[0][0] = 99
    result.polylines[3].pts[0][0] = 99
    expect(JSON.stringify(g)).toBe(before)
    expect(run(g)).toEqual(OK(metreLines))
  })
  it('SGO02 converts metres into inches exactly once', () => {
    expect(solarGraphOverlay(roofAt(.0254), { metersPerUnit: .0254 })).toEqual(OK(inchLines))
  })
  it('SGO03 converts metres into feet at double precision', () => {
    expect(solarGraphOverlay(roofAt(.3048), { metersPerUnit: .3048 })).toEqual(OK(footLines))
  })
  it('SGO04 prefers stored string routes and drops validated Z', () => {
    const g = roof(); g.strings[0].route = [[9, 8, 7], [10, 8, 6]]
    expect(run(g)).toEqual(OK([L([[9, 8], [10, 8]], green), ...metreLines.slice(1)]))
  })
  it('SGO05 uses the real decoded absolute Ground centres in reference order', () => {
    const g = ground()
    expect(decodeGroundFrameSlots(g.frames).frames[0].slots).toEqual([
      { panelId: U('panel', 1), slotNumber: 1, centre: { x: 1, y: -1 } },
      { panelId: U('panel', 2), slotNumber: 2, centre: { x: 2, y: -2 } },
    ])
    expect(solarGraphOverlay(g, { metersPerUnit: 2 })).toEqual(OK([L([[1, -1], [.5, -.5]], green)]))
  })
  it('SGO06 permits absent and empty graphs without options', () => {
    for (const g of [undefined, null, {}, G()]) expect(solarGraphOverlay(g)).toEqual(empty)
  })
  it('SGO07 refuses missing or invalid scale without a default', () => {
    for (const metersPerUnit of [undefined, NaN, Infinity, 0, -1, '1']) {
      expect(solarGraphOverlay(roof(), { metersPerUnit })).toEqual(NO('invalid_scale'))
    }
    expect(solarGraphOverlay(roof())).toEqual(NO('invalid_scale'))
    expect(solarGraphOverlay(roof(), {})).toEqual(NO('invalid_scale'))
  })
  it('SGO08 refuses nonfinite panel geometry wholesale', () => {
    const g = roof(); g.panels[1].centre = [NaN, 0]
    expect(run(g)).toEqual(NO('invalid_graph'))
  })
  it('SGO09 refuses malformed roots, lists, entities, versions and missing units', () => {
    const g = roof(); delete g.project.units
    for (const value of [false, [], G({ panels: null }), G({ graph_schema_version: 2 }), G({ strings: [{}] }), g]) {
      expect(run(value)).toEqual(NO('invalid_graph'))
    }
  })
  it('SGO10 refuses unresolved string references', () => {
    const g = roof(); g.strings[0].ordered_panel_refs[1] = U('panel', 99)
    expect(run(g)).toEqual(NO('invalid_graph'))
  })
  it('SGO11 propagates the Ground decoder refusal', () => {
    const g = ground(); g.frames[0].ground_slots.centres = ''
    expect(solarGraphOverlay(g, { metersPerUnit: 2 })).toEqual(NO('invalid_graph'))
  })
  it('SGO12 refuses duplicate panel IDs', () => {
    const g = roof(); g.panels.push(P(1, [8, 8]))
    expect(run(g)).toEqual(NO('invalid_graph'))
  })
  it('SGO13 draws combiners, end homeruns and feeders while excluding trenches', () => {
    expect(run(G({ inverters: [I(1, [0, 0], 'combiner_box')], routes: [
      R(1, 'end homerun', [[0, 0, 9], [1, 0, 8]]), R(2, 'feeder', [[1, 0], [1, 2]]),
      R(3, 'trench', [[7, 7], [8, 8]]),
    ] }))).toEqual(OK([
      L([[-.25, -.25], [.25, -.25], [.25, .25], [-.25, .25], [-.25, -.25]], purple),
      L([[0, 0], [1, 0]], blue), L([[1, 0], [1, 2]], pink),
    ]))
  })
  it('SGO14 omits invisible fallback strings and empty conductor routes', () => {
    expect(run(G({ panels: [P(1, [2, 3])], strings: [S(1, []), S(2, [1])],
      routes: [R(1, 'feeder', [])] }))).toEqual(empty)
  })
  it('SGO15 draws nothing for a single-panel string and keeps every other line', () => {
    expect(run(G({ panels: [P(1, [0, 0])], strings: [S(1, [1], [[0, 0]])] }))).toEqual(empty)
    const g = roof()
    g.panels.push(P(7, [5, 5]))
    g.strings.push(S(3, [7], [[5, 5]]))
    expect(run(g)).toEqual(OK(metreLines))
  })
  it('SGO35 draws the panel-centre line when a multi-panel string stores one route point', () => {
    expect(run(G({ panels: [P(1, [0, 0]), P(2, [1, 0])], strings: [S(1, [1, 2], [[9, 9]])] })))
      .toEqual(OK([L([[0, 0], [1, 0]], green)]))
  })
  it('SGO36 draws nothing for a one-point conductor route and keeps the others', () => {
    expect(run(G({ routes: [R(1, 'feeder', [[3, 3]]), R(2, 'feeder', [[0, 0], [1, 1]]),
      R(3, 'trench', [[4, 4]])] }))).toEqual(OK([L([[0, 0], [1, 1]], pink)]))
  })
  it('SGO37 still refuses a malformed single point', () => {
    for (const point of [['0', 0], [NaN, 0], [1], [0, 0, 'z'], null]) {
      expect(run(G({ panels: [P(1, [0, 0])], strings: [S(1, [1], [point])] }))).toEqual(NO('invalid_graph'))
      expect(run(G({ routes: [R(1, 'feeder', [point])] }))).toEqual(NO('invalid_graph'))
    }
  })
  it('SGO38 counts a one-point path toward the read-point bound', () => {
    const big = n => R(n, 'trench', Array.from({ length: 249999 }, () => [0, 0]))
    expect(run(G({ routes: [big(1), big(2), R(3, 'feeder', [[1, 1]]), R(4, 'feeder', [[2, 2]])] })))
      .toEqual(empty)
    expect(run(G({ routes: [big(1), big(2), R(3, 'feeder', [[1, 1]]), R(4, 'feeder', [[2, 2]]),
      R(5, 'feeder', [[3, 3]])] }))).toEqual(NO('overlay_limit'))
  })
  it('SGO39 draws nothing for a single-panel string on a decoded Ground frame', () => {
    const g = ground()
    const centre = decodeGroundFrameSlots(g.frames).frames[0].slots[1].centre
    g.strings = [S(1, [2], [[centre.x, centre.y]]), S(2, [1])]
    expect(solarGraphOverlay(g, { metersPerUnit: 2 })).toEqual(empty)
  })
  it('SGO16 includes the 4096-polyline boundary', () => {
    expect(run(lines(4096))).toEqual(OK(Array.from({ length: 4096 }, () => L([[0, 0], [1, 1]], pink))))
  })
  it('SGO17 refuses 4097 polylines wholesale', () => {
    expect(run(lines(4097))).toEqual(NO('overlay_limit'))
  })
  it('SGO18 includes the aggregate 100000-point boundary', () => {
    expect(run(points(100000))).toEqual(OK([
      L(Array.from({ length: 50000 }, () => [0, 0]), pink),
      L(Array.from({ length: 50000 }, () => [1, 1]), pink),
    ]))
  })
  it('SGO19 refuses 100001 aggregate emitted points wholesale', () => {
    expect(run(points(100001))).toEqual(NO('overlay_limit'))
  })
  it('SGO20 refuses finite doubles that overflow the viewer Float32 buffer', () => {
    expect(run(G({ inverters: [I(1, [1e308, 1])] }))).toEqual(NO('invalid_graph'))
  })
  it('SGO21 ignores unread metadata and unknown fields', () => {
    const g = roof(); delete g.settings; delete g.schedules
    g.extra = { deep: { a: [1, { b: 2 }] } }; g.panels[0].unknown_field = 7
    expect(run(g)).toEqual(OK(metreLines))
  })
  it('SGO22 requires numeric coordinates without coercion', () => {
    const equipment = roof(); equipment.inverters[0].position = ['3', 2]
    expect(run(equipment)).toEqual(NO('invalid_graph'))
    const route = roof(); route.routes[0].points[1] = [3, true]
    expect(run(route)).toEqual(NO('invalid_graph'))
  })
  it('SGO23 refuses a list whose length is not a non-negative safe integer', () => {
    const lengthProxy = length => new Proxy([], {
      get: (target, key, receiver) => (key === 'length' ? length : Reflect.get(target, key, receiver)),
    })
    const bypass = G({ routes: [
      R(1, 'feeder', lengthProxy(-100000)),
      R(2, 'feeder', Array.from({ length: 150000 }, () => [0, 0])),
    ] })
    expect(run(bypass)).toEqual(NO('invalid_graph'))
    for (const length of [-1, NaN, 2.5, 2 ** 53]) {
      expect(run(G({ routes: [R(1, 'feeder', lengthProxy(length))] }))).toEqual(NO('invalid_graph'))
      expect(run(G({ panels: lengthProxy(length) }))).toEqual(NO('invalid_graph'))
    }
  })
  it('SGO24 reads every coordinate exactly once, by index, never through an iterator', () => {
    let reads = 0
    const changing = [0, 0]
    Object.defineProperty(changing, 0, { enumerable: true, get: () => { reads += 1; return reads === 1 ? 0 : '7' } })
    expect(run(G({ routes: [R(1, 'feeder', [changing, [1, 1]])] }))).toEqual(OK([L([[0, 0], [1, 1]], pink)]))
    expect(reads).toBe(1)
    const iterated = G({ routes: [R(1, 'feeder', [[0, 0], [3, true]])] })
    iterated.routes[0].points[Symbol.iterator] = function* () { yield [0, 0]; yield [1, 1] }
    expect(run(freeze(iterated))).toEqual(NO('invalid_graph'))
    const position = [3, 2]
    position[Symbol.iterator] = function* () { yield 30; yield 2 }
    expect(run(G({ inverters: [I(1, position)] }))).toEqual(OK([
      L([[2.75, 1.75], [3.25, 1.75], [3.25, 2.25], [2.75, 2.25], [2.75, 1.75]], amber),
    ]))
  })
  it('SGO25 reads lists by index, so a hole or an empty iterator hides nothing', () => {
    const panels = [P(1, [0, 0]), P(2, [1, 0]), P(3, [2, 0])]
    delete panels[1]
    panels[Symbol.iterator] = function* () {}
    expect(run(G({ panels }))).toEqual(NO('invalid_graph'))
    const disguised = [P(1, ['0', 0])]
    disguised[Symbol.iterator] = function* () { yield P(1, [0, 0]) }
    expect(run(G({ panels: disguised }))).toEqual(NO('invalid_graph'))
    const trench = Array.from({ length: 500001 }, () => [0, 0])
    trench[Symbol.iterator] = function* () {}
    expect(run(G({ routes: [R(1, 'trench', trench)] }))).toEqual(NO('overlay_limit'))
  })
  it('SGO26 refuses a malformed third coordinate member', () => {
    for (const z of [NaN, Infinity, '3', null, true]) {
      const g = roof(); g.routes[0].points[1] = [3, 0, z]
      expect(run(g)).toEqual(NO('invalid_graph'))
    }
  })
  it('SGO27 refuses a malformed centre on a panel no string draws', () => {
    for (const centre of [[0], [0, 1, 2, 3], ['0', 1], [0, true], null, undefined]) {
      expect(run(G({ panels: [P(1, centre)] }))).toEqual(NO('invalid_graph'))
    }
  })
  it('SGO28 refuses an unresolved reference even when a stored route draws the string', () => {
    const g = roof(); g.strings[0].route = [[0, 0], [1, 0]]; g.strings[0].ordered_panel_refs[1] = U('panel', 99)
    expect(run(g)).toEqual(NO('invalid_graph'))
  })
  it('SGO29 refuses a repeated reference even when a stored route draws the string', () => {
    const g = roof(); g.strings[0].route = [[0, 0], [1, 0]]; g.strings[0].ordered_panel_refs[2] = U('panel', 1)
    expect(run(g)).toEqual(NO('invalid_graph'))
  })
  it('SGO30 refuses an empty id in every list', () => {
    for (const key of ['panels', 'strings', 'inverters', 'routes']) {
      const g = roof(); g[key][0].id = ''
      expect(run(g)).toEqual(NO('invalid_graph'))
    }
    const frame = ground(); frame.frames[0].id = ''
    expect(solarGraphOverlay(frame, { metersPerUnit: 2 })).toEqual(NO('invalid_graph'))
  })
  it('SGO31 refuses a list of 100001 entries and admits 100000', () => {
    const many = Array.from({ length: 100001 }, (_, index) => ({ id: 'p' + index, centre: [0, 0] }))
    expect(run(G({ panels: many }))).toEqual(NO('overlay_limit'))
    expect(run(G({ panels: many.slice(0, 100000) }))).toEqual(empty)
  })
  it('SGO32 counts every read point toward the 500000 bound, drawn or not', () => {
    const trench = n => R(1, 'trench', Array.from({ length: n }, () => [0, 0]))
    expect(run(G({ routes: [trench(500000)] }))).toEqual(empty)
    expect(run(G({ routes: [trench(500001)] }))).toEqual(NO('overlay_limit'))
  })
  it('SGO33 sums read points across paths toward the 500000 bound', () => {
    const trench = (n, count) => R(n, 'trench', Array.from({ length: count }, () => [0, 0]))
    expect(run(G({ routes: [trench(1, 250000), trench(2, 250000)] }))).toEqual(empty)
    expect(run(G({ routes: [trench(1, 250001), trench(2, 250001)] }))).toEqual(NO('overlay_limit'))
  })
  it('SGO34 ignores plan fields inherited from Object.prototype', () => {
    const expected = run(roof())
    expect(expected.refusal).toBe(null)
    expect(expected.polylines.length).toBeGreaterThan(0)
    for (const [key, value] of [['points', [['9', '8'], ['10', '8']]], ['marker', [9, 8]]]) {
      Object.prototype[key] = value
      let got
      try { got = run(roof()) } finally { delete Object.prototype[key] }
      expect(got).toEqual(expected)
    }
  })
})
