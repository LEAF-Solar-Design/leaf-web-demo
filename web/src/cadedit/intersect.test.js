// W4g-6: the intersection kernel behind TRIM / EXTEND / FILLET / CHAMFER,
// pure rows. Every expected number is derived by hand in the comment beside it.
import { describe, expect, it } from 'vitest'

import {
  MAX_BATCH_STEPS, MAX_INTERSECT_POINTS, chamferLines, crossings, curveOf, extendEntity, filletLines,
  locate, nearestEntity, trimEntity,
} from './intersect.js'

const line = (id, a, b, layer = 'A') => ({ id, type: 'LINE', layer, closed: false, vertices: [[...a, 0], [...b, 0]], radius: null, startDeg: null, endDeg: null, editable: true })
const poly = (id, pts, closed, layer = 'A') => ({ id, type: 'LWPOLYLINE', layer, closed, vertices: pts.map((p) => [...p, 0]), radius: null, startDeg: null, endDeg: null, editable: true })
const circle = (id, c, r) => ({ id, type: 'CIRCLE', layer: 'A', closed: true, vertices: [[...c, 0]], radius: r, startDeg: null, endDeg: null, editable: true })
const arc = (id, c, r, s, e) => ({ id, type: 'ARC', layer: 'A', closed: false, vertices: [[...c, 0]], radius: r, startDeg: s, endDeg: e, editable: true })
const text = (id) => ({ id, type: 'TEXT', layer: 'A', closed: false, vertices: [[1, 1, 0]], radius: null, startDeg: null, endDeg: null, editable: true })

// The horizontal (0,0)-(10,0) and the vertical through x = 5.
const H = () => line('h', [0, 0], [10, 0])
const V = () => line('v', [5, -5], [5, 5])

describe('curveOf and locate', () => {
  it('reads the four kinds and refuses the rest, with the reason', () => {
    expect(curveOf(H()).kind).toBe('LINE')
    expect(curveOf(poly('p', [[0, 0], [1, 0], [1, 1]], true))).toMatchObject({ kind: 'POLY', closed: true })
    expect(curveOf(circle('c', [0, 0], 5))).toMatchObject({ kind: 'CIRCLE', r: 5 })
    // 350 -> 20 sweeps 30 degrees counter-clockwise through 0.
    expect(curveOf(arc('a', [0, 0], 5, 350, 20))).toMatchObject({ kind: 'ARC', start: 350, end: 20, sweep: 30 })
    expect(curveOf(text('t'), 'cutting edge').refusal).toMatch(/a TEXT of this kind cannot be a cutting edge yet/)
    expect(curveOf(line('z', [1, 1], [1, 1])).refusal).toMatch(/zero length/)
    expect(curveOf(circle('c', [0, 0], 0)).refusal).toMatch(/no centre or radius/)
    expect(curveOf({ id: 'n', type: 'LINE', vertices: [[0, 0, 0], [Number.NaN, 0, 0]] }).refusal).toMatch(/not a number/)
  })

  it('bounds the point count: an entity past MAX_INTERSECT_POINTS is refused, never scanned', () => {
    const many = Array.from({ length: MAX_INTERSECT_POINTS + 1 }, (_, i) => [i, 0])
    expect(curveOf(poly('big', many, false)).refusal).toMatch(new RegExp(`more than ${MAX_INTERSECT_POINTS} points`))
    expect(MAX_BATCH_STEPS).toBe(4)
  })

  it('locates a point on a line, a polyline, a circle and an arc', () => {
    // (5, 3) projects onto the horizontal at t = 0.5, 3 away.
    expect(locate(curveOf(H()), [5, 3])).toEqual({ d: 3, s: 0.5 })
    // On the square's third side (10,10)->(0,10), a third of the way: s = 2 + 1/3... (7, 11) is 1 above x = 7.
    const sq = curveOf(poly('sq', [[0, 0], [10, 0], [10, 10], [0, 10]], true))
    const at = locate(sq, [7, 11])
    expect(at.d).toBeCloseTo(1, 12)
    expect(at.s).toBeCloseTo(2.3, 12)
    // (0, 7) is at 90 degrees, 2 outside a radius-5 circle.
    expect(locate(curveOf(circle('c', [0, 0], 5)), [0, 7])).toEqual({ d: 2, s: 90, on: true })
    // On a 0..90 arc, (0, 5) is the end (offset 90) and (-5, 0) is off the sweep, nearest the end.
    const a = curveOf(arc('a', [0, 0], 5, 0, 90))
    expect(locate(a, [0, 5])).toEqual({ d: 0, s: 90, on: true })
    expect(locate(a, [-5, 0])).toMatchObject({ s: 90, on: false })
  })
})

