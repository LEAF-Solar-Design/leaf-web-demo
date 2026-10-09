import { describe, it, expect, vi, afterEach } from 'vitest'
import { createRef, StrictMode, useCallback, useState } from 'react'
import { render, cleanup, act, fireEvent } from '@testing-library/react'
import * as THREE from 'three'

const gl = vi.hoisted(() => ({ renderers: [], controls: [], layers: [], raycasts: [] }))

vi.mock('three', async (importOriginal) => {
  const actual = await importOriginal()
  class WebGLRenderer {
    constructor() {
      this.domElement = document.createElement('canvas')
      this.scene = null
      this.disposed = false
      gl.renderers.push(this)
    }
    setPixelRatio() {}
    setSize() {}
    render(scene) { this.scene = scene }
    dispose() { this.disposed = true }
  }
  return { ...actual, WebGLRenderer }
})

vi.mock('three/examples/jsm/controls/OrbitControls.js', async () => {
  const actual = await vi.importActual('three')
  class OrbitControls {
    constructor(camera, dom) {
      this.object = camera
      this.domElement = dom
      this.target = new actual.Vector3()
      this.enabled = true
      this.mouseButtons = {}
      this.touches = {}
      gl.controls.push(this)
    }
    addEventListener() {}
    removeEventListener() {}
    update() {}
    dispose() {}
  }
  return { OrbitControls }
})

vi.mock('../solar/solarGraphOverlayLayer.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    buildSolarGraphOverlayLayer: (polylines) => {
      const layer = actual.buildSolarGraphOverlayLayer(polylines)
      gl.layers.push(layer)
      return layer
    },
  }
})

import Viewer from './Viewer.jsx'
import { useSolarGraphOverlay } from '../solar/useSolarGraphOverlay.js'
import { solarGraphOverlay, solarOverlayCanvasIntake } from '../solar/solarGraphOverlay.js'

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }

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

const colorForLayer = () => '#888888'
const intakeOf = graph => ({
  polylines: [{ handle: 'A', layer: '0', closed: true, pts: [[0,0],[10,0],[10,10],[0,10]] }],
  inserts: [], faces3d: [], layers: [{ name: '0' }],
  ...(graph === undefined ? {} : { solar_design_graph: graph }),
})
const inputOf = shown => ({
  enabled: true, shown, drawingKey: 'console:D', sceneCurrent: true,
  refreshPending: false, activeIntake: null, engineDocument: null,
  engineDirty: false, activeVersion: 7, requestedDrawingId: 'D',
})
const overlayOf = intake => ({
  polylines: solarGraphOverlay(intake.solar_design_graph, {
    metersPerUnit: intake.solar_design_graph?.project?.units?.meters_per_unit,
  }).polylines,
  intake, drawingKey: 'console:D',
})
function Harness({ input, viewerRef, intake, ...props }) {
  const { solarOverlay } = useSolarGraphOverlay(input)
  return <Viewer ref={viewerRef} intake={intake} colorForLayer={colorForLayer}
    drawingKey="console:D" solarOverlay={solarOverlay} {...props} />
}
function mountViewer(intake, props = {}, strict = false) {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  const ref = createRef()
  const element = next => <Viewer ref={ref} intake={intake}
    colorForLayer={colorForLayer} drawingKey="console:D" {...next} />
  const wrap = next => strict ? <StrictMode>{element(next)}</StrictMode> : element(next)
  const view = render(wrap(props))
  return { ...view, ref, update: next => view.rerender(wrap(next)) }
}
function mountHook(shown, extra = {}) {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  const ref = createRef()
  const input = inputOf(shown)
  const element = next => <Harness input={next} viewerRef={ref} intake={shown} {...extra} />
  const view = render(element(input))
  return { ...view, ref, input, update: next => view.rerender(element(next)) }
}
// App's wiring in miniature: the Viewer reports its own override, and the hook is told what is on the
// canvas (that override over the same base, else the base). `stamp` stands in for a remembered engine
// intake, the wiring this suite proves insufficient. `seen` keeps every render's decision.
function CanvasHarness({ input, viewerRef, intake, seen, stamp, ...props }) {
  const [override, setOverride] = useState(null)
  const onIntakeOverride = useCallback((value, base) => {
    setOverride(value == null ? null : { base, intake: value })
  }, [])
  const activeIntake = stamp === undefined ? solarOverlayCanvasIntake(override, intake) : stamp
  const { solarOverlay, reason } = useSolarGraphOverlay({ ...input, activeIntake })
  seen.push({ reason, bound: solarOverlay?.intake ?? null })
  return <Viewer ref={viewerRef} intake={intake} colorForLayer={colorForLayer}
    drawingKey="console:D" solarOverlay={solarOverlay} onIntakeOverride={onIntakeOverride} {...props} />
}
function mountCanvas(intake, input, extra = {}) {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  const ref = createRef(), seen = []
  const element = (nextInput, nextIntake, more) => <CanvasHarness input={nextInput} viewerRef={ref}
    intake={nextIntake} seen={seen} {...more} />
  const view = render(element(input, intake, extra))
  return { ...view, ref, seen,
    update: (nextInput, nextIntake = intake, more = extra) => view.rerender(element(nextInput, nextIntake, more)) }
}
const scene = () => gl.renderers.at(-1).scene
function reaches(group, target) {
  for (let node = group; node; node = node.parent) if (node === target) return true
  return false
}
const attached = () => gl.layers.filter(layer => reaches(layer.group, scene()))
const vertices = layer => layer.group.children.map(mesh => mesh.geometry.attributes.position.count)
function disposalSpies(layer) {
  return layer.group.children.flatMap(mesh => [
    vi.spyOn(mesh.geometry, 'dispose'), vi.spyOn(mesh.material, 'dispose'),
  ])
}
function disposedOnce(spies) { for (const spy of spies) expect(spy).toHaveBeenCalledTimes(1) }
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  gl.renderers.length = 0
  gl.controls.length = 0
  gl.layers.length = 0
  gl.raycasts.length = 0
})

