import { createRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import * as THREE from 'three'

const gl = vi.hoisted(() => ({ renderers: [], controls: [], frames: new Map(), id: 0 }))
vi.mock('three', async importOriginal => {
  const actual = await importOriginal()
  class WebGLRenderer {
    constructor() {
      this.domElement = document.createElement('canvas')
      this.domElement.getBoundingClientRect = () => ({ left: 0, top: 0, width: 800, height: 600 })
      gl.renderers.push(this)
    }
    setPixelRatio() {}
    setSize() {}
    render(scene, camera) {
      scene.updateMatrixWorld(true); camera.updateMatrixWorld(true)
      this.scene = scene; this.camera = camera
    }
    dispose() {}
  }
  return { ...actual, WebGLRenderer }
})
vi.mock('three/examples/jsm/controls/OrbitControls.js', async () => {
  const actual = await vi.importActual('three')
  class OrbitControls {
    constructor(camera) { this.camera = camera; this.target = new actual.Vector3(); gl.controls.push(this) }
    addEventListener() {}
    removeEventListener() {}
    update() { this.camera.lookAt(this.target); this.camera.updateMatrixWorld(true) }
    dispose() {}
  }
  return { OrbitControls }
})

import Viewer from './Viewer.jsx'
import { bridgeToIntake } from '../localCad/bridgeIntake.js'
import { expandBulgedPolylines, intakeRoundPolylines } from '../cadedit/engineIntake.js'
import { editFixture } from '../mock/editFixture.js'

const colorForLayer = () => '#888888'
beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(800)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(600)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  vi.stubGlobal('requestAnimationFrame', cb => { const id = ++gl.id; gl.frames.set(id, cb); return id })
  vi.stubGlobal('cancelAnimationFrame', id => gl.frames.delete(id))
  vi.stubGlobal('matchMedia', () => ({ matches: true }))
})
afterEach(() => {
  cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals()
  gl.renderers.length = 0; gl.controls.length = 0; gl.frames.clear()
})
function frame() {
  act(() => {
    const callbacks = [...gl.frames.values()]; gl.frames.clear()
    callbacks.forEach(cb => cb(1000))
  })
}
function mount(intake, props = {}) {
  const ref = createRef()
  let current = { intake, ...props }
  const element = () => <Viewer ref={ref} colorForLayer={colorForLayer} {...current} />
  const view = render(element())
  frame()
  return { ...view, ref, update(next) { current = { ...current, ...next }; view.rerender(element()); frame() } }
}
const renderer = () => gl.renderers.at(-1)
function objects(predicate) {
  const found = []
  renderer().scene.traverse(o => { if (predicate(o)) found.push(o) })
  return found
}
const strokes = () => objects(o => o.userData.kind === 'blocklines')
const overlay = n => objects(o => o.renderOrder === n && o.geometry)
function freeze(value) {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value) }
  return value
}
function expectedSegments(row, origin) {
  const out = []
  const n = row.closed ? row.pts.length : row.pts.length - 1
  for (let i = 0; i < n; i++) for (const p of [row.pts[i], row.pts[(i + 1) % row.pts.length]]) {
    out.push(Math.fround(p[0] - origin.x), Math.fround(p[1] - origin.y), Math.fround(Number.isFinite(p[2]) ? p[2] : 0))
  }
  return out
}
function assertBuffers(intake) {
  const rows = [...expandBulgedPolylines(intake.polylines), ...intakeRoundPolylines(intake)]
  const expectedLayers = new Map()
  for (const row of rows) {
    if (!expectedLayers.has(row.layer)) expectedLayers.set(row.layer, [])
    expectedLayers.get(row.layer).push(row)
  }
  const actualStrokes = strokes()
  expect(actualStrokes).toHaveLength(expectedLayers.size)
  const remainingLayers = new Set(expectedLayers.keys())
  for (const line of actualStrokes) {
    const origin = line.parent.parent.position
    const firstHandle = line.userData.lineHandles[0]
    const layer = rows.find(row => (row.sourceHandle ?? row.handle) === firstHandle)?.layer
    expect(expectedLayers.has(layer)).toBe(true)
    expect(remainingLayers.has(layer)).toBe(true)
    remainingLayers.delete(layer)
    const layerRows = expectedLayers.get(layer)
    const expectedHandles = layerRows.flatMap(row => Array(row.closed ? row.pts.length : row.pts.length - 1).fill(row.sourceHandle ?? row.handle))
    expect(line.geometry.attributes.position.count).toBe(expectedHandles.length * 2)
    expect(Array.from(line.geometry.attributes.position.array)).toEqual(layerRows.flatMap(row => expectedSegments(row, origin)))
    expect(line.userData.lineHandles).toEqual(expectedHandles)
  }
  expect([...remainingLayers]).toEqual([])
}
function pixel(point) {
  const p = new THREE.Vector3(...point).project(renderer().camera)
  return { x: (p.x + 1) * 400, y: (1 - p.y) * 300 }
}
function click(view, p) {
  for (const type of ['pointerdown', 'pointerup']) {
    const event = new MouseEvent(type, { bubbles: true, button: 0, clientX: p.x, clientY: p.y })
    Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: 'mouse' } })
    fireEvent(view.container.querySelector('canvas'), event)
  }
}