describe('nearestEntity', () => {
  it('finds the nearest other entity within the aperture and nothing outside it', () => {
    const entities = [H(), V(), circle('c', [50, 50], 5), text('t')]
    // (5.05, 3) is 0.05 from the vertical and 3 from the horizontal.
    expect(nearestEntity(entities, 5.05, 3, 0.2, 'h')).toEqual({ id: 'v', d: expect.closeTo(0.05, 9) })
    expect(nearestEntity(entities, 5.05, 3, 0.01, 'h')).toBeNull()
    // The selection itself never counts, even when it is nearest.
    expect(nearestEntity(entities, 2, 0.01, 0.5, 'h')).toBeNull()
    // Bad input is null, never a throw.
    expect(nearestEntity(null, 0, 0, 1)).toBeNull()
    expect(nearestEntity(entities, Number.NaN, 0, 1)).toBeNull()
    expect(nearestEntity(entities, 0, 0, -1)).toBeNull()
  })
})

describe('crossings', () => {
  it('reports a line crossing on the target param and never extends the edge', () => {
    expect(crossings(curveOf(H()), curveOf(V()))).toEqual([{ s: 0.5, p: [5, 0] }])
    // An edge that stops short of the target crosses nowhere.
    expect(crossings(curveOf(H()), curveOf(line('short', [5, 1], [5, 5])))).toEqual([])
    // Extending the target's END finds a boundary past it, as params > 1.
    const hits = crossings(curveOf(line('e', [0, 0], [4, 0])), curveOf(line('b', [10, -5], [10, 5])), 'end')
    expect(hits).toEqual([{ s: 2.5, p: [10, 0] }])
    expect(crossings(curveOf(line('e', [0, 0], [4, 0])), curveOf(line('b', [10, -5], [10, 5])), 'none')).toEqual([])
  })

  it('reports round crossings as angles, on the full circle for an arc target', () => {
    const hits = crossings(curveOf(arc('a', [0, 0], 5, 0, 90)), curveOf(line('x', [-10, 0], [10, 0])))
    expect(hits).toEqual([{ s: 0, p: [5, 0] }, { s: 180, p: [-5, 0] }])
    // Two circles of radius 5 centred 6 apart meet at x = 3, y = +-4.
    const cc = crossings(curveOf(circle('c1', [0, 0], 5)), curveOf(circle('c2', [6, 0], 5)))
    expect(cc.map((h) => h.p.map((v) => Math.round(v * 1e9) / 1e9))).toEqual([[3, 4], [3, -4]])
    // An ARC edge only counts the crossings on its sweep.
    expect(crossings(curveOf(H()), curveOf(arc('a', [5, 0], 3, 0, 180)))).toEqual([{ s: 0.8, p: [8, 0] }, { s: 0.2, p: [2, 0] }].sort((a, b) => a.s - b.s))
  })
})

