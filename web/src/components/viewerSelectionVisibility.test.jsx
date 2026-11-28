import { createRef, useState } from 'react'
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Viewer from './Viewer.jsx'

const gl = vi.hoisted(() => ({ scene: null }))
vi.mock('three', async importOriginal => {
  const actual = await importOriginal()
  class WebGLRenderer {
    domElement = document.createElement('canvas')
    setPixelRatio() {}
    setSize() {}
    render(scene) { gl.scene = scene }
    dispose() {}
  }
  return { ...actual, WebGLRenderer }
})
vi.mock('three/examples/jsm/controls/OrbitControls.js', async () => {
  const { Vector3 } = await vi.importActual('three')
  class OrbitControls {
    target = new Vector3()
    addEventListener() {}
    removeEventListener() {}
    update() {}
    dispose() {}
  }
  return { OrbitControls }
})

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  vi.stubGlobal('requestAnimationFrame', () => 1)
  vi.stubGlobal('cancelAnimationFrame', () => {})
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  gl.scene = null
})

function panel(handle, layer, x) {
  return { handle, layer, closed: true, pts: [[x, 0], [x + 2, 0], [x + 2, 2], [x, 2]] }
}

const colorForLayer = () => '#888888'
function mount(polylines, selectedHandle) {
  const ref = createRef()
  const intake = { polylines, inserts: [], faces3d: [] }
  const viewer = visibleLayers => <Viewer ref={ref} intake={intake} colorForLayer={colorForLayer}
    selectedHandle={selectedHandle} visibleLayers={visibleLayers} />
  const view = render(viewer())
  return {
    identity: view.container.firstChild.dataset,
    layers: visibleLayers => view.rerender(viewer(visibleLayers)),
    section: (handles, additive = false) => act(() => {
      ref.current.setHighlight(handles, { nativeHandles: true, additive })
    }),
  }
}

// Each fixture panel starts at a distinct world X. Check both its fill and
// outline so a stale outline, a missing fill, or a duplicate is observable.
function expectHighlights(...starts) {
  const fills = [], outlines = []
  gl.scene.updateMatrixWorld(true)
  gl.scene.traverseVisible(object => {
    if (object.renderOrder !== 20 && object.renderOrder !== 21) return
    const positions = object.geometry.attributes.position
    if (!positions.count) return
    const worldX = positions.getX(0) + object.matrixWorld.elements[12]
    ;(object.isMesh ? fills : outlines).push(worldX)
  })
  const numeric = (a, b) => a - b
  expect(fills.sort(numeric)).toEqual(starts)
  expect(outlines.sort(numeric)).toEqual(starts)
}

it('restores a selected panel when its layer is shown again', () => {
  const view = mount([panel('A', 'Panels', 0)], 'A')
  expectHighlights(0)
  view.layers({ Panels: false })
  expectHighlights()
  expect(view.identity.elementId).toBe('entity:A')
  view.layers({ Panels: true })
  expectHighlights(0)
})

it('preserves overlapping additive sections when only one layer is hidden', () => {
  const view = mount([panel('A', 'Roof', 0), panel('B', 'Panels', 10), panel('C', 'Roof', 20)])
  view.section(['A', 'B'])
  view.section(['B', 'C'], true)
  expectHighlights(0, 10, 20)
  view.layers({ Panels: false })
  expectHighlights(0, 20)
  expect(view.identity.groupHighlight).toBe('A B C')
  view.layers({})
  expectHighlights(0, 10, 20)
})

it('filters individual descriptors belonging to the same selected handle', () => {
  const view = mount([
    { ...panel('A1', 'Panels', 0), sourceHandle: 'A' },
    { ...panel('A2', 'Roof', 10), sourceHandle: 'A' },
  ], 'A')
  expectHighlights(0, 10)
  view.layers({ Panels: false })
  expectHighlights(10)
  expect(view.identity.elementId).toBe('entity:A')
  view.layers(undefined)
  expectHighlights(0, 10)
})

