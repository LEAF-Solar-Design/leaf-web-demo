import { describe, expect, it } from 'vitest'

import { bulgePoints, ARC_STEP_DEG, CIRCLE_SEGMENTS, MAX_POINTS, MIN_ARC_POINTS, engineIntake, entityToPolyline, hexHandle } from './engineIntake.js'

const near = (a, b, eps = 1e-9) => Math.abs(a - b) < eps

describe('engineIntake (W4f slice A0): engine entities -> viewer intake', () => {
  it('W4g-7b-01c keeps an unknown base as a glyph without suppressing known definitions', () => {
    const reference = { handle: '1280', type: 'INSERT', name: 'b', ip: [10, 20, 0], scale: [1, 1, 1], rotationDeg: 0 }
    const definition = { name: 'B', base: [1, 2, 0], complete: true,
      children: [{ type: 'LINE', vertices: [[1, 2, 0], [4, 2, 0]] }] }
    const source = { entities: [reference], blocks: [definition] }
    expect(engineIntake(source).polylines[0].pts).toEqual([[10, 20, 0], [13, 20, 0]])
    const unknown = engineIntake({ ...source, blocks: [{ ...definition, baseUnknown: true }] })
    expect(unknown.polylines).toEqual([])
    expect(unknown.inserts).toMatchObject([{ handle: '500', incomplete: true }])
    const mixed = engineIntake({ entities: [reference, { ...reference, handle: '1281', name: 'C' }],
      blocks: [{ ...definition, baseUnknown: true, complete: false }, { ...definition, name: 'C' }] })
    expect(mixed.polylines).toMatchObject([{ sourceHandle: '501', pts: [[10, 20, 0], [13, 20, 0]] }])
    expect(mixed.inserts).toMatchObject([{ handle: '500', incomplete: true }, { handle: '501', incomplete: false }])
  })

  it('W4g-7b-01c scales the child but leaves array spacing unscaled', () => {
    const reference = { handle: '1280', type: 'INSERT', name: 'B', ip: [10, 20, 0], scale: [2, 1, 1], rotationDeg: 0,
      columns: 2, rows: 1, columnSpacing: 10, rowSpacing: 0 }
    const definition = { name: 'B', base: [1, 2, 0], complete: true,
      children: [{ type: 'LINE', vertices: [[1, 2, 0], [2, 2, 0]] }] }
    expect(engineIntake({ entities: [reference], blocks: [definition] }).polylines.map((p) => p.pts)).toEqual([
      [[10, 20, 0], [12, 20, 0]], [[20, 20, 0], [22, 20, 0]],
    ])
  })

  it('W4g-1b: the intake handle is the DXF hex form of the worker\'s decimal id, so a canvas pick names the drawing\'s own handle', () => {
    expect(hexHandle('37986')).toBe('9462')
    expect(hexHandle('7')).toBe('7')
    expect(hexHandle('255')).toBe('FF')
    expect(hexHandle('18446744073709551615')).toBe('FFFFFFFFFFFFFFFF')
    expect(hexHandle('e1')).toBe('e1')
    expect(hexHandle('')).toBe('')
    expect(hexHandle(undefined)).toBe('')
    const line = entityToPolyline({ id: '37986', type: 'LINE', layer: 'Panels', closed: false, vertices: [[0, 0, 0], [1, 1, 0]] })
    expect(line.handle).toBe('9462')
  })

  it('maps a line and a polyline as they are, keyed by the engine handle, layer and closed flag', () => {
    const line = entityToPolyline({ id: '7', type: 'LINE', layer: 'Panels', closed: false, vertices: [[0, 0, 0], [100, 50, 0]] })
    expect(line).toEqual({ handle: '7', layer: 'Panels', pts: [[0, 0, 0], [100, 50, 0]], closed: false })
    const poly = entityToPolyline({ id: '8', type: 'LWPOLYLINE', layer: 'Outline', closed: true, vertices: [[0, 0], [4, 0], [4, 3]] })
    expect(poly).toEqual({ handle: '8', layer: 'Outline', pts: [[0, 0, 0], [4, 0, 0], [4, 3, 0]], closed: true })
  })

  it('draws a circle as a closed 48-gon on its centre and radius', () => {
    const pl = entityToPolyline({ id: '9', type: 'CIRCLE', layer: '0', vertices: [[3, 3, 0]], radius: 1.5, startDeg: null, endDeg: null })
    expect(pl.closed).toBe(true)
    expect(pl.pts).toHaveLength(CIRCLE_SEGMENTS)
    for (const [x, y] of pl.pts) expect(near(Math.hypot(x - 3, y - 3), 1.5)).toBe(true)
  })

  it('samples an arc counter-clockwise from start to end in degrees, wrapping through 360 when the end is below the start', () => {
    const quarter = entityToPolyline({ id: '10', type: 'ARC', layer: '0', vertices: [[0, 0, 0]], radius: 2, startDeg: 0, endDeg: 90 })
    expect(quarter.closed).toBe(false)
    expect(quarter.pts).toHaveLength(Math.max(MIN_ARC_POINTS, Math.ceil(90 / ARC_STEP_DEG) + 1))
    expect(near(quarter.pts[0][0], 2) && near(quarter.pts[0][1], 0)).toBe(true)
    const last = quarter.pts[quarter.pts.length - 1]
    expect(near(last[0], 0) && near(last[1], 2)).toBe(true)
    const wrap = entityToPolyline({ id: '11', type: 'ARC', layer: '0', vertices: [[0, 0, 0]], radius: 1, startDeg: 350, endDeg: 10 })
    expect(wrap.pts).toHaveLength(MIN_ARC_POINTS)
    const mid = wrap.pts[Math.floor(wrap.pts.length / 2)]
    expect(mid[0]).toBeGreaterThan(0.99)
  })

  it('skips what it cannot draw instead of throwing: bad points, missing radius, one-point lines, foreign kinds', () => {
    expect(entityToPolyline(null)).toBeNull()
    expect(entityToPolyline({ id: '1', type: 'LINE', vertices: [[0, 0]] })).toBeNull()
    expect(entityToPolyline({ id: '2', type: 'LINE', vertices: [[0, 'x'], [1, 1]] })).toBeNull()
    expect(entityToPolyline({ id: '3', type: 'CIRCLE', vertices: [[0, 0]], radius: 0 })).toBeNull()
    expect(entityToPolyline({ id: '4', type: 'CIRCLE', vertices: [[0, 0]] })).toBeNull()
    expect(entityToPolyline({ id: '5', type: 'ARC', vertices: [[0, 0]], radius: 1, startDeg: NaN, endDeg: 90 })).toBeNull()
    expect(entityToPolyline({ id: '6', type: 'OTHER', vertices: [] })).toBeNull()
    expect(entityToPolyline({ id: '6', type: 'OTHER', vertices: [[0, 0], [1, 1]] })).toBeNull()
    expect(entityToPolyline({ id: '7', type: 'INSERT', vertices: [[0, 0], [1, 1]] })).toBeNull()
  })

  it('builds the intake shape the viewer draws, counts points, and truncates honestly past the cap', () => {
    const intake = engineIntake([
      { id: '1', type: 'LINE', layer: 'A', vertices: [[0, 0], [1, 0]] },
      { id: '2', type: 'OTHER', vertices: [] },
      { id: '3', type: 'CIRCLE', layer: 'B', vertices: [[0, 0]], radius: 1 },
    ], 'one.dxf')
    expect(intake.source).toBe('engine')
    expect(intake.documentId).toBe('one.dxf')
    expect(intake.polylines.map((p) => p.handle)).toEqual(['1', '3'])
    expect(intake.inserts).toEqual([])
    expect(intake.faces3d).toEqual([])
    expect(intake.points).toBe(2 + CIRCLE_SEGMENTS)
    expect(intake.truncated).toBe(0)
    const many = Array.from({ length: Math.ceil(MAX_POINTS / 2) + 5 }, (_, i) => ({ id: String(i), type: 'LINE', vertices: [[i, 0], [i, 1]] }))
    const capped = engineIntake(many)
    expect(capped.points).toBeLessThanOrEqual(MAX_POINTS)
    expect(capped.truncated).toBe(5)
    expect(engineIntake(undefined)).toMatchObject({ polylines: [], points: 0, truncated: 0 })
  })
})

