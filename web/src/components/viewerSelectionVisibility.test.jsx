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

function mountProject(initialHandle) {
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(800)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(600)
  const intake = { polylines: [panel('A', 'Roof', 0), panel('B', 'Panels', 10), panel('C', 'Roof', 20)],
    inserts: [], faces3d: [] }
  const ref = createRef()
  function Project({ layers }) {
    const [selectedHandle, setSelectedHandle] = useState(initialHandle)
    return <Viewer ref={ref} intake={intake} colorForLayer={colorForLayer} visibleLayers={layers}
      selectedHandle={selectedHandle} marqueeGate={() => true}
      onMarqueeSelect={projectCallback(true, { current: null }, ref, setSelectedHandle)} />
  }
  const view = render(<Project />)
  const canvas = view.container.querySelector('canvas')
  vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 800, height: 600 })
  return {
    identity: view.container.firstChild.dataset,
    layers: layers => view.rerender(<Project layers={layers} />),
    drag(left, right, modifiers = {}) {
      const start = ref.current.project(left, 3)
      const end = ref.current.project(right, -1)
      for (const [type, point] of [['pointerdown', start], ['pointermove', end], ['pointerup', end]]) {
        const event = new MouseEvent(type, { bubbles: true, clientX: point.x, clientY: point.y, ...modifiers })
        Object.defineProperty(event, 'pointerId', { value: 1 })
        fireEvent(canvas, event)
      }
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
