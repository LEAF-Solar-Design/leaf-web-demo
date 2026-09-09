import { describe, expect, it } from 'vitest'
import { expandBulgedPolylines } from './engineIntake.js'

import { bulgePoints, ARC_STEP_DEG, CIRCLE_SEGMENTS, DIM_EXT_PAST, MAX_POINTS, MIN_ARC_POINTS, dimensionSchematic, mleaderSchematic, engineIntake, entityToPolyline, formatMeasurement, hexHandle } from './engineIntake.js'

const near = (a, b, eps = 1e-9) => Math.abs(a - b) < eps

describe('intake bulges for the console viewer', () => {
  const open = { layer: 'A', handle: '10', closed: false, pts: [[0, 0, 2], [10, 0, 2]] }
  it('preserves array identity for absent and zero bulges', () => {
    for (const rows of [[], [open], [{ ...open, bulges: [0, 0] }]]) {
      expect(expandBulgedPolylines(rows)).toBe(rows)
    }
  })
  it('samples the lower semicircle with the existing bulgePoints rule', () => {
    const pl = { ...open, bulges: [1, 0] }
    const rows = [pl]
    const expanded = expandBulgedPolylines(rows)
    expect(expanded).not.toBe(rows)
    expect(expanded[0]).toMatchObject({ layer: 'A', handle: '10', closed: false })
    expect(expanded[0].strokeOnly).toBe(true)
    expect(pl).not.toHaveProperty('strokeOnly')
    const pts = expanded[0].pts
    expect(pts).toHaveLength(25)
    expect(pts).toEqual([open.pts[0], ...bulgePoints(...open.pts, 1, 2), open.pts[1]])
    expect(pts[0]).toEqual([0, 0, 2])
    expect(pts[24]).toEqual([10, 0, 2])
    expect(near(pts[12][0], 5) && near(pts[12][1], -5)).toBe(true)
    expect(pts.every((p) => p[2] === 2)).toBe(true)
    expect(pl.pts).toBe(open.pts)
  })
  it('samples the closing segment without duplicating join vertices', () => {
    const pl = { ...open, closed: true, pts: [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]], bulges: [0, 0, 0, 1] }
    const expanded = expandBulgedPolylines([pl])[0]
    expect(expanded).not.toHaveProperty('strokeOnly')
    const pts = expanded.pts
    expect(pts).toEqual([...pl.pts, ...bulgePoints(pl.pts[3], pl.pts[0], 1, 0)])
    expect(pts).toHaveLength(27)
    expect(near(pts[15][0], -5) && near(pts[15][1], 5)).toBe(true)
  })
  it('leaves malformed bulge lists as chords without throwing', () => {
    for (const bulges of [[1], [NaN, 0], [Infinity, 0], ['1', 0]]) {
      const pl = { ...open, bulges }
      const rows = [pl]
      expect(() => expandBulgedPolylines(rows)).not.toThrow()
      expect(expandBulgedPolylines(rows)[0].pts).toBe(pl.pts)
    }
  })
  it('keeps an untouched neighboring record by identity', () => {
    const rows = [{ ...open, bulges: [1, 0] }, open]
    const expanded = expandBulgedPolylines(rows)
    expect(expanded[0]).not.toBe(rows[0])
    expect(expanded[1]).toBe(open)
    expect(expanded[1]).not.toHaveProperty('strokeOnly')
  })
})