describe('trimEntity', () => {
  it('cuts a line at the crossing and keeps the part the pick is NOT on', () => {
    expect(trimEntity(H(), V(), 8, 0)).toEqual({ steps: [{ op: 'setVertices', entityId: 'h', points: [[0, 0], [5, 0]], closed: false }] })
    expect(trimEntity(H(), V(), 2, 0)).toEqual({ steps: [{ op: 'setVertices', entityId: 'h', points: [[5, 0], [10, 0]], closed: false }] })
  })

  it('removes the middle of a line between two crossings, keeping both ends', () => {
    // A U-shaped edge crosses at x = 3 and x = 7; the pick at x = 5 removes the middle.
    const u = poly('u', [[3, -5], [3, 5], [7, 5], [7, -5]], false)
    expect(trimEntity(H(), u, 5, 0)).toEqual({
      steps: [
        { op: 'setVertices', entityId: 'h', points: [[0, 0], [3, 0]], closed: false },
        { op: 'createLine', inputs: { x: 7, y: 0, x2: 10, y2: 0, layer: 'A' } },
      ],
    })
  })

  it('trims an open polyline into one shorter polyline, or two', () => {
    // (0,0)->(10,0)->(10,10) cut at (5,0) [s 0.5] and (10,5) [s 1.5]; the pick at (10,2) is between.
    const l = poly('l', [[0, 0], [10, 0], [10, 10]], false, 'Out')
    const edge = poly('e', [[5, -5], [5, 5], [15, 5]], false)
    expect(trimEntity(l, edge, 10, 2)).toEqual({
      steps: [
        { op: 'setVertices', entityId: 'l', points: [[0, 0], [5, 0]], closed: false },
        { op: 'createPolyline', inputs: { pts: '10,5 10,10', closed: false, layer: 'Out' } },
      ],
    })
    // The pick past the second crossing removes the tail only.
    expect(trimEntity(l, edge, 10, 8)).toEqual({ steps: [{ op: 'setVertices', entityId: 'l', points: [[0, 0], [10, 0], [10, 5]], closed: false }] })
  })

  it('opens a closed polyline at the removed piece', () => {
    // The square cut by y = 5 at (10,5) [s 1.5] and (0,5) [s 3.5]; the pick at (10,7) removes the top.
    const sq = poly('sq', [[0, 0], [10, 0], [10, 10], [0, 10]], true)
    expect(trimEntity(sq, line('cut', [-1, 5], [11, 5]), 10, 7)).toEqual({
      steps: [{ op: 'setVertices', entityId: 'sq', points: [[0, 5], [0, 0], [10, 0], [10, 5]], closed: false }],
    })
    // One crossing cannot take a piece out of a loop.
    expect(trimEntity(sq, line('one', [5, 5], [5, 15]), 5, 12).refusal).toMatch(/needs two crossings/)
  })

  it('turns a circle into the arc left after the pick, and trims an arc by angle', () => {
    // A radius-5 circle cut by the x axis: the pick at the bottom removes 180..360, keeping 0..180.
    expect(trimEntity(circle('c', [0, 0], 5), line('cut', [-10, 0], [10, 0]), 0, -5)).toEqual({
      steps: [{ op: 'delete', entityId: 'c' }, { op: 'createArc', inputs: { x: 0, y: 0, r: 5, a0: 0, a1: 180, layer: 'A' } }],
    })
    // A 0..180 arc cut by x = 0 at 90 degrees; the pick at 45 degrees removes 0..90.
    expect(trimEntity(arc('a', [0, 0], 5, 0, 180), line('cut', [0, -10], [0, 10]), 3.5, 3.5)).toEqual({
      steps: [{ op: 'setArc', entityId: 'a', x: 0, y: 0, r: 5, a0: 90, a1: 180 }],
    })
    // The pick at 135 removes 90..180 instead.
    expect(trimEntity(arc('a', [0, 0], 5, 0, 180), line('cut', [0, -10], [0, 10]), -3.5, 3.5)).toEqual({
      steps: [{ op: 'setArc', entityId: 'a', x: 0, y: 0, r: 5, a0: 0, a1: 90 }],
    })
    // Two crossings on an arc and a pick between them: the arc keeps both ends, one as a new arc.
    expect(trimEntity(arc('a', [0, 0], 5, 0, 180), line('cut', [-3, -10], [-3, 10]).id ? poly('cuts', [[3, -10], [3, 10], [-3, 10], [-3, -10]], false) : null, 0, 5)).toEqual({
      steps: [
        { op: 'setArc', entityId: 'a', x: 0, y: 0, r: 5, a0: 0, a1: expect.closeTo(53.130102354, 6) },
        { op: 'createArc', inputs: { x: 0, y: 0, r: 5, a0: expect.closeTo(126.869897646, 6), a1: 180, layer: 'A' } },
      ],
    })
  })

  it('refuses every impossible ask with the sentence, touching nothing', () => {
    expect(trimEntity(H(), line('far', [20, -5], [20, 5]), 2, 0).refusal).toBe('Trim refused: the cutting edge does not cross the selection.')
    expect(trimEntity(H(), V(), 5, 0).refusal).toBe('Trim refused: click on the part to remove, away from the crossing.')
    expect(trimEntity(H(), H(), 2, 0).refusal).toBe('Trim refused: select a different entity as the cutting edge.')
    expect(trimEntity(H(), text('t'), 2, 0).refusal).toMatch(/^Trim refused: a TEXT of this kind cannot be a cutting edge yet/)
    expect(trimEntity(text('t'), H(), 2, 0).refusal).toMatch(/^Trim refused: a TEXT of this kind cannot be a selection yet/)
    expect(trimEntity(H(), V(), Number.NaN, 0).refusal).toMatch(/^Trim refused: the point on the part to remove: x and y must both be numbers/)
    expect(trimEntity(null, V(), 1, 0).refusal).toBe('Trim refused: select an entity and name a second one.')
    // A circle with a tangent edge has one crossing: nothing to remove.
    expect(trimEntity(circle('c', [0, 0], 5), line('tan', [-10, 5], [10, 5]), 0, -5).refusal).toMatch(/needs two crossings/)
  })
})

