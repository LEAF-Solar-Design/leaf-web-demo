import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/react'
import * as THREE from 'three'

const gl = vi.hoisted(() => ({ renderers: [], controls: [] }))
vi.mock('three', async (importOriginal) => {
  const actual = await importOriginal()
  class WebGLRenderer {
    constructor() {
      this.domElement = document.createElement('canvas')
      gl.renderers.push(this)
    }
    setPixelRatio() {}
    setSize() {}
    render(scene) { this.scene = scene }
    dispose() {}
  }
  return { ...actual, WebGLRenderer }
})
vi.mock('three/examples/jsm/controls/OrbitControls.js', async () => {
  const actual = await vi.importActual('three')
  class OrbitControls {
    constructor() {
      this.target = new actual.Vector3()
      this.enabled = true
      this.touches = { ONE: actual.TOUCH.ROTATE, TWO: actual.TOUCH.DOLLY_PAN }
      gl.controls.push(this)
    }
    addEventListener() {}
    removeEventListener() {}
    update() {}
    dispose() {}
  }
  return { OrbitControls }
})

import Viewer, { pickHandleFromHits, stackedPickOrder } from './Viewer.jsx'
import { editFixture } from '../mock/editFixture.js'

const colorForLayer = () => '#888888'
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  vi.stubGlobal('requestAnimationFrame', () => 1)
  vi.stubGlobal('cancelAnimationFrame', () => {})
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  gl.renderers.length = 0
  gl.controls.length = 0
})

function mountViewer(props = {}) {
  const element = next => <Viewer intake={editFixture} colorForLayer={colorForLayer} {...next} />
  const view = render(element(props))
  return { ...view, update: next => view.rerender(element(next)) }
}
function entity(handle) {
  let found
  gl.renderers.at(-1).scene.traverse(object => {
    if (object.userData.handle === handle) found = object
  })
  return found
}
function visible(object) {
  for (let node = object; node; node = node.parent) if (!node.visible) return false
  return true
}
function pointer(canvas, type, pointerType, pointerId, x = 10, isPrimary = true) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: 10 })
  Object.defineProperties(event, {
    pointerType: { value: pointerType }, pointerId: { value: pointerId }, isPrimary: { value: isPrimary },
  })
  fireEvent(canvas, event)
  return event
}

