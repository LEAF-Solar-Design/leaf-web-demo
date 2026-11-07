import { describe, expect, it } from 'vitest'

import { BLOCK_CHILD_CAP, CIRCLE_SEGMENTS, MAX_ARRAY_CELLS, MAX_POINTS, engineIntake, expandBlockReference, projectServerBlocks } from './engineIntake.js'
import { projectionEntities } from './engineSession.js'
import { diffPlan } from './mutationDiff.js'
import { addPickDescriptor, pickHandleFromHits } from '../components/Viewer.jsx'
import * as THREE from 'three'

const line = { handle: '256', type: 'LINE', editable: false, layer: '0', vertices: [[1, 2, 0], [4, 2, 0]], closed: false }
const circle = { handle: '257', type: 'CIRCLE', editable: false, layer: '0', vertices: [[1, 2, 0]], radius: 1 }
const block = { name: 'B', base: [1, 2, 0], children: [line], complete: true }
const insert = { id: '1280', handle: '1280', type: 'INSERT', name: 'B', ip: [10, 20, 0], scale: [2, 3, 1], rotationDeg: 90, layer: 'Refs', editable: false }
const projection = (reference = insert, definition = block) => ({ entities: [reference], blocks: [definition] })

// Same producer fields as server/tests/test_w4g7b_01s.py's DXF/inspection
// round-trip fixture: name-keyed definitions, kind/pts children, raw INSERT
// coordinates and a radian rotation. It is not an engine projection.
const serverSource = () => ({
  polylines: [],
  blocks: { Fixture: { base: [10, 20, 0], count: 1, complete: true,
    children: [{ kind: 'LINE', layer: '0', pts: [[10, 20, 0], [14, 22, 0]] }] } },
  inserts: [{ handle: 'A100', name: 'Fixture', layer: 'Panels', x: 100, y: 200, z: 0,
    rot: Math.PI / 2, nrm: [0, 0, 1], scale: [-2, 3, 1] }],
})

