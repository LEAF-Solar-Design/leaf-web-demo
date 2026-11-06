import { describe, expect, it } from 'vitest'

import { VIEW_HISTORY_LIMIT, createViewHistory, layerBounds } from './viewHistory.js'

const pose = (n) => ({ position: [n, n, 10], target: [n, n, 0], zoom: n + 1, near: 0.1, far: 100, worldPerPixel: 1 / (n + 1) })

describe('createViewHistory', () => {
  it('starts empty; pop and peek on empty are null', () => {
    const h = createViewHistory()
    expect(h.size()).toBe(0)
    expect(h.pop()).toBeNull()
    expect(h.peek()).toBeNull()
  })

  it('pops newest first and restores all three parts', () => {
    const h = createViewHistory()
    h.push({ pose: pose(1), selectedHandle: 'A', visibleLayers: { L1: true } })
    h.push({ pose: pose(2), selectedHandle: null, visibleLayers: { L1: false } })
    expect(h.size()).toBe(2)
    expect(h.peek().pose.target).toEqual([2, 2, 0])
    const second = h.pop()
    expect(second).toEqual({ pose: pose(2), selectedHandle: null, selectedHandles: [], focusedId: null, query: '', drawingKey: null, visibleLayers: { L1: false } })
    const first = h.pop()
    expect(first.selectedHandle).toBe('A')
    expect(first.selectedHandles).toEqual(['A'])
    expect(first.visibleLayers).toEqual({ L1: true })
    expect(h.size()).toBe(0)
  })

  it('never pushes a null or malformed pose', () => {
    const h = createViewHistory()
    expect(h.push({ pose: null, selectedHandle: 'A' })).toBe(false)
    expect(h.push({ pose: { zoom: 1 } })).toBe(false)
    expect(h.push()).toBe(false)
    expect(h.size()).toBe(0)
    expect(h.push({ pose: pose(0) })).toBe(true)
    expect(h.size()).toBe(1)
  })

  it('copies multi-selection and retains focus, query and drawing scope', () => {
    const h = createViewHistory()
    const selectedHandles = ['2A', '2B', '2C']
    h.push({ pose: pose(1), selectedHandle: 'old', selectedHandles, focusedId: 'g:frame', query: 'go to North', drawingKey: 'engine:guest' })
    selectedHandles.splice(0, 3, 'FF')
    expect(h.pop()).toMatchObject({ selectedHandles: ['2A', '2B', '2C'], focusedId: 'g:frame', query: 'go to North', drawingKey: 'engine:guest' })
    h.push({ pose: pose(1), selectedHandle: 'old', selectedHandles: [] })
    expect(h.pop().selectedHandles).toEqual([])
  })

  it('copies the layer map and pose so later mutation never reaches a snapshot', () => {
    const h = createViewHistory()
    const layers = { L1: true }
    const p = pose(3)
    h.push({ pose: p, visibleLayers: layers })
    layers.L1 = false
    p.target[0] = 99
    const snap = h.pop()
    expect(snap.visibleLayers).toEqual({ L1: true })
    expect(snap.pose.target[0]).toBe(3)
  })

  it(`is bounded at ${VIEW_HISTORY_LIMIT} and drops the oldest`, () => {
    expect(VIEW_HISTORY_LIMIT).toBe(50)
    const h = createViewHistory()
    for (let i = 0; i < 60; i++) h.push({ pose: pose(i) })
    expect(h.size()).toBe(50)
    expect(h.peek().pose.target[0]).toBe(59)
    let last = null
    while (h.size()) last = h.pop()
    // The ten oldest (0..9) were dropped; the oldest survivor is 10.
    expect(last.pose.target[0]).toBe(10)
  })

  it('clear empties it', () => {
    const h = createViewHistory(3)
    h.push({ pose: pose(1) })
    h.push({ pose: pose(2) })
    h.clear()
    expect(h.size()).toBe(0)
    expect(h.pop()).toBeNull()
  })
})