describe('viewer issue sweep', () => {
  it('hides block glyphs, surfaces and their outlines independently and removes them from picks', () => {
    const view = mountViewer()
    const block = entity('INS1'), face = entity('F1')
    expect(block).toBeDefined()
    expect(face).toBeDefined()
    const hits = [{ object: block }, { object: face }]
    view.update({ visibleLayers: { Blocks: false } })
    expect(visible(block)).toBe(false)
    expect(visible(block.children[0])).toBe(false)
    expect(visible(face)).toBe(true)
    expect(pickHandleFromHits(hits)).toBe('F1')
    expect(stackedPickOrder(hits)).toEqual(['F1'])
    view.update({ visibleLayers: { Surfaces: false } })
    expect(visible(block)).toBe(true)
    expect(visible(face)).toBe(false)
    expect(face.parent.children.every(child => !visible(child))).toBe(true)
    expect(stackedPickOrder(hits)).toEqual(['INS1'])
    view.update({})
    expect(stackedPickOrder(hits)).toEqual(['INS1', 'F1'])
  })

  it('excludes hits when the object itself or any ancestor is hidden', () => {
    const parent = new THREE.Group(), child = new THREE.Group(), object = new THREE.Object3D()
    parent.add(child)
    child.add(object)
    object.userData = { kind: 'insert', handle: 'A' }
    parent.visible = false
    expect(pickHandleFromHits([{ object }])).toBeNull()
    parent.visible = true
    object.visible = false
    expect(stackedPickOrder([{ object }])).toEqual([])
  })

  it('leaves one-finger pan and two-finger zoom available with the marquee gate open', () => {
    const select = vi.fn()
    const view = mountViewer({ marqueeGate: () => true, onMarqueeSelect: select })
    const canvas = view.container.querySelector('canvas'), controls = gl.controls.at(-1)
    pointer(canvas, 'pointerdown', 'touch', 1)
    pointer(canvas, 'pointermove', 'touch', 1, 40)
    expect(controls.enabled).toBe(true)
    expect(controls.touches).toEqual({ ONE: THREE.TOUCH.PAN, TWO: THREE.TOUCH.DOLLY_PAN })
    expect(view.container.querySelector('.viewer-marquee')).toBeNull()
    const received = vi.fn()
    canvas.addEventListener('pointerdown', received)
    expect(pointer(canvas, 'pointerdown', 'touch', 2, 60).defaultPrevented).toBe(false)
    expect(received).toHaveBeenCalledTimes(1)
    expect(controls.enabled).toBe(true)
    pointer(canvas, 'pointerup', 'touch', 2, 60)
    pointer(canvas, 'pointerup', 'touch', 1, 40)
    expect(select).not.toHaveBeenCalled()
  })

  it('releases a mouse marquee when touch begins so navigation receives the gesture', () => {
    const view = mountViewer({ marqueeGate: () => true, onMarqueeSelect: vi.fn() })
    const canvas = view.container.querySelector('canvas'), controls = gl.controls.at(-1)
    pointer(canvas, 'pointerdown', 'mouse', 1)
    pointer(canvas, 'pointermove', 'mouse', 1, 40)
    expect(view.container.querySelector('.viewer-marquee')).not.toBeNull()
    expect(controls.enabled).toBe(false)
    const received = vi.fn()
    canvas.addEventListener('pointerdown', received)
    pointer(canvas, 'pointerdown', 'touch', 2, 60)
    expect(view.container.querySelector('.viewer-marquee')).toBeNull()
    expect(controls.enabled).toBe(true)
    expect(received).toHaveBeenCalledTimes(1)
  })

  it('does not select an entity when a stationary secondary touch ends before the primary touch', () => {
    const select = vi.fn()
    const view = mountViewer({ marqueeGate: () => true, onMarqueeSelect: vi.fn(), onSelectEntity: select })
    const canvas = view.container.querySelector('canvas')
    const hits = vi.spyOn(THREE.Raycaster.prototype, 'intersectObjects').mockReturnValue([{ object: entity('INS1') }])
    pointer(canvas, 'pointerdown', 'touch', 1)
    pointer(canvas, 'pointerdown', 'touch', 2, 60, false)
    pointer(canvas, 'pointerup', 'touch', 2, 60, false)
    expect(select).not.toHaveBeenCalled()
    pointer(canvas, 'pointerup', 'touch', 1)
    expect(select).not.toHaveBeenCalled()
    expect(hits).not.toHaveBeenCalled()
  })

  it('suppresses later touches in the same gesture and restores picking after all touches cancel', () => {
    const select = vi.fn()
    const view = mountViewer({ marqueeGate: () => true, onMarqueeSelect: vi.fn(), onSelectEntity: select })
    const canvas = view.container.querySelector('canvas')
    vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 100, height: 100 })
    vi.spyOn(THREE.Raycaster.prototype, 'intersectObjects').mockReturnValue([{ object: entity('INS1') }])
    pointer(canvas, 'pointerdown', 'touch', 1)
    pointer(canvas, 'pointerdown', 'touch', 2, 60, false)
    pointer(canvas, 'pointerup', 'touch', 2, 60, false)
    pointer(canvas, 'pointerdown', 'touch', 3, 60, false)
    pointer(canvas, 'pointerup', 'touch', 3, 60, false)
    pointer(window, 'pointercancel', 'touch', 1)
    expect(select).not.toHaveBeenCalled()
    pointer(canvas, 'pointerdown', 'touch', 4)
    pointer(canvas, 'pointerup', 'touch', 4)
    expect(select).toHaveBeenCalledTimes(1)
    expect(select).toHaveBeenCalledWith('INS1', { additive: false })
  })
})