describe('MLEADER schematic, v87 hand-derived geometry', () => {
  const base = { id: '37986', type: 'MLEADER', layer: 'Leaders', text: 'Valve', height: 1, arrow: 0.5, dogleg: 2, textLocation: [5.1, 4.5] }
  it.each([
    { landing: [3, 4], dir: [1, 0], end: [5, 4, 0], corners: [[0.1, 0.55, 0], [0.5, 0.25, 0]] },
    { landing: [-3, 4], dir: [-1, 0], end: [-5, 4, 0], corners: [[-0.5, 0.25, 0], [-0.1, 0.55, 0]] },
    { landing: [4, 0], dir: [1, 0], end: [6, 0, 0], corners: [[0.5, 0.25, 0], [0.5, -0.25, 0]] },
  ])('draws the arrow and dogleg for $landing', ({ landing, dir, end, corners }) => {
    const entity = { ...base, vertices: [[0, 0], landing], dogleg_dir: dir }
    const pieces = mleaderSchematic(entity)
    expect(pieces).toHaveLength(4)
    expect(pieces[0].pts).toEqual([[0, 0, 0], [...landing, 0]])
    expect(pieces[1].pts).toEqual([[...landing, 0], end])
    expect(Math.hypot(end[0] - landing[0], end[1] - landing[1])).toBe(2)
    expect(pieces[2]).toMatchObject({ closed: true, pts: [[0, 0, 0], ...corners] })
    expect(pieces[3]).toMatchObject({ closed: true, pts: [[5.1, 4.5, 0], [8.1, 4.5, 0], [8.1, 5.5, 0], [5.1, 5.5, 0]] })
    expect(pieces.every((p) => p.handle === '9462' && p.layer === 'Leaders')).toBe(true)
    expect(engineIntake([entity]).polylines).toEqual(pieces)
  })
  it('uses the projected text side without an explicit dogleg direction and skips malformed records', () => {
    const entity = { ...base, vertices: [[0, 0], [-3, 4]], textLocation: [-5.1, 4.5] }
    expect(mleaderSchematic(entity)[1].pts[1]).toEqual([-5, 4, 0])
    for (const bad of [null, {}, { ...entity, vertices: [[0, 0], [0, 0]] }, { ...entity, height: NaN }, { ...entity, textLocation: null }]) {
      expect(mleaderSchematic(bad)).toEqual([])
    }
  })
})