// Exercise App's project callback with real Viewer state, as in appProjectSolarDrawing.test.jsx.
const appSource = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
function findMarqueeProp(node) {
  if (node?.type === 'JSXAttribute' && node.name.name === 'onMarqueeSelect') return node.value.expression
  if (!node || typeof node !== 'object') return
  for (const child of Object.values(node)) {
    const found = findMarqueeProp(child)
    if (found) return found
  }
}
const marqueeProp = findMarqueeProp(parse(appSource, { sourceType: 'module', plugins: ['jsx'] }))
const projectCallback = Function('projectMode', 'workspaceCardRef', 'viewerRef', 'setSelectedHandle',
  `return (${appSource.slice(marqueeProp.start, marqueeProp.end)})`)

function mountProject(initialHandle, polylines = [panel('A', 'Roof', 0), panel('B', 'Panels', 10), panel('C', 'Roof', 20)]) {
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(800)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(600)
  const intake = { polylines, inserts: [], faces3d: [] }
  const selected = vi.fn()
  const ref = createRef()
  function Project({ layers }) {
    const [selectedHandle, setSelectedHandle] = useState(initialHandle)
    return <Viewer ref={ref} intake={intake} colorForLayer={colorForLayer} visibleLayers={layers}
      selectedHandle={selectedHandle} marqueeGate={() => true}
      onMarqueeSelect={(handles, gesture) => {
        selected(handles, gesture)
        projectCallback(true, { current: null }, ref, setSelectedHandle)(handles, gesture)
      }} />
  }
  const view = render(<Project />)
  const canvas = view.container.querySelector('canvas')
  vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 800, height: 600 })
  const dragBetween = (start, end, modifiers = {}) => {
    for (const [type, point] of [['pointerdown', start], ['pointermove', end], ['pointerup', end]]) {
      const event = new MouseEvent(type, { bubbles: true, ...modifiers })
      Object.defineProperties(event, {
        pointerId: { value: 1 }, clientX: { value: point.x }, clientY: { value: point.y },
      })
      fireEvent(canvas, event)
    }
  }
  return {
    api: ref.current,
    selected,
    identity: view.container.firstChild.dataset,
    layers: layers => view.rerender(<Project layers={layers} />),
    dragBetween,
    drag(left, right, modifiers = {}) {
      dragBetween(ref.current.project(left, 3), ref.current.project(right, -1), modifiers)
    },
  }
}

it('subtracts only the overlap from a mixed-layer group', () => {
  const view = mountProject()
  view.drag(-1, 13)
  expectHighlights(0, 10)
  view.drag(9, 23, { altKey: true })
  expect(view.identity.groupHighlight).toBe('A')
  expectHighlights(0)
})

it('removes a singly selected panel with Alt-drag', () => {
  const view = mountProject('B')
  expectHighlights(10)
  view.drag(9, 13, { altKey: true })
  expect(view.identity.elementId).toBeUndefined()
  expectHighlights()
})

it('retains hidden selections across disjoint and empty Alt-drags', () => {
  const view = mountProject()
  view.drag(-1, 13)
  view.layers({ Panels: false })
  expectHighlights(0)
  view.drag(19, 23, { altKey: true })
  expect(view.identity.groupHighlight).toBe('A B')
  expectHighlights(0)
  view.drag(9, 13, { altKey: true })
  expect(view.identity.groupHighlight).toBe('A B')
  expectHighlights(0)
  view.layers({})
  expectHighlights(0, 10)
})


// Archived rooftop I5 feeder polylines, stored in graph meters and displayed
// in original drawing inches. The designated Upper layer is the test toggle.
const retainedBends = [
  ['470', 'clockwise', -1, [
    [358.8147735056913, 51.970072721747684], [340.8939148597521, 51.970072721747684],
    [340.8939148597521, 41.7601430113689], [363.1844047071577, 41.7601430113689],
  ]],
  ['472', 'counterclockwise', -1, [
    [420.1944131994379, 46.45194027003977], [401.2507642576333, 46.45194027003977],
    [401.2507642576333, 40.032885816747914], [425.6622506987236, 40.032885816747914],
  ]],
  ['475', 'reset', 1, [
    [512.7755272439267, 55.79365337960151], [549.379032564767, 55.79365337960151],
    [549.379032564767, 51.45373050866659], [514.6256397266237, 51.45373050866659],
  ]],
]