describe('server block projection', () => {
  it('projects a complete server-only block, preserving its world geometry, owner and source', () => {
    const source = serverSource(), before = structuredClone(source)
    const view = projectServerBlocks(source)
    expect(view.polylines).toHaveLength(1)
    const pl = view.polylines[0]
    expect(pl.pts[0]).toEqual([100, 200, 0])
    expect(pl.pts[1][0]).toBeCloseTo(94, 10)
    expect(pl.pts[1][1]).toBeCloseTo(192, 10)
    expect(pl).toMatchObject({ handle: 'A100', sourceHandle: 'A100', layer: 'Panels', closed: false })
    expect(view.inserts[0]).toMatchObject({ pt: [100, 200, 0], rot: 90, incomplete: false })
    const xs = pl.pts.map((p) => p[0]), ys = pl.pts.map((p) => p[1])
    expect(Math.max(...xs) - Math.min(...xs)).toBeCloseTo(6, 10)
    expect(Math.max(...ys) - Math.min(...ys)).toBeCloseTo(8, 10)
    expect(pl.pts.flat().every(Number.isFinite)).toBe(true)
    expect(source).toEqual(before)
    const pickIndex = new Map()
    addPickDescriptor(pickIndex, pl.sourceHandle, { kind: 'poly', layer: pl.layer, pts: pl.pts })
    expect(pickIndex.has('A100')).toBe(true)
  })

  it('preserves numeric-looking server hex handles and explicit child layers', () => {
    const source = serverSource()
    source.inserts[0].handle = '30'
    source.blocks.Fixture.children[0].layer = 'Detail'
    const pl = projectServerBlocks(source).polylines[0]
    expect(pl.handle).toBe('30')
    expect(pl.sourceHandle).toBe('30')
    expect(pl.layer).toBe('Detail')
  })

  it('keeps array spacing outside child scale and rotation', () => {
    const source = serverSource()
    Object.assign(source.inserts[0], { columns: 2, rows: 1, columnSpacing: 10, rowSpacing: 0 })
    const view = projectServerBlocks(source)
    expect(view.polylines).toHaveLength(2)
    expect(view.polylines[1].pts[0]).toEqual([110, 200, 0])
    expect(view.polylines[1].pts[1][0]).toBeCloseTo(104, 10)
    expect(view.polylines[1].pts[1][1]).toBeCloseTo(192, 10)
  })

  it('applies the INSERT normal after the child transform', () => {
    const source = serverSource()
    Object.assign(source.inserts[0], { x: 10, y: 20, z: 30, scale: [1, 1, 1], rot: 0, nrm: [0, 1, 0] })
    source.blocks.Fixture.base = [0, 0, 0]
    source.blocks.Fixture.children[0].pts = [[0, 0, 0], [2, 3, 4]]
    const pl = projectServerBlocks(source).polylines[0]
    expect(pl.pts).toEqual([[10, 20, 30], [8, 24, 33]])
  })

  it('maps a child polyline elevation and normal before its INSERT transform', () => {
    const source = serverSource()
    Object.assign(source.inserts[0], { x: 0, y: 0, z: 0, scale: [1, 1, 1], rot: 0 })
    Object.assign(source.blocks.Fixture, { base: [0, 0, 0], children: [
      { kind: 'LWPOLYLINE', layer: '0', closed: false, elev: 4, nrm: [0, 1, 0], pts: [[0, 0], [2, 3]] },
    ] })
    expect(projectServerBlocks(source).polylines[0].pts).toEqual([[0, 4, 0], [-2, 4, 3]])
  })

  it('samples curved children in their own plane before a nonuniform scale', () => {
    const source = serverSource()
    Object.assign(source.inserts[0], { x: 0, y: 0, z: 0, scale: [2, 3, 4], rot: 0 })
    Object.assign(source.blocks.Fixture, { base: [0, 0, 0], children: [
      { kind: 'CIRCLE', layer: '0', c: [0, 0, 0], r: 1, nrm: [0, 1, 0] },
    ] })
    const pl = projectServerBlocks(source).polylines[0]
    expect(pl.closed).toBe(true)
    expect(pl.pts[0]).toEqual([-2, 0, 0])
    expect(pl.pts[CIRCLE_SEGMENTS / 4][0]).toBeCloseTo(0, 10)
    expect(pl.pts[CIRCLE_SEGMENTS / 4][2]).toBeCloseTo(4, 10)
  })

  it('keeps partial geometry and a normalized fallback for nested and unknown children', () => {
    const source = serverSource()
    source.blocks.Fixture.children.push({ kind: 'OTHER', type: 'INSERT', layer: '0' })
    source.blocks.Fixture.count = 2
    source.blocks.Fixture.complete = false
    const view = projectServerBlocks(source)
    expect(view.polylines).toHaveLength(1)
    expect(view.inserts[0]).toMatchObject({ pt: [100, 200, 0], incomplete: true })
    expect(view.incompleteInserts).toBe(1)
    delete source.blocks.Fixture
    expect(projectServerBlocks(source)).toMatchObject({ polylines: [], incompleteInserts: 1 })
  })

  it('does not hide a falsely complete definition after an unsupported child or missing rows', () => {
    const source = serverSource()
    source.blocks.Fixture.children = [{ kind: 'INSERT', name: 'Nested' }]
    expect(projectServerBlocks(source)).toMatchObject({ polylines: [], incompleteInserts: 1 })
    source.blocks.Fixture.children = []
    expect(projectServerBlocks(source)).toMatchObject({ polylines: [], incompleteInserts: 1 })
  })

  it.each([{ rot: Infinity }, { rot: '1' }, { scale: [1, NaN, 1] }, { nrm: [0, 0, 0] }])(
    'keeps a finite fallback instead of inventing geometry for invalid transforms %s', (change) => {
      const source = serverSource()
      Object.assign(source.inserts[0], change)
      const view = projectServerBlocks(source)
      expect(view.polylines).toEqual([])
      expect(view.inserts[0].incomplete).toBe(true)
      expect(view.inserts[0].pt.every(Number.isFinite)).toBe(true)
      expect(Number.isFinite(view.inserts[0].rot)).toBe(true)
      expect(view.inserts[0].scale.every(Number.isFinite)).toBe(true)
    })

  it('bounds emitted points and retains fallback when a large array is only partly projected', () => {
    const source = serverSource()
    source.blocks.Fixture.children = Array.from({ length: 60 }, () => ({
      kind: 'CIRCLE', layer: '0', c: [0, 0, 0], r: 1, nrm: [0, 0, 1],
    }))
    source.blocks.Fixture.count = 60
    Object.assign(source.inserts[0], { columns: MAX_ARRAY_CELLS, rows: 1, columnSpacing: 10 })
    const view = projectServerBlocks(source)
    expect(view.polylines.reduce((sum, pl) => sum + pl.pts.length, 0)).toBeLessThanOrEqual(MAX_POINTS)
    expect(view.incompleteInserts).toBe(1)
    expect(view.truncated).toBeGreaterThan(0)
  })

  it('does not replace malformed stored coordinates or normals with a fabricated plane', () => {
    for (const damage of [
      (source) => { source.blocks.Fixture.base[2] = null },
      (source) => { source.blocks.Fixture.children = [{ kind: 'CIRCLE', layer: '0', c: [0, 0, null], r: 1 }] },
      (source) => { source.inserts[0].nrm = [1.7e308, 1.7e308, 1.7e308] },
    ]) {
      const source = serverSource()
      damage(source)
      const view = projectServerBlocks(source)
      expect(view.polylines).toEqual([])
      expect(view.incompleteInserts).toBe(1)
    }
  })

  it('does not expand an engine document a second time or alter its source summary', () => {
    const engine = engineIntake(projection(), 'd', undefined, 18)
    const before = structuredClone(engine)
    const view = projectServerBlocks(engine)
    expect(view.polylines).toEqual([])
    expect(view.inserts).toBe(engine.inserts)
    expect(engine).toEqual(before)
    expect(engine.sourceSummary.entityCount).toBe(18)
  })
})

