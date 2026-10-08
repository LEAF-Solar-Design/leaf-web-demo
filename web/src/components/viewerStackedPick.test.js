import { describe, expect, it } from 'vitest'
import * as THREE from 'three'
import {
  STACKED_PICK_MOVE_PX,
  STACKED_PICK_WINDOW_MS,
  createStackedPickCycle,
  pickHandleFromHits,
  stackedPickOrder,
} from './Viewer.jsx'

function meshHit(handle, { visible = true } = {}) {
  const group = new THREE.Group()
  group.visible = visible
  const mesh = new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshBasicMaterial())
  mesh.userData = { kind: 'insert', handle }
  group.add(mesh)
  return { object: mesh }
}

function polyfillHit(triHandles, faceIndex) {
  const group = new THREE.Group()
  const mesh = new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshBasicMaterial())
  mesh.userData = { kind: 'polyfill', triHandles }
  group.add(mesh)
  return { object: mesh, faceIndex }
}

function blocklinesHit(lineHandles, index) {
  const group = new THREE.Group()
  const lines = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial())
  lines.userData = { kind: 'blocklines', lineHandles }
  group.add(lines)
  return { object: lines, index }
}

const STACK = [
  meshHit('A1'),
  polyfillHit(['P0', 'B2', 'P2'], 1),
  meshHit('A1'), // same entity hit twice (two faces) is one candidate
  meshHit('HIDDEN', { visible: false }),
  blocklinesHit(['L0', 'C3'], 3),
]

describe('stackedPickOrder', () => {
  it('lists distinct visible handles nearest first, led by the plain pick', () => {
    const order = stackedPickOrder(STACK)
    expect(order).toEqual(['A1', 'B2', 'C3'])
    expect(order[0]).toBe(pickHandleFromHits(STACK))
  })

  it('returns an empty order for no hits or only hidden hits', () => {
    expect(stackedPickOrder([])).toEqual([])
    expect(stackedPickOrder([meshHit('X', { visible: false })])).toEqual([])
  })
})

describe('createStackedPickCycle', () => {
  const order = ['A1', 'B2', 'C3']

  it('uses a 4 px and 600 ms repeat window', () => {
    expect(STACKED_PICK_MOVE_PX).toBe(4)
    expect(STACKED_PICK_WINDOW_MS).toBe(600)
  })

  it('cycles repeat clicks through the stack in order and wraps', () => {
    const cycle = createStackedPickCycle()
    expect(cycle.pick(order, 100, 100, 0)).toBe('A1')
    expect(cycle.pick(order, 101, 102, 300)).toBe('B2')
    expect(cycle.pick(order, 103, 101, 800)).toBe('C3')
    expect(cycle.pick(order, 102, 100, 1300)).toBe('A1')
  })

  it('resets to the nearest hit when the pointer moves over 4 px', () => {
    const cycle = createStackedPickCycle()
    expect(cycle.pick(order, 100, 100, 0)).toBe('A1')
    cycle.move(103, 100) // within 4 px keeps the cycle
    expect(cycle.pick(order, 100, 100, 100)).toBe('B2')
    cycle.move(105, 100) // over 4 px resets it
    expect(cycle.pick(order, 100, 100, 200)).toBe('A1')
  })

  it('resets when the repeat click itself lands over 4 px away', () => {
    const cycle = createStackedPickCycle()
    expect(cycle.pick(order, 100, 100, 0)).toBe('A1')
    expect(cycle.pick(order, 105, 100, 100)).toBe('A1')
  })

  it('resets after the 600 ms timeout', () => {
    const cycle = createStackedPickCycle()
    expect(cycle.pick(order, 100, 100, 0)).toBe('A1')
    expect(cycle.pick(order, 100, 100, 600)).toBe('B2')
    expect(cycle.pick(order, 100, 100, 1201)).toBe('A1')
  })

  it('restarts when the stack under the pointer changes', () => {
    const cycle = createStackedPickCycle()
    expect(cycle.pick(order, 100, 100, 0)).toBe('A1')
    expect(cycle.pick(['A1', 'D4'], 100, 100, 100)).toBe('A1')
    expect(cycle.pick(['A1', 'D4'], 100, 100, 200)).toBe('D4')
  })

  it('leaves single and empty hits unchanged, however fast the repeat', () => {
    const cycle = createStackedPickCycle()
    const single = [meshHit('S1')]
    const singleOrder = stackedPickOrder(single)
    expect(cycle.pick(singleOrder, 100, 100, 0)).toBe(pickHandleFromHits(single))
    expect(cycle.pick(singleOrder, 100, 100, 50)).toBe('S1')
    expect(cycle.pick(singleOrder, 100, 100, 100)).toBe('S1')
    expect(cycle.pick([], 100, 100, 150)).toBeNull()
    expect(pickHandleFromHits([])).toBeNull()
  })

  it('a single hit between stacked clicks breaks the cycle', () => {
    const cycle = createStackedPickCycle()
    expect(cycle.pick(order, 100, 100, 0)).toBe('A1')
    expect(cycle.pick(['S1'], 100, 100, 100)).toBe('S1')
    expect(cycle.pick(order, 100, 100, 200)).toBe('A1')
  })

  it('reset() clears the cycle', () => {
    const cycle = createStackedPickCycle()
    expect(cycle.pick(order, 100, 100, 0)).toBe('A1')
    cycle.reset()
    expect(cycle.pick(order, 100, 100, 100)).toBe('A1')
  })
})