describe('extendEntity', () => {
  it('lengthens the end nearer the pick to the boundary, in either direction', () => {
    expect(extendEntity(line('e', [0, 0], [4, 0]), line('b', [10, -5], [10, 5]), 4, 0)).toEqual({
      steps: [{ op: 'setVertices', entityId: 'e', points: [[0, 0], [10, 0]], closed: false }],
    })
    expect(extendEntity(line('e', [2, 0], [4, 0]), line('b', [-3, -5], [-3, 5]), 2, 0)).toEqual({
      steps: [{ op: 'setVertices', entityId: 'e', points: [[-3, 0], [4, 0]], closed: false }],
    })
    // The nearest boundary crossing wins when the edge polyline crosses the ray twice.
    const zig = poly('z', [[6, -5], [6, 5], [9, 5], [9, -5]], false)
    expect(extendEntity(line('e', [0, 0], [4, 0]), zig, 4, 0).steps[0].points).toEqual([[0, 0], [6, 0]])
    // An open polyline extends its end segment along its own direction.
    expect(extendEntity(poly('p', [[0, 0], [5, 0], [5, 5]], false), line('b', [-10, 8], [10, 8]), 5, 5)).toEqual({
      steps: [{ op: 'setVertices', entityId: 'p', points: [[0, 0], [5, 0], [5, 8]], closed: false }],
    })
  })

  it('extends an arc along its circle and never into a full turn', () => {
    // 0..90 extended at its end (0,5) to the x axis crossing at 180.
    expect(extendEntity(arc('a', [0, 0], 5, 0, 90), line('b', [-10, 0], [10, 0]), -0.2, 5)).toEqual({
      steps: [{ op: 'setArc', entityId: 'a', x: 0, y: 0, r: 5, a0: 0, a1: 180 }],
    })
    // 0..90 extended at its start (5,0) backwards to the y axis crossing at 270.
    expect(extendEntity(arc('a', [0, 0], 5, 0, 90), line('b', [0, -10], [0, 10]), 5, -0.2)).toEqual({
      steps: [{ op: 'setArc', entityId: 'a', x: 0, y: 0, r: 5, a0: 270, a1: 90 }],
    })
    // 10..350 extended at its end reaches the boundary's crossing at (5,0), through zero: end 0, sweep 350.
    expect(extendEntity(arc('a', [0, 0], 5, 10, 350), line('b', [0, -10], [10, 10]), 4.9, -0.9)).toEqual({
      steps: [{ op: 'setArc', entityId: 'a', x: 0, y: 0, r: 5, a0: 10, a1: 0 }],
    })
    // 0..270 extended at its end (0,-5) toward a boundary tangent at its own START (5,0): the full turn.
    expect(extendEntity(arc('a', [0, 0], 5, 0, 270), line('b', [5, -10], [5, 10]), 0, -5).refusal).toBe('Extend refused: extending that far would close the arc into a full turn.')
    // A boundary crossing the arc ITSELF lies behind the end, not ahead of it.
    expect(extendEntity(arc('a', [0, 0], 5, 0, 270), line('b', [0, -10], [0, 10]), 0, -5).refusal).toBe('Extend refused: the boundary edge does not lie ahead of that end.')
    // Even one degree before the end (a radial boundary through 269 degrees), with a picking aperture in play.
    const radial = line('r', [0, 0], [10 * Math.cos(269 * Math.PI / 180), 10 * Math.sin(269 * Math.PI / 180)])
    expect(extendEntity(arc('a', [0, 0], 5, 0, 270), radial, 0, -5, 0.2).refusal).toBe('Extend refused: the boundary edge does not lie ahead of that end.')
  })

  it('refuses what has no end and what lies behind', () => {
    expect(extendEntity(line('e', [0, 0], [4, 0]), line('b', [-3, -5], [-3, 5]), 4, 0).refusal).toBe('Extend refused: the boundary edge does not lie ahead of that end.')
    expect(extendEntity(circle('c', [0, 0], 5), H(), 0, 5).refusal).toBe('Extend refused: a circle has no end to extend.')
    expect(extendEntity(poly('sq', [[0, 0], [1, 0], [1, 1]], true), H(), 0, 0).refusal).toBe('Extend refused: a closed polyline has no end to extend.')
    expect(extendEntity(H(), H(), 0, 0).refusal).toBe('Extend refused: select a different entity as the boundary edge.')
  })
})