describe('W4g-6d: a polyline bulge draws as its arc', () => {
  it('samples the points between the two vertices along the arc the bulge describes, with the crate\'s conventions', () => {
    // Bulge 1 is a semicircle: centre at the chord's midpoint (5,0), radius 5, counter-clockwise from (0,0)
    // to (10,0), which passes BELOW the chord (the same arc explode() would make: 180..360 degrees).
    const pts = bulgePoints([0, 0, 0], [10, 0, 0], 1, 0)
    expect(pts.length).toBeGreaterThanOrEqual(6)
    for (const p of pts) {
      expect(Math.hypot(p[0] - 5, p[1])).toBeCloseTo(5, 9)
      expect(p[1]).toBeLessThan(0)
      expect(p[2]).toBe(0)
    }
    // A negative bulge takes the other side; a straight or degenerate segment yields nothing.
    for (const p of bulgePoints([0, 0, 0], [10, 0, 0], -1, 0)) expect(p[1]).toBeGreaterThan(0)
    expect(bulgePoints([0, 0, 0], [10, 0, 0], 0, 0)).toEqual([])
    expect(bulgePoints([0, 0, 0], [10, 0, 0], Number.NaN, 0)).toEqual([])
    expect(bulgePoints([3, 3, 0], [3, 3, 0], 1, 0)).toEqual([])
    // The 90-degree fillet a polyline corner writes (tan(pi/8) from (10,8) to (8,10)) bows toward the old corner about (8,8).
    const fillet = bulgePoints([10, 8, 1], [8, 10, 1], Math.tan(Math.PI / 8), 1)
    for (const p of fillet) {
      expect(Math.hypot(p[0] - 8, p[1] - 8)).toBeCloseTo(2, 9)
      expect(p[0] + p[1]).toBeGreaterThan(18)
      expect(p[2]).toBe(1)
    }
  })

  it('the mapper draws a curved polyline with its arcs in place, keeps the closed flag, and ignores a list that does not match', () => {
    const base = { id: '20', type: 'LWPOLYLINE', layer: 'A', closed: true, editable: true, vertices: [[0, 0, 0], [10, 0, 0], [10, 8, 0], [8, 10, 0], [0, 10, 0]], radius: null, startDeg: null, endDeg: null }
    const flat = entityToPolyline(base)
    expect(flat.pts).toHaveLength(5)
    const curved = entityToPolyline({ ...base, bulges: [0, 0, Math.tan(Math.PI / 8), 0, 0] })
    expect(curved.closed).toBe(true)
    expect(curved.pts.length).toBeGreaterThan(5)
    // The vertices themselves are still in the list, in order, with the arc's points between (10,8) and (8,10).
    const at = (p) => curved.pts.findIndex((q) => q[0] === p[0] && q[1] === p[1])
    expect(at([10, 8])).toBe(2)
    expect(at([8, 10])).toBe(curved.pts.length - 2)
    for (const p of curved.pts.slice(3, -2)) expect(Math.hypot(p[0] - 8, p[1] - 8)).toBeCloseTo(2, 9)
    // The closing segment's bulge (on the last vertex) draws too.
    const closing = entityToPolyline({ ...base, vertices: [[0, 0, 0], [10, 0, 0], [10, 10, 0]], bulges: [0, 0, 1] })
    expect(closing.pts.length).toBeGreaterThan(3)
    expect(closing.pts[2]).toEqual([10, 10, 0])
    // Mismatched list: drawn straight, never thrown on.
    expect(entityToPolyline({ ...base, bulges: [1] }).pts).toHaveLength(5)
  })
})
