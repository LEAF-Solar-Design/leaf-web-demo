import { afterEach, describe, expect, it, vi } from 'vitest'
import * as THREE from 'three'
import { solarGraphOverlay } from './solarGraphOverlay.js'
import { buildSolarGraphOverlayLayer } from './solarGraphOverlayLayer.js'

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

afterEach(() => vi.restoreAllMocks())
const green = '#22c55e', amber = '#f59e0b', purple = '#a855f7', blue = '#38bdf8', pink = '#f472b6'
const project = graph => solarGraphOverlay(graph, { metersPerUnit: graph?.project?.units?.meters_per_unit })
const roofOutput = () => {
  const result = project(roof())
  expect(result).toEqual({ polylines: roofPolylines, refusal: null })
  return result.polylines
}
const identity = object => {
  expect(object.position.toArray()).toEqual([0, 0, 0])
  expect(object.rotation.toArray()).toEqual([0, 0, 0, 'XYZ'])
  expect(object.quaternion.toArray()).toEqual([0, 0, 0, 1])
  expect(object.scale.toArray()).toEqual([1, 1, 1])
  expect(object.matrix.elements).toEqual(new THREE.Matrix4().elements)
}
const common = (layer, colors, counts) => {
  const { group } = layer
  expect(group).toBeInstanceOf(THREE.Group)
  identity(group)
  expect(group.children).toHaveLength(colors.length)
  const geometries = new Set()
  const materials = new Set()
  group.children.forEach((mesh, index) => {
    expect(mesh).toBeInstanceOf(THREE.LineSegments)
    expect(mesh.parent).toBe(group)
    identity(mesh)
    expect(mesh.renderOrder).toBe(8)
    expect(mesh.geometry).toBeInstanceOf(THREE.BufferGeometry)
    expect(mesh.geometry.index).toBe(null)
    const position = mesh.geometry.getAttribute('position')
    expect(position).toBeInstanceOf(THREE.Float32BufferAttribute)
    expect(position.array).toBeInstanceOf(Float32Array)
    expect(position.itemSize).toBe(3)
    expect(position.count).toBe(counts[index])
    expect(position.array.length).toBe(counts[index] * 3)
    expect(position.usage).toBe(THREE.StaticDrawUsage)
    for (let vertex = 0; vertex < position.count; vertex += 1) expect(position.getZ(vertex)).toBe(1.5)
    expect(mesh.material).toBeInstanceOf(THREE.LineBasicMaterial)
    expect(mesh.material.isLineDashedMaterial).not.toBe(true)
    expect(mesh.material.color.getHexString()).toBe(colors[index].slice(1))
    expect(mesh.material.depthTest).toBe(false)
    expect(mesh.material.depthWrite).toBe(false)
    expect(mesh.material.transparent).toBe(false)
    expect(mesh.material.opacity).toBe(1)
    geometries.add(mesh.geometry)
    materials.add(mesh.material)
  })
  expect(geometries.size).toBe(colors.length)
  expect(materials.size).toBe(colors.length)
}
const watch = layer => layer.group.children.map(mesh => ({
  geometry: mesh.geometry, material: mesh.material,
  geometryDispose: vi.spyOn(mesh.geometry, 'dispose'),
  materialDispose: vi.spyOn(mesh.material, 'dispose'),
}))
const disposed = (resources, count) => {
  for (const resource of resources) {
    expect(resource.geometryDispose).toHaveBeenCalledTimes(count)
    expect(resource.materialDispose).toHaveBeenCalledTimes(count)
  }
}
const vertices = mesh => {
  const position = mesh.geometry.getAttribute('position')
  return Array.from({ length: position.count }, (_, index) => [position.getX(index), position.getY(index)])
}

