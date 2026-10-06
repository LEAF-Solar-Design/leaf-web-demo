import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { expandBulgedPolylines } from './engineIntake.js'

import { bulgePoints, ARC_STEP_DEG, CIRCLE_SEGMENTS, DIM_EXT_PAST, MAX_POINTS, MIN_ARC_POINTS, dimensionSchematic, mleaderSchematic, engineIntake, entityToPolyline, arcSweepDeg, formatMeasurement, hexHandle, intakeRoundPolylines } from './engineIntake.js'

const near = (a, b, eps = 1e-9) => Math.abs(a - b) < eps

describe('engine text glyphs', () => {
  const text = { id: '26', type: 'TEXT', layer: 'Notes', vertices: [[10, 20, 0]], text: 'A', height: 2, rotationDeg: 0 }
  const dimension = (dimtype = 'LINEAR', measurement = 3) => ({ id: '500', type: 'DIMENSION', dimtype, layer: '0',
    def1: [0, 0], def2: [3, 4], dimline: [1.5, 6], rotationDeg: 0, measurement })

  it('W21D2-text-route', () => {
    const intake = engineIntake([text])
    expect(intake).toMatchObject({ points: 13, truncated: 0 })
    expect(intake.polylines.map((pl) => pl.pts.length)).toEqual([9, 4])
    expect(intake.polylines.every((pl) => pl.handle === '1A' && pl.layer === 'Notes' && pl.strokeOnly && !pl.closed)).toBe(true)
    expect(near(intake.polylines[0].pts[0][0], 11.788746298124384)).toBe(true)
    const rotated = engineIntake([{ ...text, rotationDeg: 90 }])
    expect(near(rotated.polylines[0].pts[0][0], 10)).toBe(true)
    expect(near(rotated.polylines[0].pts[0][1], 21.788746298124384)).toBe(true)
    expect(engineIntake([{ ...text, text: 'A'.repeat(1024) }])).toMatchObject({ points: 13312, truncated: 0 })
    expect(engineIntake([{ ...text, text: 'A'.repeat(1025) }])).toMatchObject({ polylines: [], points: 0, truncated: 1 })
    expect(engineIntake([{ ...text, text: '\u{1F680}' }]).points).toBe(64)
    expect(engineIntake([{ ...text, rotationDeg: NaN, layer: '' }]).polylines[0]).toMatchObject({ layer: '0', pts: intake.polylines[0].pts })
    for (const patch of [{ text: '' }, { text: ' ' }, { height: 0 }, { vertices: [] }, { text: 'A'.repeat(1025), height: 0 }]) {
      expect(engineIntake([{ ...text, ...patch }])).toMatchObject({ polylines: [], points: 0, truncated: 0 })
    }
  })

  it('W21D2-dimension-values', () => {
    for (const [kind, value, points, first] of [
      ['LINEAR', 3, 110, [1.668558736426456, 6.440177690029615, 0]],
      ['ALIGNED', 5, 82, [-0.2511944718657453, 3.8949851924975323, 0]],
    ]) {
      const entity = dimension(kind, value)
      const intake = engineIntake([entity])
      expect(intake).toMatchObject({ points, truncated: 0 })
      expect(intake.polylines).toHaveLength(6)
      expect(intake.polylines.slice(0, 5)).toEqual(dimensionSchematic(entity).slice(0, 5))
      expect(intake.polylines[5]).toMatchObject({ handle: '1F4', layer: '0', closed: false, strokeOnly: true })
      for (let c = 0; c < 3; c++) expect(near(intake.polylines[5].pts[0][c], first[c])).toBe(true)
    }
  })

  it('W21D2-preserve-helper-contracts', () => {
    expect(entityToPolyline(text)).toEqual({ handle: '1A', layer: 'Notes', closed: true,
      pts: [[10, 20, 0], [11.2, 20, 0], [11.2, 22, 0], [10, 22, 0]] })
    const pieces = dimensionSchematic(dimension())
    expect(pieces).toHaveLength(6)
    expect(pieces.reduce((sum, pl) => sum + pl.pts.length, 0)).toBe(14)
    expect(pieces[5]).toMatchObject({ closed: true, pts: [[1.35, 6.3, 0], [1.65, 6.3, 0], [1.65, 6.8, 0], [1.35, 6.8, 0]] })
  })

  it('W21D2-invalid-dimensions', () => {
    const invalid = dimension('LINEAR', NaN)
    expect(engineIntake([invalid])).toMatchObject({ polylines: dimensionSchematic(invalid).slice(0, 5), points: 10, truncated: 0 })
    for (const entity of [dimension('OTHER'), { ...dimension(), def2: null },
      { ...dimension(), def2: [3, 0], rotationDeg: 90 }]) {
      expect(engineIntake([entity])).toMatchObject({ polylines: [], points: 0, truncated: 0 })
    }
  })

  it('W21D2-atomic-budget', () => {
    for (const [entity, size] of [[text, 13], [dimension(), 110]]) {
      const prefix = { id: '1', type: 'LWPOLYLINE', vertices: Array.from({ length: MAX_POINTS - size + 1 }, (_, i) => [i, 0]) }
      const capped = engineIntake([prefix, entity])
      expect(capped).toMatchObject({ points: MAX_POINTS - size + 1, truncated: 1 })
      expect(capped.polylines).toHaveLength(1)
      const fits = engineIntake([{ ...prefix, vertices: prefix.vertices.slice(1) }, entity])
      expect(fits).toMatchObject({ points: MAX_POINTS, truncated: 0 })
      expect(fits.polylines).toHaveLength(entity.type === 'TEXT' ? 3 : 7)
    }
  })

  it('W21D2-mixed-document', () => {
    const intake = engineIntake([text, { id: '27', type: 'LINE', vertices: [[0, 0], [1, 0]] }, dimension()])
    expect(intake).toMatchObject({ points: 125, truncated: 0 })
    expect(intake.polylines).toHaveLength(9)
    expect(intake.polylines.map((pl) => pl.handle)).toEqual(['1A', '1A', '1B', '1F4', '1F4', '1F4', '1F4', '1F4', '1F4'])
  })
})