it.each(retainedBends)('selects archived feeder %s after %s and a layer visibility cycle', (handle, direction, outside, points) => {
  const pts = points.map(point => point.map(meters => meters / 0.0254))
  const view = mountProject(undefined, [{ handle, layer: 'Upper', pts, closed: false }])
  const x = pts[1][0]
  const y = (pts[1][1] + pts[2][1]) / 2
  const halfHeight = Math.abs(pts[1][1] - pts[2][1]) / 4
  const distance = halfHeight * 2
  expect(view.api.setView({ center: { x, y }, zoom: 1.25 })).toBe(true)
  const pose = view.api.getPose()
  const dragAcross = (innerOffset = 0) => {
    const first = view.api.project(x + outside * distance, y - halfHeight)
    const second = view.api.project(x + outside * innerOffset, y + halfHeight)
    view.dragBetween({ x: Math.max(first.x, second.x), y: Math.max(first.y, second.y) },
      { x: Math.min(first.x, second.x), y: Math.min(first.y, second.y) })
    expect(view.selected).toHaveBeenLastCalledWith(expect.any(Array), { additive: false, mode: 'crossing' })
    return view.selected.mock.lastCall[0]
  }
  expect(dragAcross(-0.01)).toEqual([handle])
  view.layers({ Upper: false })
  if (direction === 'reset') {
    expect(view.api.rollDrawing('clockwise')).toBe(true)
    expect(view.api.rollDrawing('counterclockwise')).toBe(true)
    expect(view.api.rollDrawing('counterclockwise')).toBe(true)
  }
  expect(view.api.rollDrawing(direction)).toBe(true)
  expect(view.api.getPose()).toMatchObject({ target: pose.target, zoom: pose.zoom })
  expect(dragAcross(-0.01)).toEqual([])
  view.layers({})
  expect(dragAcross(-0.01)).toEqual([handle])
  expect(dragAcross(0.01)).toEqual([])
  expect(dragAcross()).toEqual([handle])
})

it('selects the retained St. Benedict setback corner on the crossing edge after reset', () => {
  // Native POLYLINE 1B9, flattened to the drawing plane for the Viewer.
  const pts = [
    [276535.2362204724, -92438.30708661416],
    [276535.6299212598, -92437.91338582675],
    [276535.0393700787, -92210.15748031494],
    [275123.779527559, -92209.2171276299],
    [275124.0551181102, -92439.92125984251],
    [275124.4488188976, -92440.3149606299],
  ]
  const [x, y] = pts[1]
  const handle = '1B9'
  const view = mountProject(undefined, [{ handle, layer: 'Upper', pts, closed: true }])
  expect(view.api.setView({ center: { x, y }, zoom: 1.25 })).toBe(true)
  const dragRightOfCorner = offset => {
    view.dragBetween(view.api.project(x + 40, y - 20), view.api.project(x + offset, y + 20))
    expect(view.selected).toHaveBeenLastCalledWith(expect.any(Array), { additive: false, mode: 'crossing' })
    return view.selected.mock.lastCall[0]
  }
  expect(dragRightOfCorner(0)).toEqual([handle])
  view.layers({ Upper: false })
  expect(view.api.rollDrawing('clockwise')).toBe(true)
  expect(view.api.rollDrawing('reset')).toBe(true)
  expect(dragRightOfCorner(0)).toEqual([])
  view.layers({})
  expect(dragRightOfCorner(0)).toEqual([handle])
  expect(dragRightOfCorner(0.01)).toEqual([])
})