describe('stored Solar graph in real Viewer', () => {
  it('A1B2-V01 roof and Ground attach to the matching scene', () => {
    const shown = intakeOf(roof())
    const view = mountViewer(shown, { solarOverlay: overlayOf(shown) })
    expect(attached()).toHaveLength(1)
    expect(vertices(attached()[0])).toEqual([8, 8, 4])
    const next = intakeOf(ground())
    act(() => view.ref.current.applyVersion(next))
    view.update({ solarOverlay: overlayOf(next) })
    expect(attached()).toHaveLength(1)
    expect(vertices(attached()[0])).toEqual([2])
    expect(Array.from(attached()[0].group.children[0].geometry.attributes.position.array))
      .toEqual([1, -1, 1.5, .5, -.5, 1.5])
  })

  it('A1B2-V02 absent and empty projections attach nothing', () => {
    const shown = intakeOf(roof())
    const view = mountViewer(shown)
    expect(gl.layers).toHaveLength(0)
    view.update({ solarOverlay: null })
    view.update({ solarOverlay: { ...overlayOf(shown), polylines: [] } })
    expect(gl.layers).toHaveLength(0)
    view.update({ solarOverlay: overlayOf(shown) })
    const old = attached()[0], spies = disposalSpies(old)
    view.update({ solarOverlay: { ...overlayOf(shown), polylines: [] } })
    expect(attached()).toHaveLength(0)
    disposedOnce(spies)
  })

  it('A1B2-V03 refusal and refresh remove then restore', () => {
    const shown = intakeOf(roof()), view = mountHook(shown)
    const first = attached()[0], spies = disposalSpies(first)
    view.update({ ...view.input, refreshPending: true })
    expect(attached()).toHaveLength(0)
    disposedOnce(spies)
    view.update(view.input)
    expect(attached()).toHaveLength(1)
    for (const graph of [{ ...roof(), graph_schema_version: 2 },
      (() => { const g = roof(); delete g.project.units.meters_per_unit; return g })(),
      lines(4097), points(100001)]) {
      const refused = intakeOf(graph)
      act(() => view.ref.current.applyVersion(refused))
      view.update({ ...view.input, shown: refused })
      expect(attached()).toHaveLength(0)
      act(() => view.ref.current.applyVersion(shown))
      view.update(view.input)
      expect(attached()).toHaveLength(1)
    }
  })

  it('A1B2-V04 intake and key mismatch each refuse', () => {
    const shown = intakeOf(roof()), overlay = overlayOf(shown)
    const view = mountViewer(shown, { solarOverlay: { ...overlay, intake: { ...shown } } })
    expect(gl.layers).toHaveLength(0)
    view.update({ solarOverlay: { ...overlay, drawingKey: 'console:old' } })
    expect(gl.layers).toHaveLength(0)
    view.update({ solarOverlay: overlay })
    expect(attached()).toHaveLength(1)
    view.update({ solarOverlay: { ...overlay, intake: { ...shown } } })
    expect(attached()).toHaveLength(0)
  })

  it('A1B2-V05 rebuild attaches only to the fresh scene', () => {
    const shown = intakeOf(roof()), overlay = overlayOf(shown)
    const view = mountViewer(shown, { solarOverlay: overlay })
    const oldScene = scene(), old = attached()[0], spies = disposalSpies(old)
    view.update({ solarOverlay: overlay, background: '#111111' })
    expect(scene()).not.toBe(oldScene)
    expect(attached()).toHaveLength(1)
    expect(reaches(old.group, scene())).toBe(false)
    disposedOnce(spies)
    const next = intakeOf(ground())
    act(() => view.ref.current.applyVersion(next))
    const builds = gl.layers.length
    expect(attached()).toHaveLength(0)
    view.update({ solarOverlay: overlayOf(next), background: '#111111' })
    expect(gl.layers).toHaveLength(builds + 1)
    expect(attached()).toHaveLength(1)
  })

  it('A1B2-V06 version replacement never appends', () => {
    const first = intakeOf(roof()), view = mountHook(first)
    for (const shown of [intakeOf(G()), intakeOf(ground()), intakeOf(), intakeOf(roof())]) {
      const old = attached()[0], spies = old ? disposalSpies(old) : []
      act(() => view.ref.current.applyVersion(shown))
      view.update({ ...view.input, shown })
      expect(attached()).toHaveLength(shown.solar_design_graph?.strings.length ? 1 : 0)
      disposedOnce(spies)
      if (old) expect(old.group.parent).toBeNull()
    }
    view.update({ ...view.input, shown: first })
    expect(attached()).toHaveLength(0)
  })

  it('A1B2-V07 owned resources dispose once', () => {
    const shown = intakeOf(roof()), view = mountViewer(shown, { solarOverlay: overlayOf(shown) }, true)
    const old = attached()[0], oldSpies = disposalSpies(old)
    const sibling = new THREE.Group()
    scene().add(sibling)
    view.update({ solarOverlay: overlayOf(shown) })
    disposedOnce(oldSpies)
    expect(sibling.parent).toBe(scene())
    const replaced = attached()[0], replacedSpies = disposalSpies(replaced)
    view.update({ solarOverlay: overlayOf(shown), background: '#222222' })
    disposedOnce(replacedSpies)
    const current = attached()[0], currentSpies = disposalSpies(current)
    expect(current).not.toBe(replaced)
    view.unmount()
    disposedOnce(currentSpies)
    disposedOnce(oldSpies)
    disposedOnce(replacedSpies)
    expect(gl.renderers.every(renderer => renderer.disposed)).toBe(true)
  })

  it('A1B2-V08 engine head binds exact engine scene', () => {
    const shown = intakeOf(roof()), view = mountHook(shown)
    const engine = { ...intakeOf(), source: 'engine', documentId: 'D-v1.dxf' }
    act(() => view.ref.current.applyVersion(engine))
    const input = { ...view.input, activeIntake: engine,
      engineDocument: { documentId: 'D-v1.dxf', documentOrigin: 'head', committedVersion: 7, entityCount: 0 } }
    view.update(input)
    expect(attached()).toHaveLength(1)
    for (const patch of [
      { engineDirty: true },
      { engineDocument: { ...input.engineDocument, documentOrigin: 'import' } },
      { engineDocument: { ...input.engineDocument, documentOrigin: 'starter' } },
      { activeVersion: 6 },
      { requestedDrawingId: 'old' },
      { activeIntake: { ...engine } },
    ]) {
      view.update({ ...input, ...patch })
      expect(attached()).toHaveLength(0)
      view.update(input)
      expect(attached()).toHaveLength(1)
    }
    expect(scene()).toBe(gl.renderers.at(-1).scene)
  })

  it('A1B2-V09 leaving Solar clears and returning restores', () => {
    const view = mountHook(intakeOf(roof()))
    const renderer = gl.renderers.at(-1), controls = gl.controls.at(-1)
    view.update({ ...view.input, enabled: false })
    expect(attached()).toHaveLength(0)
    view.update(view.input)
    expect(attached()).toHaveLength(1)
    expect(gl.renderers.at(-1)).toBe(renderer)
    expect(gl.controls.at(-1)).toBe(controls)
  })

  it('A1B2-V10 fresh wrapper does not rebuild', () => {
    const shown = intakeOf(roof()), overlay = overlayOf(shown)
    const view = mountViewer(shown, { solarOverlay: overlay })
    const layer = attached()[0], builds = gl.layers.length
    view.update({ solarOverlay: { ...overlay } })
    expect(attached()[0]).toBe(layer)
    expect(gl.layers).toHaveLength(builds)
    const next = { ...shown }
    view.update({ solarOverlay: { ...overlay, intake: next } })
    expect(attached()).toHaveLength(0)
    expect(gl.layers).toHaveLength(builds)
    act(() => view.ref.current.applyVersion(next))
    expect(attached()).toHaveLength(1)
  })

  it('A1B2-V11 result overlays survive Solar removal', () => {
    const shown = intakeOf(roof())
    const result = [{ pts: [[0, 0], [1, 1]], color: '#ffffff' }]
    const props = { solarOverlay: overlayOf(shown), overlayPolylines: result,
      highlightHandles: ['A'], markers: [{ pt: [2, 2] }] }
    const view = mountViewer(shown, props)
    const results = []
    scene().traverse(object => { if (object.renderOrder === 9) results.push(object) })
    expect(results.length).toBeGreaterThan(0)
    const retained = []
    scene().traverse(object => {
      if (object.renderOrder === 12 || object.renderOrder === 11) retained.push(object)
    })
    expect(retained.length).toBeGreaterThan(0)
    view.update({ ...props, solarOverlay: null })
    expect(attached()).toHaveLength(0)
    for (const object of results) expect(reaches(object, scene())).toBe(true)
    for (const object of retained) expect(reaches(object, scene())).toBe(true)
  })

  it('A1B2-V12 Solar lines are never pick inputs', () => {
    vi.stubGlobal('PointerEvent', MouseEvent)
    const shown = intakeOf(roof()), snapshot = JSON.stringify(shown)
    const view = mountViewer(shown, { solarOverlay: overlayOf(shown) })
    const solar = attached()[0].group, outer = solar.parent
    expect(outer).toBeTruthy()
    expect(outer).not.toBe(scene())
    expect(reaches(outer, scene())).toBe(true)
    vi.spyOn(THREE.Raycaster.prototype, 'intersectObjects').mockImplementation(objects => {
      gl.raycasts.push(objects.slice())
      return []
    })
    const dom = gl.renderers.at(-1).domElement
    vi.spyOn(dom, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 100, height: 100 })
    fireEvent.pointerDown(dom, { clientX: 5, clientY: 5, button: 0, pointerId: 1 })
    fireEvent.pointerUp(dom, { clientX: 5, clientY: 5, button: 0, pointerId: 1 })
    expect(gl.raycasts.length).toBeGreaterThan(0)
    for (const objects of gl.raycasts)
      for (const object of objects) {
        // Not the Solar container nor anything inside it, and not a parent the ray could descend from.
        expect(reaches(object, outer)).toBe(false)
        expect(reaches(outer, object)).toBe(false)
      }
    expect(JSON.stringify(shown)).toBe(snapshot)
    expect(view.ref.current).toBeTruthy()
  })

  it('A1B2-V13 phone and desktop admit alike', () => {
    const original = window.innerWidth
    try {
      for (const width of [390, 1600]) {
        Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
        act(() => window.dispatchEvent(new Event('resize')))
        const view = mountHook(intakeOf(roof()))
        expect(attached()).toHaveLength(1)
        view.update({ ...view.input, sceneCurrent: false })
        expect(attached()).toHaveLength(0)
        view.update(view.input)
        expect(attached()).toHaveLength(1)
        view.unmount()
      }
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: original })
    }
  })

  it('A1B2-V14 admitted boundaries keep measured batches', () => {
    const five = roof()
    five.inverters.push(I(2, [5, 5], 'combiner_box'))
    five.routes.push(R(2, 'feeder', [[0, 0], [1, 1]]))
    for (const [graph, counts] of [[lines(4096), [8192]], [points(100000), [199996]],
      [five, [8, 8, 8, 4, 2]]]) {
      const shown = intakeOf(graph), view = mountHook(shown)
      expect(attached()).toHaveLength(1)
      const layer = attached()[0]
      expect(vertices(layer)).toEqual(counts)
      expect(layer.group.children.map(mesh => '#' + mesh.material.color.getHexString()))
        .toEqual(counts.length === 5
          ? ['#22c55e', '#f59e0b', '#a855f7', '#38bdf8', '#f472b6'] : ['#f472b6'])
      expect(layer.group.children.length).toBeLessThanOrEqual(5)
      for (const mesh of layer.group.children) {
        expect(mesh.renderOrder).toBe(8)
        expect(mesh.material.depthTest).toBe(false)
        expect(mesh.material.depthWrite).toBe(false)
        expect(mesh.position.toArray()).toEqual([0, 0, 0])
        expect(mesh.scale.toArray()).toEqual([1, 1, 1])
      }
      view.unmount()
    }
  }, 30000)

  it('A1B2-V15 a version replacement over a retained engine head follows the canvas', () => {
    const head = intakeOf(roof())
    const engine = { ...intakeOf(), source: 'engine', documentId: 'D-v7.dxf' }
    const input = { ...inputOf(head),
      engineDocument: { documentId: 'D-v7.dxf', documentOrigin: 'head', committedVersion: 7, entityCount: 0 } }
    const view = mountCanvas(head, input)
    // The engine view applies its document once and keeps it: nothing below applies it again.
    act(() => view.ref.current.applyVersion(engine))
    expect(view.ref.current.getDrawingScene().intake).toBe(engine)
    expect(view.seen.at(-1)).toEqual({ reason: null, bound: engine })
    expect(attached()).toHaveLength(1)
    expect(vertices(attached()[0])).toEqual([8, 8, 4])
    // The version controller previews another version: it hands the Viewer the fetched intake.
    const preview = intakeOf(ground())
    act(() => {
      view.ref.current.applyVersion(preview)
      view.update({ ...input, shown: preview, activeVersion: 6 })
    })
    expect(view.ref.current.getDrawingScene().intake).toBe(preview)
    expect(view.seen.at(-1)).toEqual({ reason: null, bound: preview })
    expect(attached()).toHaveLength(1)
    expect(vertices(attached()[0])).toEqual([2])
    // Back to head: the controller hands over the head it fetched, a fresh object.
    const fetched = intakeOf(roof())
    act(() => {
      view.ref.current.applyVersion(fetched)
      view.update({ ...input, shown: fetched, activeVersion: 7 })
    })
    expect(view.ref.current.getDrawingScene().intake).toBe(fetched)
    expect(view.seen.at(-1)).toEqual({ reason: null, bound: fetched })
    expect(attached()).toHaveLength(1)
    expect(vertices(attached()[0])).toEqual([8, 8, 4])
    // A later render with nothing changed keeps the lines, and no render in the sequence refused.
    view.update({ ...input, shown: fetched, activeVersion: 7 })
    expect(attached()).toHaveLength(1)
    expect(view.seen.every(entry => entry.reason === null)).toBe(true)
    expect(view.seen.every(entry => entry.bound !== null)).toBe(true)
  })

  it('A1B2-V16 a remembered engine intake cannot stand in for the canvas', () => {
    const head = intakeOf(roof())
    const engine = { ...intakeOf(), source: 'engine', documentId: 'D-v7.dxf' }
    const input = { ...inputOf(head),
      engineDocument: { documentId: 'D-v7.dxf', documentOrigin: 'head', committedVersion: 7, entityCount: 0 } }
    const view = mountCanvas(head, input, { stamp: null })
    act(() => {
      view.ref.current.applyVersion(engine)
      view.update(input, head, { stamp: engine })
    })
    expect(attached()).toHaveLength(1)
    const preview = intakeOf(ground())
    act(() => {
      view.ref.current.applyVersion(preview)
      view.update({ ...input, shown: preview, activeVersion: 6 }, head, { stamp: engine })
    })
    expect(view.seen.at(-1)).toEqual({ reason: 'drawing_mismatch', bound: null })
    expect(attached()).toHaveLength(0)
    const fetched = intakeOf(roof())
    act(() => {
      view.ref.current.applyVersion(fetched)
      view.update({ ...input, shown: fetched, activeVersion: 7 }, head, { stamp: engine })
    })
    // The hook admits the scene it was told about, the Viewer shows another, and nothing says so.
    expect(view.ref.current.getDrawingScene().intake).toBe(fetched)
    expect(view.seen.at(-1)).toEqual({ reason: null, bound: engine })
    expect(attached()).toHaveLength(0)
  })

  it('A1B2-V17 the Viewer reports every override it sets or drops', () => {
    const base = intakeOf(roof()), next = intakeOf(ground())
    const engine = { ...intakeOf(), source: 'engine', documentId: 'D-v7.dxf' }
    const calls = []
    const onIntakeOverride = (value, over) => calls.push([
      value === engine ? 'engine' : value, over === base ? 'base' : over === next ? 'next' : over])
    const view = mountViewer(base, { onIntakeOverride })
    expect(calls).toEqual([[null, 'base']])
    act(() => view.ref.current.applyVersion(engine))
    expect(view.ref.current.getDrawingScene().intake).toBe(engine)
    act(() => view.ref.current.applyVersion(null))
    expect(view.ref.current.getDrawingScene().intake).toBe(base)
    act(() => view.ref.current.applyVersion(undefined))
    act(() => view.ref.current.applyVersion(engine))
    expect(calls).toEqual([[null, 'base'], ['engine', 'base'], [null, 'base'], [null, 'base'], ['engine', 'base']])
    // A new base drops the override, says so, and later overrides are reported over the new base.
    view.update({ onIntakeOverride, intake: next })
    expect(view.ref.current.getDrawingScene().intake).toBe(next)
    expect(calls.at(-1)).toEqual([null, 'next'])
    act(() => view.ref.current.applyVersion(engine))
    expect(calls.at(-1)).toEqual(['engine', 'next'])
    expect(calls).toHaveLength(7)
    // Without a listener the Viewer behaves as before.
    const quiet = mountViewer(base)
    act(() => quiet.ref.current.applyVersion(engine))
    expect(quiet.ref.current.getDrawingScene().intake).toBe(engine)
  })

  it('A1B2-V18 a new base drops a stale override without a mismatch render', () => {
    const first = intakeOf(roof()), preview = intakeOf(ground()), next = intakeOf(roof())
    const view = mountCanvas(first, inputOf(first))
    act(() => {
      view.ref.current.applyVersion(preview)
      view.update(inputOf(preview))
    })
    expect(view.seen.at(-1)).toEqual({ reason: null, bound: preview })
    expect(attached()).toHaveLength(1)
    // The drawing is re-seated: base and shown change together, and the Viewer drops its override one
    // commit later. The render in between still holds the stale override and must not refuse.
    view.seen.length = 0
    view.update(inputOf(next), next)
    expect(view.ref.current.getDrawingScene().intake).toBe(next)
    expect(view.seen.length).toBeGreaterThan(1)
    expect(view.seen.every(entry => entry.reason === null && entry.bound === next)).toBe(true)
    expect(attached()).toHaveLength(1)
    expect(vertices(attached()[0])).toEqual([8, 8, 4])
    // Re-seating the first object again must not revive the override it once carried.
    view.seen.length = 0
    view.update(inputOf(first), first)
    expect(view.ref.current.getDrawingScene().intake).toBe(first)
    expect(view.seen.every(entry => entry.reason === null && entry.bound === first)).toBe(true)
    expect(attached()).toHaveLength(1)
    // The rule itself.
    expect(solarOverlayCanvasIntake({ base: first, intake: preview }, first)).toBe(preview)
    expect(solarOverlayCanvasIntake({ base: first, intake: preview }, next)).toBe(next)
    expect(solarOverlayCanvasIntake({ base: next, intake: null }, next)).toBe(next)
    expect(solarOverlayCanvasIntake(null, next)).toBe(next)
    expect(solarOverlayCanvasIntake(undefined, undefined)).toBeNull()
  })
})
