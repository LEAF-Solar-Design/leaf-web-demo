import { createRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import * as THREE from 'three'

const gl = vi.hoisted(() => ({ renderers: [], observers: [], frames: new Map(), id: 0,
  width: 800, height: 600, reduced: true, now: 1000 }))
vi.mock('three', async importOriginal => {
  const actual = await importOriginal()
  class WebGLRenderer {
    constructor() {
      this.domElement = document.createElement('canvas')
      this.domElement.getBoundingClientRect = () => ({ left: 11, top: 17, width: gl.width, height: gl.height })
      gl.renderers.push(this)
    }
    setPixelRatio() {}
    setSize() {}
    render(scene, camera) {
      scene.updateMatrixWorld(true)
      camera.updateMatrixWorld(true)
      this.scene = scene; this.camera = camera
    }
    dispose() {}
  }
  return { ...actual, WebGLRenderer }
})
vi.mock('three/examples/jsm/controls/OrbitControls.js', async () => {
  const actual = await vi.importActual('three')
  class OrbitControls {
    constructor(camera) { this.camera = camera; this.target = new actual.Vector3() }
    addEventListener() {}
    removeEventListener() {}
    update() { this.camera.lookAt(this.target); this.camera.updateMatrixWorld(true) }
    dispose() {}
  }
  return { OrbitControls }
})

import Viewer from './Viewer.jsx'
import { editFixture } from '../mock/editFixture.js'
import { bridgeToIntake } from '../localCad/bridgeIntake.js'

const colorForLayer = () => '#888888'
const O = 1e9
const pt = (x, y, z = 99, offset = O) => [offset + x, -offset + y, z]
function drawing(offset = O, source) {
  const p = (x, y) => pt(x, y, 99, offset)
  return { source, documentId: 'D', bounds: { minX: offset, maxX: offset + 0.08,
    minY: -offset, maxY: -offset + 0.08 },
  polylines: [{ handle: 'A', layer: 'Panels', closed: true,
    pts: [p(0, 0), p(0.01, 0), p(0.01, 0.01), p(0, 0.01)] },
  { handle: 'B', layer: 'Lines', closed: false, strokeOnly: true, pts: [p(0.03, 0), p(0.03, 0.01)] }],
  inserts: [{ handle: 'I', layer: 'Blocks', name: 'glyph', pt: p(0.07, 0.07), rot: 30, scale: [1.7, 0.8, 3] }],
  faces3d: [{ handle: 'F', layer: 'Faces', p1: p(0.04, 0.04), p2: p(0.05, 0.04),
    p3: p(0.05, 0.05), p4: p(0.04, 0.05) }] }
}
function freeze(value) {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value) }
  return value
}
beforeEach(() => {
  gl.width = 800; gl.height = 600; gl.reduced = true; gl.now = 1000
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => gl.width)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(() => gl.height)
  vi.spyOn(performance, 'now').mockImplementation(() => gl.now)
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback) { this.callback = callback; gl.observers.push(this) }
    observe() {}
    disconnect() {}
  })
  vi.stubGlobal('requestAnimationFrame', callback => { const id = ++gl.id; gl.frames.set(id, callback); return id })
  vi.stubGlobal('cancelAnimationFrame', id => gl.frames.delete(id))
  vi.stubGlobal('matchMedia', () => ({ matches: gl.reduced }))
})
afterEach(() => {
  cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals()
  gl.renderers.length = 0; gl.observers.length = 0; gl.frames.clear()
})
function frame() {
  act(() => {
    const callbacks = [...gl.frames.values()]; gl.frames.clear()
    callbacks.forEach(callback => callback(gl.now))
  })
}
function mount(intake, props = {}) {
  const ref = createRef()
  let current = { intake, drawingKey: 'drawing', ...props }
  const element = () => <Viewer ref={ref} colorForLayer={colorForLayer} {...current} />
  const view = render(element())
  return { ...view, ref, update(next) { current = { ...current, ...next }; view.rerender(element()) } }
}
const renderer = () => gl.renderers.at(-1)
function objects(predicate) {
  const found = []
  renderer().scene.traverse(object => { if (predicate(object)) found.push(object) })
  return found
}
const order = n => objects(object => object.renderOrder === n && object.geometry)
const polyLines = () => objects(object => object.userData.kind === 'blocklines')[0]
const glyph = () => objects(object => object.userData.handle === 'I')[0]
function vertex(object, index = 0) {
  renderer().scene.updateMatrixWorld(true)
  return new THREE.Vector3().fromBufferAttribute(object.geometry.attributes.position, index).applyMatrix4(object.matrixWorld)
}
function sameXY(actual, expected) {
  expect(actual.x).toBeCloseTo(expected[0], 6)
  expect(actual.y).toBeCloseTo(expected[1], 6)
}
// WebGL uploads model-view and projection matrices as Float32. Exercise that
// path as well as world transforms, so a double-only reconstruction cannot pass.
function renderedPixel(object, index) {
  const camera = renderer().camera
  renderer().scene.updateMatrixWorld(true); camera.updateMatrixWorld(true)
  const modelView = new THREE.Matrix4().multiplyMatrices(camera.matrixWorldInverse, object.matrixWorld)
  modelView.fromArray(new Float32Array(modelView.elements))
  const projection = new THREE.Matrix4().fromArray(new Float32Array(camera.projectionMatrix.elements))
  const v = new THREE.Vector3().fromBufferAttribute(object.geometry.attributes.position, index)
    .applyMatrix4(modelView).applyMatrix4(projection)
  return { x: 11 + (v.x + 1) * gl.width / 2, y: 17 + (1 - v.y) * gl.height / 2 }
}
function pointer(canvas, type, point) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX: point.x, clientY: point.y })
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: 'mouse' } })
  fireEvent(canvas, event)
}
function click(view, point) {
  const canvas = view.container.querySelector('canvas')
  pointer(canvas, 'pointerdown', point); pointer(canvas, 'pointerup', point)
}
function marquee(view, a, b) {
  const canvas = view.container.querySelector('canvas')
  pointer(canvas, 'pointerdown', a); pointer(canvas, 'pointermove', b); pointer(canvas, 'pointerup', b)
}