describe('intake bulges for the console viewer', () => {
  it('carries strokeOnly into the pick descriptor and both highlight loops', () => {
    const source = readFileSync(path.resolve(process.cwd(), 'src/components/Viewer.jsx'), 'utf8')
    expect(source).toContain("{ kind: 'poly', layer: pl.layer, pts, strokeOnly: !!pl.strokeOnly }")
    expect((source.match(/d\.strokeOnly/g) || []).length).toBeGreaterThanOrEqual(2)
  })
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
      expect(intake.points).toBe(110)
      expect(intake.polylines[5]).toMatchObject({ closed: false, strokeOnly: true })
      expect(intake.polylines[5].pts).toHaveLength(100)
      expect(intake.polylines).not.toContainEqual(dimensionSchematic(dim)[5])
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

describe('W4g-bulge-plane-aware: a bulged segment samples in its own OCS plane', () => {
  it('a. +Z is byte-identical, with or without a [0,0,1] normal key', () => {
    const plain = bulgePoints([0, 0, 0], [10, 0, 0], 1, 0)
    expect(bulgePoints([0, 0, 0], [10, 0, 0], 1, 0, [0, 0, 1])).toEqual(plain)
    expect(bulgePoints([0, 0, 0], [10, 0, 0], 1, 0, undefined)).toEqual(plain)
  })

  it('b. Reflected matches main (A\'s own two-vertex case) within floating-point noise', () => {
    const pl = { layer: 'A', handle: '11', closed: false, pts: [[0, 0, -3], [-2, 0, -3]], bulges: [-1, 0] }
    const today = expandBulgedPolylines([pl])[0].pts
    const reflected = expandBulgedPolylines([{ ...pl, normal: [0, 0, -1] }])[0].pts
    expect(reflected).toHaveLength(today.length)
    // The mirror and sign flip cancel to the same geometry, but reach it through
    // an extra matrix multiply: measured max abs diff per component is 5.8e-16.
    for (let i = 0; i < today.length; i++) {
      for (let c = 0; c < 3; c++) {
        expect(Math.abs(reflected[i][c] - today[i][c])).toBeLessThan(1e-12)
      }
    }
  })

  // The frozen basis (A's own): N = (0, 0.6, 0.8) maps OCS (x, y, e) to WCS
  // (-x, -0.8y + 0.6e, 0.6y + 0.8e). OCS (0,0),(2,0) at elevation 3 is WCS
  // [0,1.8,2.4],[-2,1.8,2.4]; bulge 1 (Nz > 0, so the stored bulge is the OCS
  // bulge unchanged) is the lower semicircle whose OCS midpoint (1,-1,3) is
  // WCS (-1, 2.6, 1.8).
  const tiltedNormal = [0, 0.6, 0.8]
  const tiltedPl = { layer: 'A', handle: '12', closed: false, pts: [[0, 1.8, 2.4], [-2, 1.8, 2.4]], bulges: [1, 0], normal: tiltedNormal }

  it('c. A tilted arc lands in the polyline\'s plane, at the hand-derived midpoint', () => {
    const pts = expandBulgedPolylines([tiltedPl])[0].pts
    expect(pts.some((p) => near(p[0], -1) && near(p[1], 2.6) && near(p[2], 1.8))).toBe(true)
    // Both vertices sit at z = 2.4 by construction, so a whole-list z != 2.4
    // check is false by construction; restrict to the interior arc samples.
    const [v0, v1] = tiltedPl.pts
    const interior = pts.filter((p) => !(near(p[0], v0[0]) && near(p[1], v0[1]) && near(p[2], v0[2])) &&
      !(near(p[0], v1[0]) && near(p[1], v1[1]) && near(p[2], v1[2])))
    expect(interior.every((p) => !near(p[2], 2.4))).toBe(true)
  })

  it('d. Every sample lies on the polyline\'s own plane', () => {
    const v0 = tiltedPl.pts[0]
    const pts = expandBulgedPolylines([tiltedPl])[0].pts
    for (const p of pts) {
      const dot = (p[0] - v0[0]) * tiltedNormal[0] + (p[1] - v0[1]) * tiltedNormal[1] + (p[2] - v0[2]) * tiltedNormal[2]
      expect(Math.abs(dot)).toBeLessThan(1e-9)
    }
  })

  it('e. The endpoints still meet: no sample repeats a vertex, and the tail chord matches the arc\'s own step', () => {
    const [v0, v1] = tiltedPl.pts
    const pts = expandBulgedPolylines([tiltedPl])[0].pts
    expect(pts[0]).toEqual(v0)
    expect(pts[pts.length - 1]).toEqual(v1)
    for (const p of pts.slice(1, -1)) {
      expect(p).not.toEqual(v0)
      expect(p).not.toEqual(v1)
    }
    const chord = (p, q) => Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2])
    const step = chord(pts[1], pts[2])
    const tail = chord(pts[pts.length - 2], pts[pts.length - 1])
    expect(tail).toBeCloseTo(step, 6)
  })

  it('f. Degenerate normals (zero, non-finite, near-+Z, malformed) keep today\'s behaviour and never throw', () => {
    const a = [0, 0, 0]; const b = [10, 0, 0]; const bulge = 1; const z = 0
    const today = bulgePoints(a, b, bulge, z)
    const degenerate = [
      [0, 0, 0], [NaN, 0, 1], [0, Infinity, 1], [-Infinity, 0, 1],
      [1e-9, -1e-9, 1 + 1e-9], undefined, null, 'nope', [0, 0], [0, 0, 1, 0],
    ]
    for (const normal of degenerate) {
      expect(() => bulgePoints(a, b, bulge, z, normal)).not.toThrow()
      expect(bulgePoints(a, b, bulge, z, normal)).toEqual(today)
    }
  })
})