describe('engineIntake (W4f slice A0): engine entities -> viewer intake', () => {
  it('carries dictionary names and converts group and member ids to hex', () => {
    const entities = Object.assign([], { groups: [{ id: '240', name: 'RACK', memberIds: ['10', '32'], unnamed: false, selectable: true, description: '' }] })
    expect(engineIntake(entities).groups).toEqual([{ handle: 'F0', name: 'RACK', flags: 0, selectable: true, description: '', members: ['A', '20'] }])
  })
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

  describe('W4g-7b-04c: dimensionSchematic, the hand-derived rows', () => {
    it('formats a measurement to 3 decimals, trailing zeros trimmed', () => {
      expect(formatMeasurement(3)).toBe('3')
      expect(formatMeasurement(5)).toBe('5')
      expect(formatMeasurement(4.1)).toBe('4.1')
      expect(formatMeasurement(4.12345)).toBe('4.123')
      expect(formatMeasurement(Number.NaN)).toBe('')
    })

    it('ALIGNED (0,0)-(3,4), dimline (1.5,6): the dimension line is the chord through (1.08,5.44)-(-1.92,1.44), measurement 5', () => {
      const entity = { id: '500', type: 'DIMENSION', dimtype: 'ALIGNED', layer: '0', def1: [0, 0], def2: [3, 4], dimline: [1.5, 6], rotationDeg: 0, measurement: 5 }
      const pieces = dimensionSchematic(entity)
      expect(pieces).toHaveLength(6)
      const [ext1, ext2, dimLine] = pieces
      expect(dimLine.pts[0]).toEqual([-1.92, 1.44, 0])
      expect(dimLine.pts[1]).toEqual([1.08, 5.44, 0])
      expect(Math.hypot(dimLine.pts[1][0] - dimLine.pts[0][0], dimLine.pts[1][1] - dimLine.pts[0][1])).toBeCloseTo(5, 9)
      expect(ext1.pts[0]).toEqual([0, 0, 0])
      expect(ext2.pts[0]).toEqual([3, 4, 0])
      // Every extension line runs DIM_EXT_PAST past its foot.
      expect(Math.hypot(ext1.pts[1][0] - (-1.92), ext1.pts[1][1] - 1.44)).toBeCloseTo(DIM_EXT_PAST, 9)
      expect(Math.hypot(ext2.pts[1][0] - 1.08, ext2.pts[1][1] - 5.44)).toBeCloseTo(DIM_EXT_PAST, 9)
    })

    it('LINEAR rot 0, dimline (1.5,6): the dimension line is y=6 from x=0 to x=3, measurement 3', () => {
      const entity = { id: '500', type: 'DIMENSION', dimtype: 'LINEAR', layer: '0', def1: [0, 0], def2: [3, 4], dimline: [1.5, 6], rotationDeg: 0, measurement: 3 }
      const [ext1, ext2, dimLine] = dimensionSchematic(entity)
      expect(dimLine.pts).toEqual([[0, 6, 0], [3, 6, 0]])
      expect(ext1.pts[0]).toEqual([0, 0, 0])
      expect(ext2.pts[0]).toEqual([3, 4, 0])
    })

    it('LINEAR rot 90, dimline (1.5,6): the dimension line is x=1.5 from y=0 to y=4, measurement 4', () => {
      const entity = { id: '500', type: 'DIMENSION', dimtype: 'LINEAR', layer: '0', def1: [0, 0], def2: [3, 4], dimline: [1.5, 6], rotationDeg: 90, measurement: 4 }
      const [, , dimLine] = dimensionSchematic(entity)
      expect(dimLine.pts).toEqual([[1.5, 0, 0], [1.5, 4, 0]])
    })

    it('draws the same line for a raw or a canonical dimline point (the console fact)', () => {
      const raw = { id: '500', type: 'DIMENSION', dimtype: 'ALIGNED', layer: '0', def1: [0, 0], def2: [3, 4], dimline: [1.5, 6], measurement: 5 }
      const canonical = { ...raw, dimline: [1.08, 5.44] }
      const hand = [[-1.92, 1.44, 0], [1.08, 5.44, 0]]
      expect(dimensionSchematic(raw)[2].pts).toEqual(hand)
      expect(dimensionSchematic(canonical)[2].pts).toEqual(hand)
    })

    it('an OTHER dimtype draws nothing through engineIntake, a RADIUS reads as visible-by-handle only', () => {
      const other = { id: '500', type: 'DIMENSION', dimtype: 'OTHER', layer: '0', editable: false }
      expect(engineIntake([other]).polylines).toEqual([])
    })

    it('engineIntake draws a LINEAR dimension\'s schematic pieces, keyed by its own hex handle', () => {
      const dim = { id: '37986', type: 'DIMENSION', dimtype: 'LINEAR', layer: '0', def1: [0, 0], def2: [3, 4], dimline: [1.5, 6], rotationDeg: 0, measurement: 3 }
      const intake = engineIntake([dim])
      expect(intake.polylines).toHaveLength(6)
      expect(intake.polylines.every((p) => p.handle === '9462')).toBe(true)
    })

    it('a malformed dimension record draws nothing, never throws', () => {
      expect(dimensionSchematic({ dimtype: 'ALIGNED', def1: [0, 0], def2: null, dimline: [1, 1] })).toEqual([])
      expect(dimensionSchematic({ dimtype: 'ALIGNED', def1: [0, 0], def2: [0, 0], dimline: [1, 1] })).toEqual([])
    })

    it('W4g-7b-04c-3 F3a: a LINEAR whose rotation projects the definition points to nothing draws nothing, never a zero-length line', () => {
      expect(dimensionSchematic({ dimtype: 'LINEAR', def1: [0, 0], def2: [3, 0], dimline: [1.5, 6], rotationDeg: 90, measurement: 0 })).toEqual([])
      // The same two points at a rotation that DOES project still draw.
      expect(dimensionSchematic({ dimtype: 'LINEAR', def1: [0, 0], def2: [3, 4], dimline: [1.5, 6], rotationDeg: 90, measurement: 4 })).toHaveLength(6)
    })
  })
})
