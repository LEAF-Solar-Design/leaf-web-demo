import { createRef } from 'react'
import { act, cleanup, render } from '@testing-library/react'
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