describe('filletLines', () => {
  it('rounds a 90-degree corner: tangent points r back along each kept side, the MINOR arc between', () => {
    // x: (0,0)-(10,0), y: (10,0)-(10,10), r = 2: tangents (8,0) and (10,2), centre (8,2), arc 270 -> 0.
    expect(filletLines(line('x', [0, 0], [10, 0]), line('y', [10, 0], [10, 10]), 2, 2, 0, 10, 8)).toEqual({
      steps: [
        { op: 'setVertices', entityId: 'x', points: [[0, 0], [8, 0]], closed: false },
        { op: 'setVertices', entityId: 'y', points: [[10, 2], [10, 10]], closed: false },
        { op: 'createArc', inputs: { x: 8, y: 2, r: 2, a0: 270, a1: 0, layer: 'A' } },
      ],
    })
    // The other side of the second line: centre (8,-2), arc 0 -> 90 (the minor arc again).
    expect(filletLines(line('x', [0, 0], [10, 0]), line('y', [10, -10], [10, 10]), 2, 2, 0, 10, -8).steps[2]).toEqual({
      op: 'createArc', inputs: { x: 8, y: -2, r: 2, a0: 0, a1: 90, layer: 'A' },
    })
  })

  it('extends a line that stops short of the corner, and r = 0 makes the sharp corner', () => {
    // x stops at 8, y starts at (10,2): both reach the crossing (10,0) with r = 0.
    expect(filletLines(line('x', [0, 0], [8, 0]), line('y', [10, 2], [10, 10]), 0, 2, 0, 10, 8)).toEqual({
      steps: [
        { op: 'setVertices', entityId: 'x', points: [[0, 0], [10, 0]], closed: false },
        { op: 'setVertices', entityId: 'y', points: [[10, 0], [10, 10]], closed: false },
      ],
    })
  })

  it('keeps the orientation of each line and picks the kept side from the point', () => {
    // The first line drawn right-to-left: its start is the kept end, so the tangent replaces the END.
    const out = filletLines(line('x', [10, 0], [0, 0]), line('y', [10, 0], [10, 10]), 2, 2, 0, 10, 8)
    expect(out.steps[0]).toEqual({ op: 'setVertices', entityId: 'x', points: [[8, 0], [0, 0]], closed: false })
  })

  it('refuses a radius whose tangent point would fall past a kept end, naming the largest that fits', () => {
    // 90 degrees, both kept parts 10 long: tan(45) * 10 = 10 is the most; 20 overshoots, 10 exactly leaves no line.
    const x = line('x', [0, 0], [10, 0])
    const y = line('y', [10, 0], [10, 10])
    expect(filletLines(x, y, 20, 2, 0, 10, 8).refusal).toBe('Fillet refused: the radius is too large for these two lines (at most 10 fits).')
    expect(filletLines(x, y, 10, 2, 0, 10, 8).refusal).toBe('Fillet refused: the radius is too large for these two lines (at most 10 fits).')
    expect(filletLines(x, y, 9.999, 2, 0, 10, 8).steps).toHaveLength(3)
    // The shorter kept part bounds it: the second line kept 4 long allows tan(45) * 4 = 4.
    expect(filletLines(x, line('y2', [10, 0], [10, 4]), 5, 2, 0, 10, 3).refusal).toBe('Fillet refused: the radius is too large for these two lines (at most 4 fits).')
    // A pick past the first line's own end names a part with no length on that side.
    expect(filletLines(x, y, 1, 12, 0, 10, 8).refusal).toBe('Fillet refused: the part of the first line to keep has no length on that side of the crossing.')
    // The same when the two lines do NOT touch and the pick lies beyond the
    // crossing: the endpoint in the picked direction sits BEHIND the crossing
    // (reach is signed), so nothing of the line is on that side. Kimi's three
    // repros, round two: fillet r = 1, chamfer, and the r = 0 corner.
    const far = line('f', [15, -5], [15, 5])
    expect(filletLines(x, far, 1, 16, 0, 15, 1).refusal).toBe('Fillet refused: the part of the first line to keep has no length on that side of the crossing.')
    expect(chamferLines(x, far, 1, 1, 16, 0, 15, 1).refusal).toBe('Chamfer refused: the part of the first line to keep has no length on that side of the crossing.')
    expect(filletLines(x, far, 0, 16, 0, 15, 1).refusal).toBe('Fillet refused: the part of the first line to keep has no length on that side of the crossing.')
    // And the legitimate extension to a corner the lines do not yet reach: the
    // pick ON the line, the crossing ahead, the tangent point at 15 - 1.
    expect(filletLines(x, far, 1, 9, 0, 15, 1).steps[0]).toEqual({ op: 'setVertices', entityId: 'x', points: [[0, 0], [14, 0]], closed: false })
    expect(filletLines(x, far, 0, 9, 0, 15, 1).steps[0]).toEqual({ op: 'setVertices', entityId: 'x', points: [[0, 0], [15, 0]], closed: false })
    // An acute corner explodes r / tan(theta / 2): 45 degrees between the kept parts, r = 4 needs 9.66, r = 5 does not fit 10.
    const diag = line('d', [10, 0], [0, 10])
    expect(filletLines(x, diag, 4, 2, 0, 2, 8).steps).toHaveLength(3)
    expect(filletLines(x, diag, 5, 2, 0, 2, 8).refusal).toMatch(/^Fillet refused: the radius is too large for these two lines \(at most 4\.142 fits\)\.$/)
  })

  it('refuses parallel lines, a pick on the crossing, a negative radius and non-lines', () => {
    expect(filletLines(line('x', [0, 0], [10, 0]), line('y', [0, 1], [10, 1]), 1, 2, 0, 2, 1).refusal).toBe('Fillet refused: the two lines are parallel and never meet.')
    expect(filletLines(line('x', [0, 0], [10, 0]), line('y', [10, 0], [10, 10]), 2, 10, 0, 10, 8).refusal).toMatch(/click on the first line to one side of the crossing/)
    expect(filletLines(line('x', [0, 0], [10, 0]), line('y', [10, 0], [10, 10]), -1, 2, 0, 10, 8).refusal).toBe('Fillet refused: the radius must be a number that is 0 or more.')
    expect(filletLines(line('x', [0, 0], [10, 0]), circle('c', [10, 0], 3), 1, 2, 0, 10, 3).refusal).toBe('Fillet refused: the second object is a CIRCLE; this round takes two lines.')
    expect(filletLines(arc('a', [0, 0], 5, 0, 90), line('y', [10, 0], [10, 10]), 1, 2, 0, 10, 8).refusal).toBe('Fillet refused: the selection is a ARC; this round takes two lines.')
    expect(filletLines(line('x', [0, 0], [10, 0]), line('y', [10, 0], [10, 10]), 1, Number.NaN, 0, 10, 8).refusal).toMatch(/the point on the first line: x and y must both be numbers/)
  })
})