describe('layerBounds (Up frames the selected layer)', () => {
  const intake = {
    polylines: [
      { handle: 'P1', layer: 'Walls', pts: [[0, 0], [10, 0], [10, 5]] },
      { handle: 'P2', layer: 'Walls', pts: [[-2, 3]] },
      { handle: 'P3', layer: 'Other', pts: [[100, 100], [200, 200]] },
      { handle: 'P4', layer: 'Dot', pts: [[4, 7]] },
    ],
    inserts: [
      { handle: 'I1', layer: 'Blocks', pt: [1, 2] },
      { handle: 'I2', layer: 'Blocks', pt: [6, -3] },
    ],
    faces3d: [
      { handle: 'F1', layer: 'Surfaces', p1: [0, 0, 0], p2: [3, 0, 0], p3: [3, 4, 1], p4: [0, 4, 1] },
    ],
    layers: ['Walls', 'Other', 'Dot', 'Blocks', 'Surfaces', 'Empty'],
  }

  it('covers a polyline layer and ignores other layers', () => {
    expect(layerBounds(intake, 'Walls')).toEqual({ minX: -2, minY: 0, maxX: 10, maxY: 5 })
  })

  it('an inserts-only layer returns the bounds of its insertion points', () => {
    expect(layerBounds(intake, 'Blocks')).toEqual({ minX: 1, minY: -3, maxX: 6, maxY: 2 })
  })

  it('a 3D-faces-only layer returns its corner bounds', () => {
    expect(layerBounds(intake, 'Surfaces')).toEqual({ minX: 0, minY: 0, maxX: 3, maxY: 4 })
  })

  it('a single-point layer returns a padded, non-degenerate box', () => {
    const box = layerBounds(intake, 'Dot')
    expect(box).toEqual({ minX: 3.5, minY: 6.5, maxX: 4.5, maxY: 7.5 })
    expect(box.maxX - box.minX).toBeGreaterThan(0)
    expect(box.maxY - box.minY).toBeGreaterThan(0)
  })

  it('a layer with no geometry, a null layer and a missing intake return null', () => {
    expect(layerBounds(intake, 'Empty')).toBeNull()
    expect(layerBounds(intake, null)).toBeNull()
    expect(layerBounds(null, 'Walls')).toBeNull()
  })
})

// APS/DXF producer shape: raw WCS insertion point, radians, kind-based children.
const serverBlocks = () => ({
  polylines: [],
  blocks: { Fixture: { base: [10, 20, 0], count: 1, complete: true,
    children: [{ kind: 'LINE', layer: '0', pts: [[10, 20, 0], [14, 22, 0]] }] } },
  inserts: [{ handle: 'A100', name: 'Fixture', layer: 'Panels', x: 100, y: 200, z: 0,
    rot: Math.PI / 2, scale: [-2, 3, 1], nrm: [0, 0, 1] }],
})
const blockBounds = { minX: 94, minY: 192, maxX: 100, maxY: 200 }

it('frames exact server block children on their rendered layers and leaves the source unchanged', () => {
  const intake = serverBlocks(), before = structuredClone(intake)
  expect(layerBounds(intake, 'Panels')).toEqual(blockBounds)
  expect(intake).toEqual(before)
  intake.blocks.Fixture.children[0].layer = 'Detail'
  expect(layerBounds(intake, 'Detail')).toEqual(blockBounds)
  expect(layerBounds(intake, 'Panels')).toBeNull()
})

it('uses partial block paths and only the normalized insertion fallback for missing geometry', () => {
  const intake = serverBlocks()
  intake.blocks.Fixture.complete = false
  intake.blocks.Fixture.children[0].layer = 'Detail'
  expect(layerBounds(intake, 'Detail')).toEqual(blockBounds)
  expect(layerBounds(intake, 'Panels')).toEqual({ minX: 99.5, minY: 199.5, maxX: 100.5, maxY: 200.5 })
  intake.blocks = {}
  expect(layerBounds(intake, 'Panels')).toEqual({ minX: 99.5, minY: 199.5, maxX: 100.5, maxY: 200.5 })
  intake.inserts[0].x = Infinity
  expect(layerBounds(intake, 'Panels')).toBeNull()
})

it.each(['engine', 'bridge'])('preserves existing %s projection and insertion-point bounds', (source) => {
  const intake = serverBlocks()
  intake.source = source
  intake.polylines = [{ handle: 'A100', layer: 'Panels', pts: [[94, 192, 0], [100, 200, 0]] }]
  intake.inserts = [{ handle: 'A100', layer: 'Panels', pt: [101, 201, 0], incomplete: false }]
  expect(layerBounds(intake, 'Panels')).toEqual({ ...blockBounds, maxX: 101, maxY: 201 })
})

it('uses the shared projector for OCS child coordinates without changing drawing units', () => {
  const intake = serverBlocks()
  intake.blocks.Fixture.base = [0, 0, 0]
  intake.blocks.Fixture.children[0].pts = [[0, 0, 0], [2, 3, 4]]
  Object.assign(intake.inserts[0], { x: 10, y: 20, z: 30, rot: 0, scale: [1, 1, 1], nrm: [0, 1, 0] })
  expect(layerBounds(intake, 'Panels')).toEqual({ minX: 8, minY: 20, maxX: 10, maxY: 24 })
})