describe('W4g-7b-01c blockExpansion', () => {
  it.each([[2, 26], [-2, 14]])('scales x by %s before rotating, placing the endpoint at y=%s', (sx, endY) => {
    const intake = engineIntake(projection({ ...insert, scale: [sx, 3, 1] }))
    expect(intake.polylines).toHaveLength(1)
    const poly = intake.polylines[0]
    expect(poly.pts[0]).toEqual([10, 20, 0])
    expect(poly.pts[1][0]).toBeCloseTo(10, 10)
    expect(poly.pts[1][1]).toBeCloseTo(endY, 10)
    expect(poly.sourceHandle).toBe('500')
    expect(poly.handle).toBe('500')
    expect(poly.layer).toBe('Refs')
    expect(intake.inserts[0].incomplete).toBe(false)
  })

  it('one INSERT expands to two polylines, both picked through its hex handle', () => {
    const source = projection(insert, { ...block, children: [line, circle] })
    const intake = engineIntake(source)
    expect(source.entities).toHaveLength(1)
    expect(intake.polylines).toHaveLength(2)
    expect(intake.polylines.map((p) => p.sourceHandle)).toEqual(['500', '500'])
    expect(intake.polylines.map((p) => p.handle)).toEqual(['500', '500'])
    expect(intake.points).toBe(2 + CIRCLE_SEGMENTS)
    // Non-uniform scale turns the circle into an ellipse after sampling.
    const pts = intake.polylines[1].pts
    expect(pts[0][0]).toBeCloseTo(10, 10)
    expect(pts[0][1]).toBeCloseTo(22, 10)
    expect(pts[CIRCLE_SEGMENTS / 4][0]).toBeCloseTo(7, 10)
    expect(pts[CIRCLE_SEGMENTS / 4][1]).toBeCloseTo(20, 10)
  })

  it('incomplete definitions keep listed geometry and request the square marker', () => {
    const intake = engineIntake(projection(insert, { ...block, complete: false }))
    expect(intake.polylines).toHaveLength(1)
    expect(intake.inserts).toMatchObject([{ handle: '500', pt: [10, 20, 0], incomplete: true }])
    const missing = engineIntake([insert])
    expect(missing.polylines).toEqual([])
    expect(missing.inserts[0].incomplete).toBe(true)
  })

  it('bounds expansion and refuses to recurse into nested or foreign children', () => {
    const many = expandBlockReference(insert, { ...block, children: Array.from({ length: 61 }, () => line) })
    expect(many.polylines).toHaveLength(BLOCK_CHILD_CAP)
    expect(many.complete).toBe(false)
    const nested = expandBlockReference(insert, { ...block, children: [line, insert, { ...line, type: 'FUTURE' }] })
    expect(nested.polylines).toHaveLength(1)
    expect(nested.complete).toBe(false)
  })

  it('maps z about the base, keeps explicit child layers, and samples rotated TEXT before transforming', () => {
    const reference = { ...insert, ip: [10, 20, 30], scale: [2, 3, 4], rotationDeg: 0 }
    const text = { handle: '258', type: 'TEXT', layer: 'Labels', vertices: [[1, 2, 5]], text: 'A', height: 1, rotationDeg: 90 }
    const intake = engineIntake(projection(reference, { ...block, base: [1, 2, 3], children: [text] }))
    const pl = intake.polylines[0]
    expect(pl.pts[0]).toEqual([10, 20, 38])
    expect(pl.pts[1][0]).toBeCloseTo(10, 10)
    expect(pl.pts[1][1]).toBeCloseTo(21.8, 10)
    expect(pl.layer).toBe('Labels')
    expect(pl.closed).toBe(true)
  })

  it('keeps block catalogues on session snapshots for drawing, saving, and undo', () => {
    const before = projectionEntities(projection())
    const after = projectionEntities(projection(insert, { ...block, base: [0, 0, 0] }))
    expect(before).toHaveLength(1)
    expect(before.blocks[0].base).toEqual([1, 2, 0])
    expect(engineIntake(before).polylines[0].pts[0]).toEqual([10, 20, 0])
    expect(diffPlan(before, after).reason).toMatch(/definition.*cannot carry/)
    expect(diffPlan(before, projectionEntities(projection()))).toEqual({ mutations: {}, count: 0, reason: null })
    expect(projectionEntities({ entities: [] }).blocks).toBeUndefined()
  })

  it('does not turn malformed references into invented geometry', () => {
    expect(expandBlockReference({ ...insert, scale: [2, NaN, 1] }, block)).toEqual({ polylines: [], complete: false })
    expect(expandBlockReference({ ...insert, ip: null }, block)).toEqual({ polylines: [], complete: false })
  })

  it('adds unscaled array spacing to the insertion point as the crate expands cells, with a cell cap', () => {
    const reference = { ...insert, scale: [2, 1, 1], rotationDeg: 0, columns: 2, rows: 1, columnSpacing: 10, rowSpacing: 4 }
    const intake = engineIntake(projection(reference))
    expect(intake.polylines.map((p) => p.pts)).toEqual([
      [[10, 20, 0], [16, 20, 0]], [[20, 20, 0], [26, 20, 0]],
    ])
    expect(intake.polylines.every((p) => p.sourceHandle === '500')).toBe(true)
    expect(intake.inserts[0].incomplete).toBe(false)
    const transformed = expandBlockReference({ ...reference, rows: 2, scale: [-2, 3, 1], rotationDeg: 90 }, block)
    const starts = transformed.polylines.map((p) => p.pts[0])
    for (const [i, expected] of [[10, 20], [20, 20], [10, 24], [20, 24]].entries()) {
      expect(starts[i][0]).toBeCloseTo(expected[0], 9)
      expect(starts[i][1]).toBeCloseTo(expected[1], 9)
      expect(transformed.polylines[i].pts[1][0]).toBeCloseTo(expected[0], 9)
      expect(transformed.polylines[i].pts[1][1]).toBeCloseTo(expected[1] - 6, 9)
    }
    const capped = engineIntake(projection({ ...reference, columns: MAX_ARRAY_CELLS + 1 }))
    expect(capped.polylines).toHaveLength(MAX_ARRAY_CELLS)
    expect(capped.inserts[0].incomplete).toBe(true)
    expect(expandBlockReference({ ...reference, rows: 0 }, block)).toEqual({ polylines: [], complete: false })
  })

  it('keeps both child descriptors and a LINE click selects the parent INSERT', () => {
    const intake = engineIntake(projection(insert, { ...block, children: [line, circle] }))
    const index = new Map()
    const meshes = intake.polylines.map((pl) => {
      addPickDescriptor(index, pl.sourceHandle, { kind: 'poly', pts: pl.pts })
      const segments = []
      const handles = []
      for (let i = 0; i < pl.pts.length - (pl.closed ? 0 : 1); i += 1) {
        segments.push(...pl.pts[i], ...pl.pts[(i + 1) % pl.pts.length])
        handles.push(pl.sourceHandle)
      }
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(segments, 3))
      const mesh = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial())
      mesh.userData = { kind: 'blocklines', lineHandles: handles }
      return mesh
    })
    expect(index.get('500')).toHaveLength(2)
    const ray = new THREE.Raycaster(new THREE.Vector3(10, 25, 10), new THREE.Vector3(0, 0, -1))
    ray.params.Line.threshold = 0.01
    const hits = ray.intersectObjects(meshes, false)
    expect(hits[0].object).toBe(meshes[0])
    expect(pickHandleFromHits(hits)).toBe('500')
    ray.set(new THREE.Vector3(7, 20, 10), new THREE.Vector3(0, 0, -1))
    expect(pickHandleFromHits(ray.intersectObjects(meshes, false))).toBe('500')
    for (const mesh of meshes) { mesh.geometry.dispose(); mesh.material.dispose() }
  })
})