// ARC-SWEEP: an arc's sweep is computed in constant time and exactly at any magnitude. Past
// four turns the sweep is the difference of the two angles' exact residues (% is exact for
// doubles) and the samples start from the start angle's residue, so an ARC whose angles are huge
// draws the arc those angles name instead of looping forever, losing the smaller angle to
// rounding, or collapsing onto its start. Every expected value is the same arc drawn with its
// angles already reduced, a hand-derived point, or the base sampler copied as an oracle.
describe('ARC-SWEEP bounded arc sampling', () => {
  // The base sampler, copied as the oracle for differences up to four turns.
  function legacyArcPoints(cx, cy, z, r, startDeg, endDeg) {
    let sweep = endDeg - startDeg
    while (sweep <= 0) sweep += 360
    while (sweep > 360) sweep -= 360
    const n = Math.max(MIN_ARC_POINTS, Math.ceil(sweep / ARC_STEP_DEG) + 1)
    const pts = new Array(n)
    for (let i = 0; i < n; i += 1) {
      const a = ((startDeg + (sweep * i) / (n - 1)) * Math.PI) / 180
      pts[i] = [cx + r * Math.cos(a), cy + r * Math.sin(a), z]
    }
    return pts
  }
  // Integer-valued doubles: BigInt gives their exact residues without the product's float path.
  const residue = (deg) => Number(BigInt(deg) % 360n)
  const roundArc = (start, end) => intakeRoundPolylines({ arcs: [{ handle: 'A', layer: 'L', c: [1, 2, 0], r: 3, start_deg: start, end_deg: end }] })
  const engineArc = (start, end) => ({ id: '7', type: 'ARC', layer: 'L', closed: false, vertices: [[1, 2, 0]], radius: 3, startDeg: start, endDeg: end })

  it('AS1 a difference of many turns draws the reduced arc', () => {
    expect(roundArc(10, 10 + 360 * 1e6 + 30)).toEqual(roundArc(10, 40))
    expect(entityToPolyline(engineArc(10, 10 + 360 * 1e6 + 30))).toEqual(entityToPolyline(engineArc(10, 40)))
  })

  it('AS2 a difference of 1e300 returns the reduced arc', () => {
    const got = roundArc(0, 1e300)
    const mapped = entityToPolyline(engineArc(0, 1e300))
    expect(got).toEqual(roundArc(0, 1e300 % 360))
    expect(mapped).toEqual(entityToPolyline(engineArc(0, 1e300 % 360)))
    expect(got[0].pts.length).toBeGreaterThanOrEqual(MIN_ARC_POINTS)
  })

  it('AS3 a difference that overflows draws the arc between the two residues', () => {
    const r = residue(1.7e308)
    expect(1.7e308 % 360).toBe(r)
    expect(roundArc(-1.7e308, 1.7e308)).toEqual(roundArc(-r, r))
    expect(entityToPolyline(engineArc(-1.7e308, 1.7e308))).toEqual(entityToPolyline(engineArc(-r, r)))
    const both = intakeRoundPolylines({ arcs: [
      { handle: 'X', layer: 'L', c: [1, 2, 0], r: 3, start_deg: -1.7e308, end_deg: 1.7e308 },
      { handle: 'A', layer: 'L', c: [1, 2, 0], r: 3, start_deg: 10, end_deg: 40 },
    ] })
    expect(both.map((p) => p.handle)).toEqual(['X', 'A'])
    expect(both[1]).toEqual(roundArc(10, 40)[0])
  })

  it('AS4 differences up to four turns draw exactly as before', () => {
    let state = 0x5a17
    const rnd = () => { state = (Math.imul(1664525, state) + 1013904223) >>> 0; return state / 4294967296 }
    for (let k = 0; k < 2000; k += 1) {
      const start = 1440 * rnd() - 720
      const end = start + 2880 * rnd() - 1440
      const [got] = roundArc(start, end)
      expect(got.pts).toEqual(legacyArcPoints(1, 2, 0, 3, start, end))
    }
  })

  it('AS5 the four-turn boundary keeps the loop on its own side', () => {
    for (const d of [1440, -1440, 1080.5, -1079.25, 0, 360]) {
      expect(roundArc(5, 5 + d)[0].pts).toEqual(legacyArcPoints(1, 2, 0, 3, 5, 5 + d))
    }
  })

  it('AS6 a huge start draws from its exact residue, not collapsed onto the start', () => {
    // 1e19 mod 360 is 280 by hand (0 mod 40, 1 mod 9): the arc runs 280 -> 90, sweep 170.
    expect(residue(1e19)).toBe(280)
    const [got] = roundArc(1e19, 90)
    expect(got).toEqual(roundArc(280, 90)[0])
    expect(entityToPolyline(engineArc(1e19, 90))).toEqual(entityToPolyline(engineArc(280, 90)))
    const at = (deg) => [1 + 3 * Math.cos((deg * Math.PI) / 180), 2 + 3 * Math.sin((deg * Math.PI) / 180)]
    const first = got.pts[0]
    const last = got.pts[got.pts.length - 1]
    expect(Math.abs(first[0] - at(280)[0])).toBeLessThan(1e-9)
    expect(Math.abs(first[1] - at(280)[1])).toBeLessThan(1e-9)
    expect(Math.abs(last[0] - 1)).toBeLessThan(1e-9)
    expect(Math.abs(last[1] - 5)).toBeLessThan(1e-9)
  })

  // Exact rationals for doubles: x * 2^1074 as a BigInt, so residues and differences are exact.
  function scaled(x) {
    const view = new DataView(new ArrayBuffer(8))
    view.setFloat64(0, x)
    const hi = view.getUint32(0)
    const sign = hi >>> 31 ? -1n : 1n
    const exp = (hi >>> 20) & 0x7ff
    const frac = (BigInt(hi & 0xfffff) << 32n) | BigInt(view.getUint32(4))
    if (exp === 0) return sign * frac
    return sign * ((frac | (1n << 52n)) << BigInt(exp - 1))
  }
  // One ulp of a positive finite double, in the same scale.
  function ulpScaled(x) {
    const view = new DataView(new ArrayBuffer(8))
    view.setFloat64(0, x)
    const exp = (view.getUint32(0) >>> 20) & 0x7ff
    return exp === 0 ? 1n : 1n << BigInt(exp - 1)
  }
  const TURN = 360n << 1074n
  // The true counter-clockwise sweep between the two angles' exact residues, in (0, TURN].
  function exactSweep(start, end) {
    let d = (scaled(end) % TURN) - (scaled(start) % TURN)
    while (d <= 0n) d += TURN
    while (d > TURN) d -= TURN
    return d
  }

  it('AS7 a sliver past four turns stays a sliver, either sign', () => {
    // -79.99999999999999 is exactly -80 + 2^-46 and 1e19 mod 360 is 280: the true sweep is 2^-46,
    // and the rounded residue difference (-360) would draw the whole circle.
    const sliver = 2 ** -46
    expect(-79.99999999999999).toBe(-80 + sliver)
    expect(arcSweepDeg(1e19, -79.99999999999999)).toBe(sliver)
    expect(arcSweepDeg(-80.00000000000001, 1e19)).toBe(sliver)
    const near280 = [1 + 3 * Math.cos((280 * Math.PI) / 180), 2 + 3 * Math.sin((280 * Math.PI) / 180)]
    for (const [start, end] of [[1e19, -79.99999999999999], [-80.00000000000001, 1e19]]) {
      const [got] = roundArc(start, end)
      expect(got.pts.length).toBe(MIN_ARC_POINTS)
      for (const p of got.pts) {
        expect(Math.abs(p[0] - near280[0])).toBeLessThan(1e-9)
        expect(Math.abs(p[1] - near280[1])).toBeLessThan(1e-9)
      }
      expect(entityToPolyline(engineArc(start, end)).pts.length).toBe(MIN_ARC_POINTS)
    }
  })

  it('AS8 the four-turn boundary keeps the base rule where the two rules disagree', () => {
    // -1e-14 to 1440 rounds to a difference of exactly 1440, which the base rule (and this one up
    // to four turns) reads as a full circle; the residue rule would read the 1e-14 sliver. Drawing
    // the full circle here pins which side of the boundary the case takes.
    expect(1440 - -1e-14).toBe(1440)
    expect(arcSweepDeg(-1e-14, 1440)).toBe(360)
    expect(roundArc(-1e-14, 1440)[0].pts).toEqual(legacyArcPoints(1, 2, 0, 3, -1e-14, 1440))
    expect(roundArc(-1e-14, 1440)[0].pts.length).toBe(Math.ceil(360 / ARC_STEP_DEG) + 1)
  })

  it('AS9 past four turns the sweep is the exact sweep rounded once (BigInt oracle)', () => {
    let state = 0x7e31
    const rnd = () => { state = (Math.imul(1664525, state) + 1013904223) >>> 0; return state / 4294967296 }
    const huge = [1e19, -1e19, 2 ** 60 + 4096, -(2 ** 70), 1e300, -1e300, 1.7e308, -1.7e308, 1e17, 3.3e22]
    const offsets = [0, 2 ** -46, -(2 ** -46), 2 ** -60, -(2 ** -60), 5e-324, -5e-324, 1e-300, 1e-14]
    // Measured witnesses where adding 360 to a small negative residue difference rounds, so the
    // rounding error of that addition must be carried to stay within half an ulp.
    const witnesses = [[6.404326603412502e-7, -4.788203013917946e+112], [4.2980020149344314e+172, 4.890027472723243e-13],
      [-4.210455386748698e-11, -6.081700334532813e+163], [-0.000004379423019229556, -6.038752527121911e+286]]
    let checked = 0
    const check = (start, end) => {
      if (Math.abs(end - start) <= 1440) return
      const got = arcSweepDeg(start, end)
      expect(got > 0 && got <= 360).toBe(true)
      const err = scaled(got) - exactSweep(start, end)
      expect(2n * (err < 0n ? -err : err) <= ulpScaled(got)).toBe(true)
      checked += 1
    }
    for (const [start, end] of witnesses) check(start, end)
    for (const s of huge) {
      const rs = s % 360
      for (const off of offsets) {
        for (const k of [-360, 0, 360]) {
          check(s, rs + k + off)
          check(rs + k + off, s)
        }
      }
    }
    for (let i = 0; i < 1000; i += 1) {
      const s = (rnd() < 0.5 ? -1 : 1) * rnd() * 10 ** (4 + Math.floor(rnd() * 300))
      const e = (rnd() < 0.5 ? -1 : 1) * rnd() * 10 ** (4 + Math.floor(rnd() * 300))
      check(s, e)
      check(s, (s % 360) + 360 * Math.floor(rnd() * 3 - 1) + (rnd() - 0.5) * 2 ** -40)
    }
    expect(checked).toBeGreaterThan(1500)
  })
})