describe('unmounted Solar graph overlay layer', () => {
  it('A1B1-L01 empty layer owns no resources', () => {
    const geometryDispose = vi.spyOn(THREE.BufferGeometry.prototype, 'dispose')
    const materialDispose = vi.spyOn(THREE.Material.prototype, 'dispose')
    const layer = buildSolarGraphOverlayLayer([])
    const second = buildSolarGraphOverlayLayer([])
    expect(layer.group).not.toBe(second.group)
    expect(layer.group).toBeInstanceOf(THREE.Group)
    expect(layer.group.parent).toBe(null)
    common(layer, [], [])
    layer.dispose()
    layer.dispose()
    second.dispose()
    expect(layer.group.children).toEqual([])
    expect(geometryDispose).not.toHaveBeenCalled()
    expect(materialDispose).not.toHaveBeenCalled()
  })

  it('A1B1-L02 roof batches exact colours and vertices', () => {
    const layer = buildSolarGraphOverlayLayer(roofOutput())
    expect(layer.group.parent).toBe(null)
    common(layer, [green, amber, blue], [8, 8, 4])
    expect(layer.group.children.reduce((sum, mesh) => sum + mesh.geometry.getAttribute('position').count, 0)).toBe(20)
    expect(layer.group.children.reduce((sum, mesh) => sum + mesh.geometry.getAttribute('position').array.length, 0)).toBe(60)
    layer.dispose()
  })

  it('A1B1-L03 segment buffers preserve boundaries and z', () => {
    const layer = buildSolarGraphOverlayLayer(roofOutput())
    common(layer, [green, amber, blue], [8, 8, 4])
    expect(vertices(layer.group.children[0])).toEqual([
      [0, 0], [1, 0], [1, 0], [2, 0], [2, 1], [1, 1], [1, 1], [0, 1],
    ])
    expect(vertices(layer.group.children[1])).toEqual([
      [2.75, 1.75], [3.25, 1.75], [3.25, 1.75], [3.25, 2.25],
      [3.25, 2.25], [2.75, 2.25], [2.75, 2.25], [2.75, 1.75],
    ])
    expect(vertices(layer.group.children[2])).toEqual([[2, 0], [3, 0], [3, 0], [3, 2]])
    layer.dispose()
  })

  it('A1B1-L04 compact Ground creates one segment', () => {
    const projection = project(ground())
    expect(projection).toEqual({ polylines: groundPolylines, refusal: null })
    const layer = buildSolarGraphOverlayLayer(projection.polylines)
    common(layer, [green], [2])
    expect(Array.from(layer.group.children[0].geometry.getAttribute('position').array)).toEqual([1, -1, 1.5, .5, -.5, 1.5])
    layer.dispose()
  })

  it('A1B1-L05 five semantic colours cap mesh count', () => {
    const graph = roof()
    graph.inverters.push(I(2, [0, 0], 'combiner_box'))
    graph.routes.push(R(2, 'feeder', [[1, 0], [1, 2]]))
    const projection = project(graph)
    expect(projection).toEqual({ polylines: [
      ...roofPolylines.slice(0, 3),
      { color: purple, pts: [[-.25, -.25], [.25, -.25], [.25, .25], [-.25, .25], [-.25, -.25]] },
      roofPolylines[3], { color: pink, pts: [[1, 0], [1, 2]] },
    ], refusal: null })
    expect(projection.polylines).toHaveLength(6)
    expect(projection.polylines.reduce((n, line) => n + line.pts.length, 0)).toBe(21)
    const layer = buildSolarGraphOverlayLayer(projection.polylines)
    common(layer, [green, amber, purple, blue, pink], [8, 8, 8, 4, 2])
    expect(layer.group.children.reduce((sum, mesh) => sum + mesh.geometry.getAttribute('position').count, 0)).toBe(30)
    layer.dispose()
  })

  it('A1B1-L06 route boundary stays one batch', () => {
    const projection = project(lines(4096))
    expect(projection.refusal).toBe(null)
    expect(projection.polylines).toHaveLength(4096)
    const layer = buildSolarGraphOverlayLayer(projection.polylines)
    common(layer, [pink], [8192])
    const positions = layer.group.children[0].geometry.getAttribute('position').array
    expect(positions).toHaveLength(24576)
    for (let offset = 0; offset < positions.length; offset += 6) {
      expect(Array.from(positions.slice(offset, offset + 6))).toEqual([0, 0, 1.5, 1, 1, 1.5])
    }
    layer.dispose()
  })

  it('A1B1-L07 refused replacement leaves no layer geometry', () => {
    for (const [reason, graph] of refusals()) {
      const scene = new THREE.Scene()
      const old = buildSolarGraphOverlayLayer(roofOutput())
      common(old, [green, amber, blue], [8, 8, 4])
      const resources = watch(old)
      expect(resources).toHaveLength(3)
      scene.add(old.group)
      const projection = project(graph)
      expect(projection).toEqual({ polylines: [], refusal: reason })
      old.dispose()
      const replacement = buildSolarGraphOverlayLayer(projection.polylines)
      scene.add(replacement.group)
      expect(replacement.group.children).toEqual([])
      expect(old.group.parent).toBe(null)
      expect(old.group.children).toEqual([])
      disposed(resources, 1)
      replacement.dispose()
    }
  })

  it('A1B1-L08 disposal is captured and idempotent', () => {
    for (const cleared of [false, true]) {
      const layer = buildSolarGraphOverlayLayer(roofOutput())
      common(layer, [green, amber, blue], [8, 8, 4])
      const resources = watch(layer)
      const scene = new THREE.Scene()
      scene.add(layer.group)
      if (cleared) {
        layer.group.clear()
        layer.group.removeFromParent()
      }
      disposed(resources, 0)
      layer.dispose()
      expect(layer.group.parent).toBe(null)
      expect(layer.group.children).toEqual([])
      disposed(resources, 1)
      layer.dispose()
      disposed(resources, 1)
    }
  })

  it('A1B1-L09 independent layers preserve inputs and siblings', () => {
    const roofInput = freeze(roofOutput())
    const groundInput = freeze(project(ground()).polylines)
    const before = JSON.stringify([roofInput, groundInput])
    const a = buildSolarGraphOverlayLayer(roofInput)
    const b = buildSolarGraphOverlayLayer(groundInput)
    common(a, [green, amber, blue], [8, 8, 4])
    common(b, [green], [2])
    expect(a.group).not.toBe(b.group)
    const aResources = watch(a)
    const bResources = watch(b)
    expect(aResources).toHaveLength(3)
    expect(bResources).toHaveLength(1)
    for (const owned of aResources) {
      expect(owned.geometry).not.toBe(bResources[0].geometry)
      expect(owned.material).not.toBe(bResources[0].material)
    }
    const sibling = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial())
    const siblingGeometryDispose = vi.spyOn(sibling.geometry, 'dispose')
    const siblingMaterialDispose = vi.spyOn(sibling.material, 'dispose')
    const scene = new THREE.Scene()
    scene.add(a.group, b.group, sibling)
    a.dispose()
    expect(a.group.parent).toBe(null)
    expect(a.group.children).toEqual([])
    expect(scene.children).toEqual([b.group, sibling])
    expect(b.group.parent).toBe(scene)
    expect(sibling.parent).toBe(scene)
    disposed(aResources, 1)
    disposed(bResources, 0)
    expect(siblingGeometryDispose).not.toHaveBeenCalled()
    expect(siblingMaterialDispose).not.toHaveBeenCalled()
    b.dispose()
    disposed(bResources, 1)
    disposed(aResources, 1)
    expect(scene.children).toEqual([sibling])
    expect(siblingGeometryDispose).not.toHaveBeenCalled()
    expect(siblingMaterialDispose).not.toHaveBeenCalled()
    expect(JSON.stringify([roofInput, groundInput])).toBe(before)
    sibling.geometry.dispose()
    sibling.material.dispose()
  })
})