describe('Viewer local XY origin', () => {
  it('keeps 0.01-unit detail in actual buffers and rendered pixels through pan, zoom and resize', () => {
    const intake = freeze(drawing()), before = JSON.stringify(intake)
    const view = mount(intake)
    const lines = polyLines(), attribute = lines.geometry.attributes.position
    expect(attribute.array).toBeInstanceOf(Float32Array)
    expect(attribute.getX(1) - attribute.getX(0)).toBeCloseTo(0.01, 6)
    expect(lines.parent.parent.position.x).toBeGreaterThan(O - 1)
    expect(lines.parent.parent.position.z).toBe(0)
    const check = () => {
      frame()
      const a = renderedPixel(lines, 0), b = renderedPixel(lines, 1)
      expect(b.x - a.x).toBeGreaterThan(10)
      for (const [pixel, world] of [[a, intake.polylines[0].pts[0]], [b, intake.polylines[0].pts[1]]]) {
        const projected = view.ref.current.project(...world)
        expect(pixel.x).toBeCloseTo(projected.x, 2)
        expect(pixel.y).toBeCloseTo(projected.y, 2)
        sameXY(view.ref.current.unproject(projected.x, projected.y), world)
      }
      sameXY(vertex(lines), intake.polylines[0].pts[0])
      sameXY(glyph().getWorldPosition(new THREE.Vector3()), intake.inserts[0].pt)
      expect(view.ref.current.getPose().target[0]).toBeGreaterThan(O - 1)
    }
    check()
    act(() => view.ref.current.setView({ center: { x: O + 0.02, y: -O + 0.02 }, zoom: 1.3 }))
    check()
    gl.width = 1100; gl.height = 700
    act(() => gl.observers.at(-1).callback())
    check()
    expect(JSON.stringify(intake)).toBe(before)
    expect(view.ref.current.getDrawingScene().intake).toBe(intake)
  })

  it('uses real rays and WCS marquee coordinates before and after view changes, respecting hidden layers', () => {
    const select = vi.fn(), selected = vi.fn(), intake = drawing(O, 'engine')
    const view = mount(intake, { onSelectEntity: select, marqueeGate: () => true, onMarqueeSelect: selected })
    const check = () => {
      frame()
      for (const [handle, x, y] of [['A', 0.005, 0.005], ['B', 0.03, 0.005], ['I', 0.07, 0.07], ['F', 0.045, 0.045]]) {
        click(view, view.ref.current.project(O + x, -O + y))
        expect(select).toHaveBeenLastCalledWith(handle, { additive: false })
      }
      marquee(view, view.ref.current.project(O - 0.002, -O + 0.012), view.ref.current.project(O + 0.012, -O - 0.002))
      expect(selected.mock.calls.at(-1)[0]).toEqual(['A'])
    }
    check()
    act(() => view.ref.current.setView({ center: { x: O + 0.02, y: -O + 0.02 }, zoom: 1.2 }))
    gl.width = 1000; gl.height = 800
    act(() => gl.observers.at(-1).callback())
    check()
    view.update({ visibleLayers: { Panels: false, Blocks: false, Faces: false } })
    frame()
    for (const [x, y] of [[0.005, 0.005], [0.07, 0.07], [0.045, 0.045]]) {
      click(view, view.ref.current.project(O + x, -O + y))
      expect(select).toHaveBeenLastCalledWith(null, { additive: false })
    }
    marquee(view, view.ref.current.project(O - 0.002, -O + 0.012), view.ref.current.project(O + 0.012, -O - 0.002))
    expect(selected.mock.calls.at(-1)[0]).toEqual([])
  })

  it('localizes every overlay, face and insert while retaining the existing display Z values', () => {
    const intake = freeze(drawing()), pts = intake.polylines[0].pts
    const solar = freeze({ intake, drawingKey: 'drawing', polylines: [{ color: '#ffffff', pts }] })
    const view = mount(intake, { highlightHandles: ['A'], selectedHandle: 'A',
      markers: [{ pt: pts[0] }], overlayPolylines: [{ pts }],
      pendingEdit: { added: [{ pts, closed: true }], removed: ['A'] },
      stringRoutes: [{ id: 'route', pts }], solarOverlay: solar })
    const bounds = { minX: O, minY: -O, maxX: O + 0.01, maxY: -O + 0.01 }
    act(() => {
      view.ref.current.setRubberBand(pts, true)
      view.ref.current.setSnapMarker({ x: O, y: -O, kind: 'endpoint' }, 0.01)
      expect(view.ref.current.setFocusMarker(bounds)).toBe(true)
    })
    frame()
    const root = polyLines().parent.parent
    for (const object of objects(object => object.geometry)) {
      const positions = object.geometry.attributes.position
      expect(Array.from(positions.array).every(Number.isFinite)).toBe(true)
      for (let i = 0; i < positions.count; i++) {
        expect(Math.abs(positions.getX(i))).toBeLessThan(1)
        expect(Math.abs(positions.getY(i))).toBeLessThan(1)
      }
      let ancestor = object
      while (ancestor && ancestor !== root) ancestor = ancestor.parent
      expect(ancestor).toBe(root)
    }
    for (const n of [8, 9, 10, 11, 12, 14, 15, 20, 21]) expect(order(n).length).toBeGreaterThan(0)
    for (const n of [8, 9, 14, 15, 20, 21]) for (const object of order(n)) sameXY(vertex(object), pts[0])
    sameXY(vertex(order(10).find(object => object.isLineSegments)), pts[0])
    const snap = order(11).find(object => Math.abs(vertex(object).x - (O - 0.005)) < 1e-6)
    expect(snap).toBeDefined()
    sameXY(vertex(snap), [O - 0.005, -O - 0.005])
    const focus = order(11).find(object => object.isLine && !object.isLineSegments)
    sameXY(vertex(focus), pts[0])
    expect(vertex(focus, 1).x - vertex(focus).x).toBeCloseTo(0.01, 6)
    const displayZ = [[8, 1.5], [9, 1.5], [12, 2], [14, 4], [15, 3], [20, 5], [21, 5.1]]
    for (const [n, z] of displayZ) expect(vertex(order(n)[0]).z).toBeCloseTo(z, 5)
    expect(vertex(polyLines()).z).toBeCloseTo(0.05, 6)
    const face = objects(object => object.userData.handle === 'F')[0]
    sameXY(vertex(face), intake.faces3d[0].p1)
    expect(vertex(face).z).toBe(0.5)
    expect(glyph().position.z).toBe(2)
    view.update({ selectedHandle: 'I' })
    const outline = order(21)[0]
    sameXY(outline.getWorldPosition(new THREE.Vector3()), intake.inserts[0].pt)
    expect(outline.rotation.z).toBe(glyph().rotation.z)
    expect(outline.scale.toArray()).toEqual(glyph().scale.toArray())
    act(() => view.ref.current.setView({ center: { x: O + 0.02, y: -O + 0.02 }, zoom: 1.4 }))
    gl.width = 900; gl.height = 700
    act(() => gl.observers.at(-1).callback())
    frame()
    for (const object of objects(object => object.geometry && object.geometry.attributes.position.count)) {
      const world = vertex(object), a = renderedPixel(object, 0), b = view.ref.current.project(world.x, world.y)
      expect(a.x).toBeCloseTo(b.x, 2); expect(a.y).toBeCloseTo(b.y, 2)
    }
    expect(intake.bounds).toEqual(drawing().bounds)
    expect(intake.polylines[0].pts).toBe(pts)
    expect(solar.polylines[0].pts).toBe(pts)
  })

  it('keeps animated route vertices and the glowing head in the same local frame', () => {
    gl.reduced = false
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      createRadialGradient: () => ({ addColorStop() {} }), fillRect() {},
    })
    const intake = drawing(), pts = intake.polylines[0].pts.slice(0, 2)
    mount(intake, { stringRoutes: [{ id: 'animated', pts }] })
    gl.now += 0.000005
    frame()
    const line = order(14)[0], head = objects(object => object.isSprite)[0]
    expect(head.visible).toBe(true)
    expect(line.geometry.drawRange.count).toBe(2)
    const end = vertex(line, 1), center = head.getWorldPosition(new THREE.Vector3())
    expect(center.x).toBeCloseTo(end.x, 6); expect(center.y).toBeCloseTo(end.y, 6)
    expect(center.x).toBeGreaterThan(pts[0][0])
    expect(center.x).toBeLessThan(pts[1][0])
    expect(Math.abs(head.position.x)).toBeLessThan(1)
  })

  it('resets the frame on applyVersion and drawing switches without translating persisted bridge data', () => {
    const bridge = freeze(bridgeToIntake({ drawing: 'large.dwg', bounds: { minX: O, maxX: O + 0.01 },
      lines: [{ handle: 'A', layer: 'Panels', pts: [pt(0, 0), pt(0.01, 0)] }] }))
    const before = JSON.stringify(bridge), view = mount(bridge)
    sameXY(vertex(polyLines()), bridge.polylines[0].pts[0])
    expect(view.ref.current.getDrawingScene().intake).toBe(bridge)
    const firstRoot = polyLines().parent.parent
    const next = freeze(drawing(-O, 'engine'))
    act(() => view.ref.current.applyVersion(next))
    expect(polyLines().parent.parent).not.toBe(firstRoot)
    expect(polyLines().parent.parent.position.x).toBeLessThan(-O + 1)
    sameXY(vertex(polyLines()), next.polylines[0].pts[0])
    act(() => view.ref.current.setRubberBand(next.polylines[0].pts))
    const band = order(10).find(object => object.isLineSegments)
    sameXY(vertex(band), next.polylines[0].pts[0])
    view.update({ intake: editFixture, drawingKey: 'ordinary' })
    expect(view.ref.current.getDrawingScene().intake).toBe(editFixture)
    expect(Math.abs(polyLines().parent.parent.position.x)).toBeLessThan(1000)
    sameXY(vertex(polyLines()), editFixture.polylines[0].pts[0])
    view.update({ intake: { polylines: [], inserts: [], faces3d: [] }, drawingKey: 'empty' })
    expect(renderer().scene.children[0].position.toArray()).toEqual([0, 0, 0])
    expect(JSON.stringify(bridge)).toBe(before)
  })

  it.each([undefined, 'engine', 'bridge'])('preserves ordinary fixture fills, glyphs, faces and visibility for %s', source => {
    const intake = freeze({ ...editFixture, source, documentId: 'fixture' }), snapshot = JSON.stringify(intake)
    const view = mount(intake)
    const fills = objects(object => object.userData.kind === 'polyfill')
    expect(fills.reduce((count, object) => count + object.geometry.attributes.position.count, 0)).toBe(source === 'bridge' ? 0 : 18)
    sameXY(vertex(polyLines()), intake.polylines[0].pts[0])
    const insert = objects(object => object.userData.handle === 'INS2')[0]
    sameXY(insert.getWorldPosition(new THREE.Vector3()), intake.inserts[1].pt)
    expect(insert.rotation.z).toBeCloseTo(Math.PI / 6)
    expect(insert.scale.toArray()).toEqual([1.7, 1.7, 1])
    view.update({ visibleLayers: { Panels: false, Blocks: false, Surfaces: false } })
    expect(polyLines().parent.visible).toBe(false)
    expect(insert.parent.visible).toBe(false)
    expect(objects(object => object.userData.handle === 'F1')[0].parent.visible).toBe(false)
    expect(JSON.stringify(intake)).toBe(snapshot)
  })

  it('keeps empty, degenerate and rejected nonfinite XY geometry safe', () => {
    const intake = { polylines: [null, { pts: [] }, { pts: Array(2) }, { pts: [[NaN, 0], [1, 1]] },
      { pts: [[0, 0], [Infinity, 1]] }], inserts: [{ pt: [0, NaN] }],
    faces3d: [{ p1: [0, 0], p2: [1, 0], p3: [Infinity, 1] }] }
    const view = mount(intake)
    expect(renderer().scene.children[0].position.toArray()).toEqual([0, 0, 0])
    expect(objects(object => object.geometry)).toEqual([])
    expect(view.ref.current.getPose().position.every(Number.isFinite)).toBe(true)
    const degenerate = { polylines: [{ handle: 'A', layer: '0', pts: [pt(0, 0), pt(0, 0)] }] }
    act(() => view.ref.current.applyVersion(degenerate))
    expect(Array.from(polyLines().geometry.attributes.position.array).every(Number.isFinite)).toBe(true)
    sameXY(vertex(polyLines()), pt(0, 0))
    act(() => {
      view.ref.current.setRubberBand([[NaN, 0], [1, 1]])
      view.ref.current.setSnapMarker({ x: Infinity, y: 0, kind: 'endpoint' })
      expect(view.ref.current.setFocusMarker({ minX: NaN, minY: 0, maxX: 1, maxY: 1 })).toBe(false)
    })
    view.update({ overlayPolylines: [{ pts: [[NaN, 0], [0, Infinity]] }],
      markers: [{ pt: [Infinity, NaN] }], stringRoutes: [{ pts: [[0, 0], [NaN, 0]] }],
      solarOverlay: { intake: degenerate, drawingKey: 'drawing', polylines: [{ color: '#ffffff', pts: [[NaN, 0], [0, 1]] }] },
      pendingEdit: { added: [{ pts: [[NaN, 0], [0, Infinity]] }] } })
    expect(order(8)).toHaveLength(0)
    for (const object of objects(object => object.geometry))
      expect(Array.from(object.geometry.attributes.position.array).every(Number.isFinite)).toBe(true)
  })
})