describe('Viewer bridge XYZ buffers', () => {
  it.each([false, true])('keeps every endpoint and the closing edge for closed=%s, with matching overlays', closed => {
    const intake = freeze(bridgeToIntake({ polylines: [{ handle: 'A', layer: 'Elevated', closed,
      pts: [[0, 0, -12], [10, 0, 32], [5, 8, 0]] }] }))
    const before = JSON.stringify(intake)
    const view = mount(intake, { highlightHandles: ['A'], selectedHandle: 'A' })
    assertBuffers(intake)
    const actual = Array.from(strokes()[0].geometry.attributes.position.array)
    expect(actual.filter((_, i) => i % 3 === 2)).toEqual(closed ? [-12, 32, 32, 0, 0, -12] : [-12, 32, 32, 0])
    for (const n of [11, 21]) expect(Array.from(overlay(n)[0].geometry.attributes.position.array)).toEqual(actual)
    for (const n of [10, 20]) expect(overlay(n)[0].geometry.attributes.position.count).toBe(0)
    expect(objects(o => o.userData.kind === 'polyfill')[0].geometry.attributes.position.count).toBe(0)
    expect(strokes()[0].renderOrder).toBe(1)
    expect(strokes()[0].material.depthWrite).toBe(false)
    view.update({ panelSculpture: true, rotateEnabled: true })
    assertBuffers(intake)
    for (const n of [11, 21]) expect(Array.from(overlay(n)[0].geometry.attributes.position.array)).toEqual(actual)
    expect(objects(o => o.userData.kind === 'polyfill')[0].geometry.attributes.position.count).toBe(0)
    expect(JSON.stringify(intake)).toBe(before)
  })

  it('keeps sloping lines, zero, missing and malformed Z in actual buffers without coercion', () => {
    const bad = [undefined, null, NaN, Infinity, -Infinity, '14', true, {}, [], Symbol('z')]
    const intake = freeze({ source: 'bridge', polylines: bad.map((z, i) => ({ handle: `B${i}`, layer: 'Raw',
      pts: [[i, 0, z], [i, 1, -i]], closed: false })) })
    mount(intake)
    assertBuffers(intake)
    const zs = Array.from(strokes()[0].geometry.attributes.position.array).filter((_, i) => i % 3 === 2)
    expect(zs).toEqual(bad.flatMap((_, i) => [0, -i]))
  })

  it('composes per-vertex Z with local XY rebasing before Float32 allocation', () => {
    const O = 1e9
    const intake = freeze(bridgeToIntake({ lines: [{ handle: 'Slope', layer: 'Detail',
      pts: [[O, -O, -7], [O + 0.01, -O + 0.02, 19]] }] }))
    const view = mount(intake, { selectedHandle: 'Slope', highlightHandles: ['Slope'] })
    assertBuffers(intake)
    const line = strokes()[0], attr = line.geometry.attributes.position
    expect(attr.array).toBeInstanceOf(Float32Array)
    expect(attr.getX(1) - attr.getX(0)).toBeCloseTo(0.01, 6)
    expect(attr.getY(1) - attr.getY(0)).toBeCloseTo(0.02, 6)
    expect([attr.getZ(0), attr.getZ(1)]).toEqual([-7, 19])
    expect(line.parent.parent.position.z).toBe(0)
    for (const n of [11, 21]) expect(Array.from(overlay(n)[0].geometry.attributes.position.array)).toEqual(Array.from(attr.array))
    expect(view.ref.current.getDrawingScene().intake).toBe(intake)
  })

  it('preserves sampled circles, arcs and planar or nonplanar bulges and their identities', () => {
    const intake = freeze(bridgeToIntake({
      circles: [{ handle: 'C', layer: 'Round', c: [20, 20, 81], r: 3 }],
      arcs: [{ handle: 'R', layer: 'Round', c: [30, 20, -9], r: 4, start_deg: 15, end_deg: 190 }],
      polylines: [
        { handle: 'P', layer: 'Bulged', closed: true, pts: [[0, 0, 13], [10, 0, 13], [5, 7, 13]], bulges: [1, 0, 0.5] },
        { handle: 'N', layer: 'Bulged', pts: [[0, 20, -4], [10, 20, 26]], bulges: [1, 0] },
      ],
    }))
    const before = JSON.stringify(intake)
    const view = mount(intake)
    assertBuffers(intake)
    for (const row of [...expandBulgedPolylines(intake.polylines), ...intakeRoundPolylines(intake)]) {
      view.update({ highlightHandles: [row.handle], selectedHandle: row.handle })
      const origin = strokes()[0].parent.parent.position
      for (const n of [11, 21]) expect(Array.from(overlay(n)[0].geometry.attributes.position.array)).toEqual(expectedSegments(row, origin))
    }
    expect(JSON.stringify(intake)).toBe(before)
  })

  it('retains sampled XYZ for tilted planar bulges and sourceHandle attribution', () => {
    const row = { handle: 'child', sourceHandle: 'parent', layer: 'Tilted', closed: false,
      pts: [[0, 0, 0], [10, 0, 0]], normal: [0, Math.SQRT1_2, Math.SQRT1_2], bulges: [1, 0] }
    const intake = freeze({ source: 'bridge', polylines: [row] })
    mount(intake, { selectedHandle: 'parent', highlightHandles: ['parent'] })
    assertBuffers(intake)
    const positions = Array.from(strokes()[0].geometry.attributes.position.array)
    expect(positions.filter((_, i) => i % 3 === 2).some(z => Math.abs(z) > 1)).toBe(true)
    for (const n of [11, 21]) expect(Array.from(overlay(n)[0].geometry.attributes.position.array)).toEqual(positions)
  })

  it.each([false, true])('picks a slope or closing edge with a real tilted-camera ray, closed=%s', closed => {
    const select = vi.fn()
    const intake = bridgeToIntake({ polylines: [{ handle: 'A', layer: 'Slope', closed,
      pts: closed ? [[-5, 0, -12], [5, 0, 18], [5, 5, 6]] : [[-5, 0, -12], [5, 0, 18]] }] })
    const view = mount(intake, { onSelectEntity: select, highlightHandles: ['A'], selectedHandle: 'A' })
    const controls = gl.controls.at(-1)
    const point = closed ? [0, 2.5, -3] : [0, 0, 3]
    controls.target.set(...point); renderer().camera.position.set(0, -60, 70); controls.update(); frame()
    const hit = pixel(point), flattened = pixel([point[0], point[1], 0.05])
    expect(Math.abs(hit.y - flattened.y)).toBeGreaterThan(20)
    click(view, hit)
    expect(select).toHaveBeenLastCalledWith('A', { additive: false })
    for (const n of [11, 21]) expect(Array.from(overlay(n)[0].geometry.attributes.position.array)).toEqual(Array.from(strokes()[0].geometry.attributes.position.array))
    view.update({ visibleLayers: { Slope: false } })
    expect(strokes()[0].parent.visible).toBe(false)
    click(view, hit)
    expect(select).toHaveBeenLastCalledWith(null, { additive: false })
  })

  it.each([[-1e6, -999990], [1e6, 1000010], [-1e6, 1e6]])('keeps elevation range %s to %s inside camera depth clipping', (a, b) => {
    const intake = bridgeToIntake({ lines: [{ handle: 'High', layer: 'Z', pts: [[0, 0, a], [10, 1, b]] }] })
    const view = mount(intake)
    const check = () => {
      frame()
      const camera = renderer().camera, line = strokes()[0], attr = line.geometry.attributes.position
      for (let i = 0; i < attr.count; i++) {
        const world = new THREE.Vector3().fromBufferAttribute(attr, i).applyMatrix4(line.matrixWorld)
        const projected = world.clone().project(camera)
        expect(projected.z).toBeGreaterThan(-1)
        expect(projected.z).toBeLessThan(1)
        expect(world.z).toBeLessThan(camera.position.z)
      }
    }
    check()
    act(() => view.ref.current.setView({ zoom: 1.4, center: { x: 4, y: 2 } }))
    check()
    act(() => view.ref.current.fit())
    check()
    const controls = gl.controls.at(-1)
    renderer().camera.position.x += 20; renderer().camera.position.y -= 20
    controls.update()
    check()
  })

  it('handles a large allowed polyline without argument-spread overflow', () => {
    const pts = Array.from({ length: 30000 }, (_, i) => [i, i % 2, i % 3 - 1])
    const intake = bridgeToIntake({ polylines: [{ handle: 'Many', layer: 'Dense', pts, closed: true }] })
    mount(intake, { highlightHandles: ['Many'], selectedHandle: 'Many' })
    expect(strokes()[0].geometry.attributes.position.count).toBe(60000)
    assertBuffers(intake)
    for (const n of [11, 21]) expect(overlay(n)[0].geometry.attributes.position.count).toBe(60000)
  })

  it('retains zero-elevation bridge wireframes and restores legacy display heights on source changes', () => {
    const bridge = freeze({ ...editFixture, source: 'bridge' })
    const view = mount(bridge, { highlightHandles: ['PL1'], selectedHandle: 'PL1' })
    for (const line of strokes()) expect(Array.from(line.geometry.attributes.position.array).filter((_, i) => i % 3 === 2).every(z => z === 0)).toBe(true)
    expect(objects(o => o.userData.kind === 'polyfill').every(o => o.geometry.attributes.position.count === 0)).toBe(true)
    view.update({ intake: editFixture })
    expect(strokes()[0].geometry.attributes.position.getZ(0)).toBeCloseTo(0.05)
    expect(overlay(11)[0].geometry.attributes.position.getZ(0)).toBeCloseTo(1.1)
    expect(overlay(21)[0].geometry.attributes.position.getZ(0)).toBeCloseTo(5.1)
  })

  it('rejects invalid XY without joining broken segments and keeps empty bridge scenes safe', () => {
    const view = mount({ source: 'bridge', polylines: [null, { pts: [] },
      { handle: 'Bad', pts: [[0, 0, 1], [Infinity, 2, 4]] }, { pts: [[NaN, 0], [1, 1]] }] })
    expect(strokes()).toEqual([])
    expect(view.ref.current.getPose().position.every(Number.isFinite)).toBe(true)
    const good = { handle: 'Good', layer: 'Valid', pts: [[-10, -10, -3], [-5, -5, 7]], closed: false }
    const broken = [
      { handle: 'OpenGap', layer: 'Broken', closed: false,
        pts: [[0, 0, 1], [Infinity, 2, 4], [4, 0, 9], [6, 2, 11]] },
      { handle: 'ClosedGap', layer: 'Broken', closed: true,
        pts: [[10, 0, 2], [12, NaN, 5], [14, 0, 8], [16, 2, 12]] },
      { handle: 'ClosedLast', layer: 'Broken', closed: true,
        pts: [[20, 0, 3], [22, 2, 6], [24, 0, 10], [undefined, 2, 14]] },
      { handle: 'ClosedFirst', layer: 'Broken', closed: true,
        pts: [[30, null, 4], [32, 2, 7], [34, 0, 11], [36, 2, 15]] },
    ]
    view.update({ intake: freeze({ source: 'bridge', polylines: [good, ...broken] }),
      highlightHandles: broken.map(row => row.handle), selectedHandle: 'ClosedLast' })
    // Reject each malformed row as a whole, including any otherwise valid edges.
    // The exact buffer and handle checks also forbid joining across its bad vertex.
    assertBuffers({ polylines: [good] })
    expect(strokes()[0].userData.lineHandles).toEqual(['Good'])
    expect(overlay(11)).toHaveLength(1)
    expect(overlay(11)[0].geometry.attributes.position.count).toBe(0)
    expect(overlay(21)).toEqual([])
    expect(view.ref.current.getPose().position.every(Number.isFinite)).toBe(true)
    view.update({ intake: { source: 'bridge', polylines: [] } })
    expect(strokes()).toEqual([])
  })

  it.each([undefined, 'engine'])('retains flat and sculpture fixture geometry for source %s', source => {
    const intake = freeze({ ...editFixture, source }), before = JSON.stringify(intake)
    const view = mount(intake, { selectedHandle: 'PL1', highlightHandles: ['PL1'] })
    const line = strokes()[0]
    expect(Array.from(line.geometry.attributes.position.array).filter((_, i) => i % 3 === 2).every(z => Math.abs(z - 0.05) < 1e-6)).toBe(true)
    expect(objects(o => o.userData.kind === 'polyfill').reduce((n, o) => n + o.geometry.attributes.position.count, 0)).toBe(18)
    expect(overlay(11)[0].geometry.attributes.position.getZ(0)).toBeCloseTo(1.1)
    expect(overlay(21)[0].geometry.attributes.position.getZ(0)).toBeCloseTo(5.1)
    view.update({ panelSculpture: true })
    // First panel centre (25,20), drawing bounds (10,10)-(120,82).
    const nx = (25 - 65) / 55, ny = (20 - 46) / 36
    const dome = Math.sqrt(1 - Math.min(1, nx * nx * 0.82 + ny * ny * 0.18))
    const top = 110 * 0.24 * Math.max(0.12, dome + 0.04 * Math.sin(25 * 0.48) * Math.cos(20 * 0.31))
    const attr = strokes()[0].geometry.attributes.position
    expect(attr.getZ(0)).toBeCloseTo(top - 110 * 0.006, 4)
    expect(attr.getZ(1)).toBeCloseTo(top, 4)
    expect(attr.getZ(8)).toBeCloseTo(top + 0.05, 4)
    expect(objects(o => o.userData.kind === 'polyfill').reduce((n, o) => n + o.geometry.attributes.position.count, 0)).toBe(108)
    expect(JSON.stringify(intake)).toBe(before)
  })
})