describe('chamferLines', () => {
  it('bevels the corner d1 and d2 back along the kept sides and joins them with a line', () => {
    expect(chamferLines(line('x', [0, 0], [10, 0]), line('y', [10, 0], [10, 10]), 2, 3, 2, 0, 10, 8)).toEqual({
      steps: [
        { op: 'setVertices', entityId: 'x', points: [[0, 0], [8, 0]], closed: false },
        { op: 'setVertices', entityId: 'y', points: [[10, 3], [10, 10]], closed: false },
        { op: 'createLine', inputs: { x: 8, y: 0, x2: 10, y2: 3, layer: 'A' } },
      ],
    })
    // Both distances 0: the sharp corner, no third line.
    expect(chamferLines(line('x', [0, 0], [8, 0]), line('y', [10, 2], [10, 10]), 0, 0, 2, 0, 10, 8).steps).toHaveLength(2)
    expect(chamferLines(line('x', [0, 0], [10, 0]), line('y', [10, 0], [10, 10]), -1, 1, 2, 0, 10, 8).refusal).toBe('Chamfer refused: both distances must be numbers that are 0 or more.')
    // A distance that reaches or passes a kept end has no line left to cut, on either line.
    expect(chamferLines(line('x', [0, 0], [10, 0]), line('y', [10, 0], [10, 10]), 20, 1, 2, 0, 10, 8).refusal).toBe('Chamfer refused: the first distance is too large for the first line (less than 10 fits).')
    expect(chamferLines(line('x', [0, 0], [10, 0]), line('y', [10, 0], [10, 10]), 1, 10, 2, 0, 10, 8).refusal).toBe('Chamfer refused: the second distance is too large for the second line (less than 10 fits).')
    expect(chamferLines(line('x', [0, 0], [10, 0]), line('y', [10, 0], [10, 10]), 9.99, 9.99, 2, 0, 10, 8).steps).toHaveLength(3)
  })
})
