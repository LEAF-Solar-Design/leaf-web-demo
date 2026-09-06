import { describe, expect, it } from 'vitest'

import { BLOCK_CHILD_CAP, CIRCLE_SEGMENTS, MAX_ARRAY_CELLS, engineIntake, expandBlockReference } from './engineIntake.js'
import { projectionEntities } from './engineSession.js'
import { diffPlan } from './mutationDiff.js'
import { addPickDescriptor, pickHandleFromHits } from '../components/Viewer.jsx'
import * as THREE from 'three'

const line = { handle: '256', type: 'LINE', editable: false, layer: '0', vertices: [[1, 2, 0], [4, 2, 0]], closed: false }
const circle = { handle: '257', type: 'CIRCLE', editable: false, layer: '0', vertices: [[1, 2, 0]], radius: 1 }
const block = { name: 'B', base: [1, 2, 0], children: [line], complete: true }
const insert = { id: '1280', handle: '1280', type: 'INSERT', name: 'B', ip: [10, 20, 0], scale: [2, 3, 1], rotationDeg: 90, layer: 'Refs', editable: false }
const projection = (reference = insert, definition = block) => ({ entities: [reference], blocks: [definition] })

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
