// W4g-6: the intersection kernel behind TRIM / EXTEND / FILLET / CHAMFER,
// pure rows. Every expected number is derived by hand in the comment beside it.
import { describe, expect, it } from 'vitest'

import {
  MAX_BATCH_STEPS, MAX_COORD, MAX_INTERSECT_POINTS, chamferLines, crossings, curveOf, extendEntity, filletLines,
  locate, nearestEntity, trimEntity,
} from './intersect.js'

const line = (id, a, b, layer = 'A') => ({ id, type: 'LINE', layer, closed: false, vertices: [[...a, 0], [...b, 0]], radius: null, startDeg: null, endDeg: null, editable: true })
const poly = (id, pts, closed, layer = 'A', bulges = null) => ({ id, type: 'LWPOLYLINE', layer, closed, vertices: pts.map((p) => [...p, 0]), radius: null, startDeg: null, endDeg: null, editable: true, ...(bulges ? { bulges } : {}) })
const circle = (id, c, r) => ({ id, type: 'CIRCLE', layer: 'A', closed: true, vertices: [[...c, 0]], radius: r, startDeg: null, endDeg: null, editable: true })
const arc = (id, c, r, s, e) => ({ id, type: 'ARC', layer: 'A', closed: false, vertices: [[...c, 0]], radius: r, startDeg: s, endDeg: e, editable: true })
const text = (id) => ({ id, type: 'TEXT', layer: 'A', closed: false, vertices: [[1, 1, 0]], radius: null, startDeg: null, endDeg: null, editable: true })
const parsePts = (pts) => pts.split(' ').map((p) => p.split(',').map(Number))

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
    // W4g-6b/6c: an arc and a circle take the tangent-circle path (their rows below); a polyline is still refused naming why.
    expect(filletLines(line('x', [0, 0], [10, 0]), poly('p', [[10, 0], [10, 10], [20, 10]], false), 1, 2, 0, 10, 8).refusal).toBe('Fillet refused: a polyline corner needs a bulge the engine does not carry yet; not in this round.')
    expect(filletLines(line('x', [0, 0], [10, 0]), line('y', [10, 0], [10, 10]), 1, Number.NaN, 0, 10, 8).refusal).toMatch(/the point on the first line: x and y must both be numbers/)
  })
})

describe('filletLines on arcs (W4g-6b)', () => {
  // The line y = 0 from (0,0) to (10,0) and the LEFT half of the circle
  // centred (10,0) radius 5 (an arc 90..270), crossing the line at (5,0).
  const X = () => line('x', [0, 0], [10, 0])
  const half = () => arc('a', [10, 0], 5, 90, 270)
  const near = (v, want) => expect(v).toBeCloseTo(want, 6)
  const DEG = 180 / Math.PI
  // The hand derivation, in code: C = (10 - sqrt(35), 1); T2 = O + 5 (C - O) / 6.
  const CX = 10 - Math.sqrt(35)
  const T2 = [10 + 5 * (CX - 10) / 6, 5 / 6]
  const T2_DEG = ((Math.atan2(T2[1], T2[0] - 10) * DEG) + 360) % 360
  const FILLET_END_DEG = ((Math.atan2(T2[1] - 1, T2[0] - CX) * DEG) + 360) % 360

  it('LINE x ARC, r = 1, keeping the line\'s left part and the arc\'s upper part: the centre is on y = 1 and on the circle of radius 6', () => {
    // C = (10 - sqrt(35), 1); T1 = (Cx, 0); T2 = O + 5 (C - O) / 6 = (5.0699, 0.8333) at 170.4059 degrees.
    const out = filletLines(X(), half(), 1, 2, 0, 7, 3)
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(3)
    const cx = 10 - Math.sqrt(35)
    expect(out.steps[0].op).toBe('setVertices'); expect(out.steps[0].entityId).toBe('x')
    expect(out.steps[0].points[0]).toEqual([0, 0]); near(out.steps[0].points[1][0], cx); near(out.steps[0].points[1][1], 0)
    expect(out.steps[1].op).toBe('setArc'); expect(out.steps[1].entityId).toBe('a')
    near(out.steps[1].a0, 90); near(out.steps[1].a1, T2_DEG); near(out.steps[1].r, 5)
    expect(T2_DEG).toBeCloseTo(170.4059, 3)
    expect(out.steps[2].op).toBe('createArc')
    near(out.steps[2].inputs.x, cx); near(out.steps[2].inputs.y, 1); expect(out.steps[2].inputs.r).toBe(1)
    near(out.steps[2].inputs.a0, 270); near(out.steps[2].inputs.a1, FILLET_END_DEG)
    expect(FILLET_END_DEG).toBeCloseTo(350.4059, 3)
    // The mirror pick set, keeping the arc's LOWER part: the centre drops to y = -1 and the arc keeps 189.59..270.
    const low = filletLines(X(), half(), 1, 2, 0, 7, -3)
    near(low.steps[1].a0, 360 - T2_DEG); near(low.steps[1].a1, 270); near(low.steps[2].inputs.y, -1)
  })

  it('ARC x LINE reads the same corner from the other side, and r = 0 is the corner at the crossing nearest the picks', () => {
    const swapped = filletLines(half(), X(), 1, 7, 3, 2, 0)
    expect(swapped.steps[0].op).toBe('setArc'); expect(swapped.steps[0].entityId).toBe('a'); near(swapped.steps[0].a1, T2_DEG)
    expect(swapped.steps[1].op).toBe('setVertices'); expect(swapped.steps[1].entityId).toBe('x')
    const corner = filletLines(X(), half(), 0, 2, 0, 7, 3)
    expect(corner.steps).toEqual([
      { op: 'setVertices', entityId: 'x', points: [[0, 0], [5, 0]], closed: false },
      { op: 'setArc', entityId: 'a', x: 10, y: 0, r: 5, a0: 90, a1: 180 },
    ])
    // The arc EXTENDS to a crossing beyond its end: an arc 90..150 still meets the line at (5,0) = 180 degrees.
    const short = filletLines(X(), arc('a', [10, 0], 5, 90, 150), 0, 2, 0, 7, 3)
    expect(short.steps[1]).toEqual({ op: 'setArc', entityId: 'a', x: 10, y: 0, r: 5, a0: 90, a1: 180 })
  })

  it('ARC x ARC: the fillet circle is tangent to both (its centre R +/- r from each centre) and both arcs keep positive sweeps', () => {
    // A: centre (0,0) r 5 from 0..90; B: centre (6,0) r 5 from 90..180; they cross at (3,4).
    const A = arc('a', [0, 0], 5, 0, 90)
    const B = arc('b', [6, 0], 5, 90, 180)
    const corner = filletLines(A, B, 0, 5, 0.5, 2, 1.5)
    // The crossing (3,4): 53.1301 degrees on A, 126.8699 on B (atan2(4, 3) and atan2(4, -3)).
    expect(corner.steps[0]).toMatchObject({ op: 'setArc', entityId: 'a', a0: 0 }); near(corner.steps[0].a1, Math.atan2(4, 3) * DEG)
    expect(corner.steps[1]).toMatchObject({ op: 'setArc', entityId: 'b', a1: 180 }); near(corner.steps[1].a0, Math.atan2(4, -3) * DEG)
    const out = filletLines(A, B, 1, 5, 0.5, 2, 1.5)
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(3)
    const C = [out.steps[2].inputs.x, out.steps[2].inputs.y]
    expect(out.steps[2].inputs.r).toBe(1)
    const dA = Math.hypot(C[0], C[1])
    const dB = Math.hypot(C[0] - 6, C[1])
    expect(Math.abs(dA - 6) < 1e-6 || Math.abs(dA - 4) < 1e-6).toBe(true)
    expect(Math.abs(dB - 6) < 1e-6 || Math.abs(dB - 4) < 1e-6).toBe(true)
    // Each arc keeps the part on its pick's side, shorter than before, and each kept end is the tangent point (5 from its centre).
    const sweepOf = (s) => ((s.a1 - s.a0) % 360 + 360) % 360
    expect(sweepOf(out.steps[0])).toBeGreaterThan(0); expect(sweepOf(out.steps[0])).toBeLessThan(90)
    expect(sweepOf(out.steps[1])).toBeGreaterThan(0); expect(sweepOf(out.steps[1])).toBeLessThan(90)
    expect(out.steps[0].a0).toBe(0); expect(out.steps[1].a1).toBe(180)
  })

  it('refuses a polyline, a radius no circle can take, and a chamfer with an arc, each naming why', () => {
    expect(filletLines(half(), poly('p', [[0, 0], [10, 0], [10, 10]], false), 1, 7, 3, 2, 0).refusal).toBe('Fillet refused: a polyline corner needs a bulge the engine does not carry yet; not in this round.')
    // Lines and arcs EXTEND to the tangent points (the reference's rule), so a large radius still fits when the offsets meet;
    // a line too far from the circle for any circle of that radius to touch both is the refusal.
    expect(filletLines(X(), half(), 100, 2, 0, 7, 3).steps).toHaveLength(3)
    expect(filletLines(line('f', [0, 20], [20, 20]), half(), 1, 2, 20, 7, 3).refusal).toBe('Fillet refused: no circle of that radius is tangent to both objects.')
    // A line tangent to the arc's circle touches without crossing: no corner at r = 0. At r > 0 the offset
    // pair meeting at the touch point itself (both tangent points coincident, a zero-sweep arc) is no fillet
    // and is skipped (kimi, #1051); what remains is the CUSP fillet the reference draws too: its centre 1 below
    // the line and 6 from the arc's centre, C = (10 - sqrt 20, 4), the arc kept to atan2(4, Cx - 10) = 138.19.
    const tangent = line('t', [0, 5], [20, 5])
    expect(filletLines(tangent, half(), 0, 2, 5, 9, 4).refusal).toBe('Fillet refused: the two objects touch without crossing; no corner to make.')
    const cusp = filletLines(tangent, half(), 1, 9, 5, 9, 4)
    const cuspX = 10 - Math.sqrt(20)
    expect(cusp.steps).toHaveLength(3)
    near(cusp.steps[0].points[0][0], cuspX); expect(cusp.steps[0].points[0][1]).toBe(5); expect(cusp.steps[0].points[1]).toEqual([20, 5])
    near(cusp.steps[1].a0, 90); near(cusp.steps[1].a1, ((Math.atan2(4, cuspX - 10) * DEG) + 360) % 360)
    near(cusp.steps[2].inputs.x, cuspX); near(cusp.steps[2].inputs.y, 4); expect(cusp.steps[2].inputs.r).toBe(1)
    // Two arcs whose circles touch at (5,0): r = 0 has no corner; r = 1 is the VALLEY fillet between them, its
    // centre 6 from both centres, C = (5, sqrt 11), the arcs kept to atan2(sqrt 11, 5) = 33.56 and from 146.44.
    const touchA = arc('ta', [0, 0], 5, 0, 90)
    const touchB = arc('tb', [10, 0], 5, 90, 180)
    expect(filletLines(touchA, touchB, 0, 4.9, 1, 5.1, 1).refusal).toBe('Fillet refused: the two objects touch without crossing; no corner to make.')
    const valley = filletLines(touchA, touchB, 1, 4.9, 1, 5.1, 1)
    const s11 = Math.sqrt(11)
    expect(valley.steps).toHaveLength(3)
    expect(valley.steps[0].a0).toBe(0); near(valley.steps[0].a1, Math.atan2(s11, 5) * DEG)
    near(valley.steps[1].a0, 180 - Math.atan2(s11, 5) * DEG); expect(valley.steps[1].a1).toBe(180)
    near(valley.steps[2].inputs.x, 5); near(valley.steps[2].inputs.y, s11); expect(valley.steps[2].inputs.r).toBe(1)
    // Every emitted arc keeps a real sweep: no plan carries a0 === a1, the crossing, cusp and valley cases alike.
    for (const plan of [0.5, 1, 3].map((r) => filletLines(X(), half(), r, 2, 0, 7, 3)).concat([cusp, valley])) {
      for (const s of plan.steps) {
        if (s.op === 'setArc') expect(((s.a1 - s.a0) % 360 + 360) % 360).toBeGreaterThan(1e-6)
        if (s.op === 'createArc') expect(((s.inputs.a1 - s.inputs.a0) % 360 + 360) % 360).toBeGreaterThan(1e-6)
      }
    }
    expect(filletLines(line('f', [0, 20], [20, 20]), half(), 0, 2, 20, 7, 3).refusal).toBe('Fillet refused: the two objects never meet, even extended.')
    expect(chamferLines(X(), half(), 1, 1, 2, 0, 7, 3).refusal).toBe('Chamfer refused: the second object is a ARC; CHAMFER between lines takes two lines.')
  })
})

describe('filletLines against a circle (W4g-6c)', () => {
  // The reference never cuts a circle: the fillet arc is added and the circle
  // stays whole, so a plan carries a step for the OTHER object (when it is not
  // a circle too) and one createArc, never a setArc or a delete for the circle.
  const X = () => line('x', [0, 0], [10, 0])
  const disc = () => circle('c', [10, 0], 5)
  const near = (v, want) => expect(v).toBeCloseTo(want, 6)
  const DEG = 180 / Math.PI
  // The same corner as the LINE x ARC row: C = (10 - sqrt 35, 1), T2 = O + 5 (C - O) / 6, the fillet arc 270..350.4059 about C.
  const CX = 10 - Math.sqrt(35)
  const T2 = [10 + 5 * (CX - 10) / 6, 5 / 6]
  const FILLET_END_DEG = ((Math.atan2(T2[1] - 1, T2[0] - CX) * DEG) + 360) % 360

  it('LINE x CIRCLE, r = 1: the line is cut to its tangent point, the arc is added, the circle is untouched', () => {
    const out = filletLines(X(), disc(), 1, 2, 0, 7, 3)
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(2)
    expect(out.steps[0].op).toBe('setVertices'); expect(out.steps[0].entityId).toBe('x')
    expect(out.steps[0].points[0]).toEqual([0, 0]); near(out.steps[0].points[1][0], CX); near(out.steps[0].points[1][1], 0)
    expect(out.steps[1].op).toBe('createArc')
    near(out.steps[1].inputs.x, CX); near(out.steps[1].inputs.y, 1); expect(out.steps[1].inputs.r).toBe(1)
    near(out.steps[1].inputs.a0, 270); near(out.steps[1].inputs.a1, FILLET_END_DEG)
    expect(out.steps.some((s) => s.entityId === 'c')).toBe(false)
    // The circle as the selection reads the same corner from the other side: the edge line is the one cut.
    const swapped = filletLines(disc(), X(), 1, 7, 3, 2, 0)
    expect(swapped.steps).toHaveLength(2)
    expect(swapped.steps[0]).toMatchObject({ op: 'setVertices', entityId: 'x' }); near(swapped.steps[0].points[1][0], CX)
    expect(swapped.steps[1].op).toBe('createArc'); near(swapped.steps[1].inputs.y, 1)
    // r = 0: the line is cut to the crossing nearest the picks, (5,0) not (15,0); the circle is still whole.
    expect(filletLines(X(), disc(), 0, 2, 0, 7, 3).steps).toEqual([{ op: 'setVertices', entityId: 'x', points: [[0, 0], [5, 0]], closed: false }])
  })

  it('ARC x CIRCLE and CIRCLE x CIRCLE: the fillet circle is R + r from each centre, the arc keeps its part outside the circle', () => {
    // A: the quarter arc centre (0,0) r 5 from 0..90; B: the circle centre (6,0) r 5; they cross at (3,4).
    // Picks at 70 degrees on A (1.71, 4.70) and 110 degrees on B (4.29, 4.70), both outside the other
    // circle, so the outer fillet wins: C = (3, sqrt 27) is 6 from both centres, T1 = 5 C / 6 = (2.5, 4.33)
    // at 60 degrees on A, T2 = (3.5, 4.33); the fillet is the MINOR arc 240..300 about C.
    const A = arc('a', [0, 0], 5, 0, 90)
    const B = circle('b', [6, 0], 5)
    const p1 = [5 * Math.cos(70 / DEG), 5 * Math.sin(70 / DEG)]
    const p2 = [6 + 5 * Math.cos(110 / DEG), 5 * Math.sin(110 / DEG)]
    const CY = Math.sqrt(27)
    const out = filletLines(A, B, 1, p1[0], p1[1], p2[0], p2[1])
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(2)
    expect(out.steps[0]).toMatchObject({ op: 'setArc', entityId: 'a', x: 0, y: 0, r: 5, a1: 90 }); near(out.steps[0].a0, 60)
    expect(out.steps[1].op).toBe('createArc')
    near(out.steps[1].inputs.x, 3); near(out.steps[1].inputs.y, CY); expect(out.steps[1].inputs.r).toBe(1)
    near(out.steps[1].inputs.a0, 240); near(out.steps[1].inputs.a1, 300)
    // The circle as the selection: the arc is still the one cut.
    const swapped = filletLines(B, A, 1, p2[0], p2[1], p1[0], p1[1])
    expect(swapped.steps).toHaveLength(2)
    expect(swapped.steps[0]).toMatchObject({ op: 'setArc', entityId: 'a' }); near(swapped.steps[0].a0, 60)
    // Two circles: nothing is cut, the plan is the one arc.
    const two = filletLines(circle('a', [0, 0], 5), B, 1, p1[0], p1[1], p2[0], p2[1])
    expect(two.steps).toHaveLength(1)
    expect(two.steps[0].op).toBe('createArc'); near(two.steps[0].inputs.x, 3); near(two.steps[0].inputs.y, CY)
    near(two.steps[0].inputs.a0, 240); near(two.steps[0].inputs.a1, 300)
    // The circle side is never a reason to refuse: every emitted arc keeps a real sweep across radii.
    for (const r of [0.5, 1, 3]) {
      for (const s of filletLines(A, B, r, p1[0], p1[1], p2[0], p2[1]).steps) {
        if (s.op === 'setArc') expect(((s.a1 - s.a0) % 360 + 360) % 360).toBeGreaterThan(1e-6)
        if (s.op === 'createArc') expect(((s.inputs.a1 - s.inputs.a0) % 360 + 360) % 360).toBeGreaterThan(1e-6)
      }
    }
  })

  it('refuses what a circle cannot do: r = 0 between two circles, a tangent contact at r = 0, no tangent circle, and a chamfer', () => {
    expect(filletLines(circle('a', [0, 0], 5), circle('b', [6, 0], 5), 0, 1, 4.9, 5, 4.9).refusal)
      .toBe('Fillet refused: two circles have no corner to make; a fillet between circles needs a radius greater than 0.')
    // The line y = 5 touches the circle at (10,5): no corner at r = 0; at r = 1 the CUSP fillet on the pick's side,
    // C = (10 - sqrt 20, 4), the line cut to (Cx, 5), the circle whole.
    const tangent = line('t', [0, 5], [20, 5])
    expect(filletLines(tangent, disc(), 0, 9, 5, 9, 4).refusal).toBe('Fillet refused: the two objects touch without crossing; no corner to make.')
    const cusp = filletLines(tangent, disc(), 1, 9, 5, 9, 4)
    const cuspX = 10 - Math.sqrt(20)
    expect(cusp.steps).toHaveLength(2)
    near(cusp.steps[0].points[0][0], cuspX); expect(cusp.steps[0].points[0][1]).toBe(5); expect(cusp.steps[0].points[1]).toEqual([20, 5])
    near(cusp.steps[1].inputs.x, cuspX); near(cusp.steps[1].inputs.y, 4)
    expect(filletLines(line('f', [0, 20], [20, 20]), disc(), 1, 2, 20, 7, 3).refusal).toBe('Fillet refused: no circle of that radius is tangent to both objects.')
    expect(filletLines(line('f', [0, 20], [20, 20]), disc(), 0, 2, 20, 7, 3).refusal).toBe('Fillet refused: the two objects never meet, even extended.')
    expect(chamferLines(X(), disc(), 1, 1, 2, 0, 7, 3).refusal).toBe('Chamfer refused: the second object is a CIRCLE; CHAMFER between lines takes two lines.')
  })
})

describe('polyline corners and bulges (W4g-6d)', () => {
  const B = Math.tan(Math.PI / 8) // the bulge of a 90-degree fillet: tan of a quarter of the included angle
  const near = (v, want) => expect(v).toBeCloseTo(want, 9)
  // The counter-clockwise 10 x 10 square and its clockwise twin.
  const SQ = () => poly('sq', [[0, 0], [10, 0], [10, 10], [0, 10]], true)
  const CW = () => poly('cw', [[0, 0], [0, 10], [10, 10], [10, 0]], true)

  it('curveOf reads one bulge per vertex, refuses a list that does not match, and reads no list as straight', () => {
    const curved = curveOf(poly('p', [[0, 0], [10, 0], [10, 10]], false, 'A', [0, 0.5, 0]))
    expect(curved).toMatchObject({ kind: 'POLY', curved: true, bulges: [0, 0.5, 0] })
    expect(curveOf(poly('p', [[0, 0], [10, 0], [10, 10]], false))).toMatchObject({ curved: false, bulges: [0, 0, 0] })
    expect(curveOf(poly('p', [[0, 0], [10, 0], [10, 10]], false, 'A', [0, 0])).refusal).toMatch(/bulge list does not match its points/)
    expect(curveOf(poly('p', [[0, 0], [10, 0], [10, 10]], false, 'A', [0, Number.NaN, 0])).refusal).toMatch(/a bulge that is not a number/)
    // Below the crate's own straight threshold a bulge is straight.
    expect(curveOf(poly('p', [[0, 0], [10, 0]], false, 'A', [1e-12, 0])).curved).toBe(false)
  })

  it('trims curved polylines but refuses a boundary behind a curved end and two-entity curved corners', () => {
    const arcy = poly('arcy', [[0, -5], [5, -5], [5, 5]], false, 'A', [0, 0.5, 0])
    // The arc about (1.25,0), r 6.25, meets H only at (7.5,0), halfway along.
    const trimmed = trimEntity(arcy, H(), 5, 3)
    expect(trimmed.steps).toHaveLength(1)
    const { points, bulges, ...shape } = trimmed.steps[0]
    expect(shape).toEqual({ op: 'setVertices', entityId: 'arcy', closed: false })
    expect(points).toHaveLength(3)
    expect(points.slice(0, 2)).toEqual([[0, -5], [5, -5]])
    near(points[2][0], 7.5); expect(points[2][1]).toBeCloseTo(0, 9)
    expect(bulges).toHaveLength(3)
    near(bulges[0], 0); near(bulges[1], Math.sqrt(5) - 2); near(bulges[2], 0)
    const cutLine = trimEntity(H(), arcy, 8, 0)
    expect(cutLine.steps).toHaveLength(1)
    const { points: cutPoints, ...cutShape } = cutLine.steps[0]
    expect(cutShape).toEqual({ op: 'setVertices', entityId: 'h', closed: false })
    expect(cutPoints).toHaveLength(2)
    expect(cutPoints[0]).toEqual([0, 0]); near(cutPoints[1][0], 7.5); expect(cutPoints[1][1]).toEqual(0)
    expect(extendEntity(arcy, H(), 5, 4).refusal).toBe('Extend refused: the boundary edge does not lie ahead of that end.')
    const extended = extendEntity(line('e', [0, 0], [4, 0]), arcy, 4, 0)
    expect(extended.steps).toHaveLength(1)
    const { points: endPoints, ...endShape } = extended.steps[0]
    expect(endShape).toEqual({ op: 'setVertices', entityId: 'e', closed: false })
    expect(endPoints).toHaveLength(2)
    expect(endPoints[0]).toEqual([0, 0]); near(endPoints[1][0], 7.5); expect(endPoints[1][1]).toEqual(0)
    expect(filletLines(H(), arcy, 1, 2, 0, 5, 3).refusal).toBe('Fillet refused: the second object is a polyline with curved segments; not in this round.')
    expect(chamferLines(H(), arcy, 1, 1, 2, 0, 5, 3).refusal).toBe('Chamfer refused: the second object is a polyline with curved segments; not in this round.')
    // A straight polyline still trims as before.
    expect(trimEntity(H(), poly('v', [[5, -5], [5, 5]], false), 8, 0).steps).toHaveLength(1)
  })

  it('FILLET at a polyline corner: V becomes two tangent points and the first carries the arc as a bulge, ONE step', () => {
    // The corner (10,10): picks (10,5) on the second side and (5,10) on the third; r = 2 at 90 degrees puts
    // T1 = (10,8), T2 = (8,10), the square turns LEFT there so the bulge is +tan(pi/8) = 0.4142 (centre (8,8)).
    const out = filletLines(SQ(), SQ(), 2, 10, 5, 5, 10)
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(1)
    expect(out.steps[0]).toMatchObject({ op: 'setVertices', entityId: 'sq', closed: true, points: [[0, 0], [10, 0], [10, 8], [8, 10], [0, 10]] })
    expect(out.steps[0].bulges).toHaveLength(5)
    near(out.steps[0].bulges[2], B)
    expect(out.steps[0].bulges.filter((b) => b === 0)).toHaveLength(4)
    // The picks in the other order name the same corner and the same plan.
    expect(filletLines(SQ(), SQ(), 2, 5, 10, 10, 5).steps).toEqual(out.steps)
    // The clockwise square turns RIGHT at (10,10): the same points, the bulge negative.
    const cw = filletLines(CW(), CW(), 2, 5, 10, 10, 5)
    expect(cw.steps[0].points).toEqual([[0, 0], [0, 10], [8, 10], [10, 8], [10, 0]])
    near(cw.steps[0].bulges[2], -B)
    // The corner at the FIRST vertex (0,0) is where the closing segment meets the first: the two new points lead the list.
    const wrap = filletLines(SQ(), SQ(), 2, 0, 5, 5, 0)
    expect(wrap.steps[0].points).toEqual([[0, 2], [2, 0], [10, 0], [10, 10], [0, 10]])
    near(wrap.steps[0].bulges[0], B)
    // An open L keeps its ends: the corner (10,0) of (0,0)-(10,0)-(10,10).
    const L = poly('l', [[0, 0], [10, 0], [10, 10]], false)
    const open = filletLines(L, L, 2, 5, 0, 10, 5)
    expect(open.steps[0]).toMatchObject({ closed: false, points: [[0, 0], [8, 0], [10, 2], [10, 10]] })
    near(open.steps[0].bulges[1], B)
    // A bulge the polyline already carries elsewhere rides through unchanged.
    const mixed = poly('m', [[0, 0], [10, 0], [10, 10], [0, 10]], false, 'A', [0.3, 0, 0, 0])
    const kept = filletLines(mixed, mixed, 2, 10, 5, 5, 10)
    expect(kept.steps[0].points).toEqual([[0, 0], [10, 0], [10, 8], [8, 10], [0, 10]])
    expect(kept.steps[0].bulges[0]).toBe(0.3); near(kept.steps[0].bulges[2], B)
  })

  it('CHAMFER at a polyline corner: V becomes the two cut points, no bulge', () => {
    expect(chamferLines(SQ(), SQ(), 2, 3, 10, 5, 5, 10).steps).toEqual([
      { op: 'setVertices', entityId: 'sq', points: [[0, 0], [10, 0], [10, 8], [7, 10], [0, 10]], closed: true, bulges: [0, 0, 0, 0, 0] },
    ])
    expect(chamferLines(SQ(), SQ(), 0, 0, 10, 5, 5, 10).refusal).toBe('Chamfer refused: the corner is already sharp; a chamfer on a polyline corner needs a distance greater than 0.')
    expect(chamferLines(SQ(), SQ(), 10, 1, 10, 5, 5, 10).refusal).toBe('Chamfer refused: the first distance is too large for the first segment (less than 10 fits).')
    expect(chamferLines(SQ(), SQ(), 1, 10, 10, 5, 5, 10).refusal).toBe('Chamfer refused: the second distance is too large for the second segment (less than 10 fits).')
  })

  it('refuses what is not a corner of one polyline, each naming why', () => {
    expect(filletLines(SQ(), SQ(), 0, 10, 5, 5, 10).refusal).toBe('Fillet refused: the corner is already sharp; a fillet on a polyline corner needs a radius greater than 0.')
    expect(filletLines(SQ(), SQ(), 20, 10, 5, 5, 10).refusal).toBe('Fillet refused: the radius is too large for these two segments (at most 10 fits).')
    expect(filletLines(SQ(), SQ(), 2, 10, 3, 10, 7).refusal).toBe('Fillet refused: click two different segments of the polyline that meet at the corner.')
    expect(filletLines(SQ(), SQ(), 2, 5, 0, 5, 10).refusal).toBe('Fillet refused: the two segments do not meet at a corner; click two segments that share a vertex.')
    // Open: the first and last segments do not meet.
    const openSq = poly('o', [[0, 0], [10, 0], [10, 10], [0, 10]], false)
    expect(filletLines(openSq, openSq, 2, 0, 5, 5, 10).refusal).toBe('Fillet refused: the two segments do not meet at a corner; click two segments that share a vertex.')
    // A curved segment AT the corner; one segment only; a line named as its own second object.
    const curvedCorner = poly('c', [[0, 0], [10, 0], [10, 10], [0, 10]], true, 'A', [0, 0.4, 0, 0])
    expect(filletLines(curvedCorner, curvedCorner, 2, 10, 5, 5, 10).refusal).toBe('Fillet refused: a segment at that corner is curved; not in this round.')
    expect(filletLines(poly('one', [[0, 0], [10, 0]], false), poly('one', [[0, 0], [10, 0]], false), 2, 2, 0, 8, 0).refusal).toBe('Fillet refused: the polyline has one segment; no corner to make.')
    expect(filletLines(H(), H(), 1, 2, 0, 8, 0).refusal).toBe('Fillet refused: select a different entity as the second object.')
    expect(chamferLines(H(), H(), 1, 1, 2, 0, 8, 0).refusal).toBe('Chamfer refused: select a different entity as the second object.')
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

describe('TRIM on curved polylines (W4g-6e)', () => {
  // Positive bulge 1: the lower semicircle about (5,0), start 180, sweep +180.
  const SEMI = () => poly('p', [[0, 0], [10, 0]], false, 'A', [1, 0])
  const SEMI3 = () => poly('p', [[0, 0], [10, 0], [10, 10]], false, 'A', [1, 0, 0])
  const RING = () => poly('r', [[0, 0], [10, 0], [10, 10], [0, 10]], true, 'A', [0, 0, 0, 1])
  const B45 = Math.tan(Math.PI / 8)
  const B60 = Math.tan(Math.PI / 12)
  const B30 = Math.tan(Math.PI / 24)
  const near = (v, want) => expect(v).toBeCloseTo(want, 9)
  const expectBulges = (actual, want) => {
    expect(actual).toHaveLength(want.length)
    want.forEach((b, i) => near(actual[i], b))
  }
  const expectVertices = (step, id, points, bulges = null) => {
    const { points: actual, bulges: actualBulges, ...shape } = step
    expect(shape).toEqual({ op: 'setVertices', entityId: id, closed: false })
    expect(actual).toHaveLength(points.length)
    points.forEach((p, i) => {
      expect(actual[i]).toHaveLength(2)
      p.forEach((v, j) => {
        if (bulges === null && Number.isInteger(v)) expect(actual[i][j]).toEqual(v)
        else near(actual[i][j], v)
      })
    })
    if (bulges === null) expect(step).not.toHaveProperty('bulges')
    else expectBulges(actualBulges, bulges)
  }
  const expectCreated = (step, pts, bulges = null) => {
    expect(step.op).toEqual('createPolyline')
    const { bulges: actual, pts: actualPts, ...inputs } = step.inputs
    expect(inputs).toEqual({ closed: false, layer: 'A' })
    expect(Object.keys(step).sort()).toEqual(['inputs', 'op'])
    if (bulges === null) {
      expect(actualPts).toEqual(pts)
      expect(step.inputs).not.toHaveProperty('bulges')
    } else {
      const points = parsePts(actualPts)
      const want = parsePts(pts)
      expect(points).toHaveLength(want.length)
      want.forEach((p, i) => {
        expect(points[i]).toHaveLength(2)
        p.forEach((v, j) => near(points[i][j], v))
      })
      expectBulges(actual, bulges)
    }
  }

  it('locates and picks the arc instead of its chord', () => {
    const at = locate(curveOf(SEMI()), [5, -7])
    expect(at.d).toEqual(2); near(at.s, 0.5)
    const nearest = nearestEntity([SEMI()], 5, -5.5, 1)
    expect(Object.keys(nearest).sort()).toEqual(['d', 'id'])
    expect(nearest.id).toEqual('p'); near(nearest.d, 0.5)
  })

  it('keeps either half of a positive semicircle as a 90-degree arc', () => {
    // V meets the lower semicircle at (5,-5), directed fraction 1/2.
    const tail = trimEntity(SEMI3(), V(), 2, -4)
    expect(tail.steps).toHaveLength(1)
    expectVertices(tail.steps[0], 'p', [[5, -5], [10, 0], [10, 10]], [B45, 0, 0])
    const head = trimEntity(SEMI3(), V(), 8, -4)
    expect(head.steps).toHaveLength(1)
    expectVertices(head.steps[0], 'p', [[0, 0], [5, -5]], [B45, 0])
  })

  it('keeps half of a negative major arc without substituting its minor sweep', () => {
    // Centre (5,5), r = 5 sqrt(2), start 225, sweep -270: midpoint angle 90.
    const big = poly('b', [[0, 0], [10, 0]], false, 'A', [-Math.tan(67.5 * Math.PI / 180), 0])
    const out = trimEntity(big, line('c', [5, 6], [5, 20]), -2.07, 5)
    expect(out.steps).toHaveLength(1)
    expectVertices(out.steps[0], 'b', [[5, 12.071067812], [10, 0]], [-Math.tan(33.75 * Math.PI / 180), 0])
  })

  it('canonicalizes a crossing shared by a chord end and an arc start', () => {
    const mix = poly('m', [[0, 0], [10, 0], [20, 0]], false, 'A', [0, 1, 0])
    // y = x - 10 meets (10,0); its other circle root (15,5) is off the sweep.
    const cutter = line('d', [5, -5], [15, 5])
    expect(crossings(curveOf(mix), curveOf(cutter))).toEqual([{ s: 1, p: [10, 0] }])
    const tail = trimEntity(mix, cutter, 2, 0)
    expect(tail.steps).toHaveLength(1)
    expectVertices(tail.steps[0], 'm', [[10, 0], [20, 0]], [1, 0])
    const head = trimEntity(mix, cutter, 15, -4)
    expect(head.steps).toHaveLength(1)
    expectVertices(head.steps[0], 'm', [[0, 0], [10, 0]])
  })

  it('snaps a near-end arc crossing within the aperture, leaving no sliver', () => {
    // The lower root is about 1e-4 from (10,0), within the 1e-3 aperture.
    const cutter = line('n', [10 - 1e-9, -1], [10 - 1e-9, 10])
    expect(crossings(curveOf(SEMI3()), curveOf(cutter), 'none', 1e-3)).toEqual([{ s: 1, p: [10, 0] }])
    const out = trimEntity(SEMI3(), cutter, 3, -4, 1e-3)
    expect(out.steps).toHaveLength(1)
    expectVertices(out.steps[0], 'p', [[10, 0], [10, 10]])
  })

  it('opens a closed polyline across its curved closing segment and keeps both arc fragments', () => {
    // x = -3 meets the left semicircle at (-3,9) and (-3,1). Each kept arc
    // sweeps 36.86989765 degrees, with bulge tan(theta/4) = sqrt(10) - 3.
    const out = trimEntity(RING(), line('x', [-3, 0], [-3, 10]), -5, 5)
    expect(out.steps).toHaveLength(1)
    expectVertices(out.steps[0], 'r', [[-3, 1], [0, 0], [10, 0], [10, 10], [0, 10], [-3, 9]],
      [Math.sqrt(10) - 3, 0, 0, 0, Math.sqrt(10) - 3, 0])
  })

  it('removes the middle of an open arc and creates a second polyline with its own bulges', () => {
    // y = -5 sin(60) meets angles 240 and 300: each kept end sweeps 60 degrees.
    const cutter = line('y', [0, -4.330127018922193], [10, -4.330127018922193])
    const out = trimEntity(SEMI(), cutter, 5, -5)
    expect(out.steps).toHaveLength(2)
    expectVertices(out.steps[0], 'p', [[0, 0], [2.5, -4.330127019]], [B60, 0])
    expectCreated(out.steps[1], '7.5,-4.330127019 10,0', [B60, 0])
  })

  it('intersects chord and arc roles and rejects circle roots outside either directed sweep', () => {
    // The line's upper root (5,5) is outside SEMI's lower sweep.
    const straight = trimEntity(line('l', [5, -10], [5, 10]), SEMI(), 5, -8)
    expect(straight.steps).toHaveLength(1)
    expectVertices(straight.steps[0], 'l', [[5, -5], [5, 10]])
    // Two radius-5 circles separated by 5 in y meet at x = 5 +/- sqrt(18.75),
    // y = -2.5, angles 210 and 330 on SEMI: the kept ends sweep 30 degrees.
    const up = poly('u', [[10, -5], [0, -5]], false, 'A', [1, 0])
    const curved = trimEntity(SEMI(), up, 5, -5)
    expect(curved.steps).toHaveLength(2)
    expectVertices(curved.steps[0], 'p', [[0, 0], [0.669872981, -2.5]], [B30, 0])
    expectCreated(curved.steps[1], '9.330127019,-2.5 10,0', [B30, 0])
    const down = poly('u', [[0, -5], [10, -5]], false, 'A', [1, 0])
    expect(crossings(curveOf(SEMI()), curveOf(down))).toEqual([])
    expect(trimEntity(SEMI(), down, 5, -5).refusal).toEqual('Trim refused: the cutting edge does not cross the selection.')
  })

  it('dedupes tangent roots and coincident supports but preserves distinct visits to one point', () => {
    const tangent = line('t', [0, -5], [10, -5])
    const hits = crossings(curveOf(SEMI()), curveOf(tangent))
    expect(hits).toHaveLength(1)
    near(hits[0].s, 0.5); expect(hits[0].p).toEqual([5, -5])
    const tail = trimEntity(SEMI(), tangent, 2, -4)
    expect(tail.steps).toHaveLength(1)
    expectVertices(tail.steps[0], 'p', [[5, -5], [10, 0]], [B45, 0])
    expect(crossings(curveOf(SEMI()), curveOf(poly('q', [[0, 0], [10, 0]], false, 'A', [1, 0])))).toEqual([])
    const bow = poly('w', [[0, 0], [10, 10], [10, 0], [0, 10]], false)
    const cutter = line('z', [-1, 5], [11, 5])
    const visits = crossings(curveOf(bow), curveOf(cutter))
    expect(visits).toHaveLength(3)
    ;[0.5, 1.5, 2.5].forEach((s, i) => near(visits[i].s, s))
    expect(visits.map((h) => h.p)).toEqual([[5, 5], [10, 5], [5, 5]])
    const out = trimEntity(bow, cutter, 7.5, 7.5)
    expect(out.steps).toHaveLength(2)
    expectVertices(out.steps[0], 'w', [[0, 0], [5, 5]])
    expectCreated(out.steps[1], '10,5 10,0 0,10')
  })

  it('refuses overflowing bulges and curved segments of zero length before emitting steps', () => {
    const overflow = poly('o', [[0, 0], [10, 0]], false, 'A', [1e200, 0])
    const zero = poly('o', [[0, 0], [0, 0], [10, 0]], false, 'A', [0.5, 0, 0])
    for (const [entity, reason] of [[overflow, 'bulge that overflows'], [zero, 'curved segment of zero length']]) {
      expect(curveOf(entity).refusal).toMatch(new RegExp(reason))
      const out = trimEntity(entity, V(), 2, -4)
      expect(out).toEqual({ refusal: `Trim refused: the selection polyline has a ${reason}.` })
      expect(out).not.toHaveProperty('steps')
    }
  })

  it('admits 1000 points and carries full bulges exactly, but refuses 1001 points', () => {
    const pts = Array.from({ length: 1000 }, (_, i) => [i, i % 2])
    const bulges = pts.map(() => 0.1)
    const cutter = line('k', [500.5, -1], [500.5, 2])
    const out = trimEntity(poly('g', pts, false, 'A', bulges), cutter, 200, 0.5)
    expect(out.steps).toHaveLength(1)
    const { points: kept, bulges: keptBulges, ...shape } = out.steps[0]
    expect(shape).toEqual({ op: 'setVertices', entityId: 'g', closed: false })
    expect(kept).toHaveLength(500)
    near(kept[0][0], 500.5)
    expect(kept.slice(1)).toEqual(pts.slice(501))
    expect(keptBulges).toHaveLength(500)
    expect(keptBulges[0]).toBeGreaterThan(0)
    expect(keptBulges[0]).toBeLessThan(0.1)
    keptBulges.slice(1, -1).forEach((b) => expect(b).toBe(0.1))
    near(keptBulges[499], 0)
    const tooMany = poly('g', [...pts, [1000, 0]], false, 'A', [...bulges, 0.1])
    expect(trimEntity(tooMany, cutter, 200, 0.5).refusal).toMatch(/more than 1000 points/)
  })

  it('stores the closed seam as one crossing at zero and preserves a whole closing arc', () => {
    // y = x meets vertices 0 and 2; (5,5), the other circle root, is off the arc.
    const cutter = line('s', [-1, -1], [11, 11])
    expect(crossings(curveOf(RING()), curveOf(cutter))).toEqual([{ s: 0, p: [0, 0] }, { s: 2, p: [10, 10] }])
    const out = trimEntity(RING(), cutter, 5, 0)
    expect(out.steps).toHaveLength(1)
    expectVertices(out.steps[0], 'r', [[10, 10], [0, 10], [0, 0]], [0, 1, 0])
  })

  it('extends a straight end without losing other bulges and accepts a curved boundary', () => {
    const trimmy = poly('e', [[0, 0], [10, 0], [10, 5]], false, 'A', [1, 0, 0])
    const out = extendEntity(trimmy, line('b', [0, 8], [20, 8]), 10, 6)
    expect(out.steps).toHaveLength(1)
    expectVertices(out.steps[0], 'e', [[0, 0], [10, 0], [10, 8]], [1, 0, 0])
    const straight = extendEntity(line('l', [5, -10], [5, -6]), SEMI(), 5, -6)
    expect(straight.steps).toHaveLength(1)
    expectVertices(straight.steps[0], 'l', [[5, -10], [5, -5]])
  })
})

describe('EXTEND on a curved end segment (W4g-6e)', () => {
  const B45 = Math.tan(Math.PI / 8)
  const B135 = Math.tan(135 * Math.PI / 180 / 4)
  // The terminal arc is about (10,5), r 5, start 270, sweep +90.
  const hook = () => poly('k', [[0, 0], [10, 0], [15, 5]], false, 'A', [0, B45, 0])
  const expectVertices = (out, id, points, bulges) => {
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(1)
    const { points: actual, bulges: actualBulges, ...shape } = out.steps[0]
    expect(shape).toEqual({ op: 'setVertices', entityId: id, closed: false })
    expect(actual).toHaveLength(points.length)
    points.forEach((p, i) => {
      expect(actual[i]).toHaveLength(2)
      p.forEach((v, j) => expect(actual[i][j]).toBeCloseTo(v, 9))
    })
    expect(actualBulges).toHaveLength(bulges.length)
    bulges.forEach((b, i) => {
      if (Number.isInteger(b)) expect(actualBulges[i]).toEqual(b)
      else expect(actualBulges[i]).toBeCloseTo(b, 9)
    })
  }

  it('extends arcy to the vertical boundary crossing in its gap', () => {
    const arcy = poly('arcy', [[0, -5], [5, -5], [5, 5]], false, 'A', [0, 0.5, 0])
    // x = 0 reaches (0,sqrt(37.5)), growing the sweep to about 154.667 degrees.
    const out = extendEntity(arcy, line('t', [0, 0], [0, 10]), 5, 4)
    expectVertices(out, 'arcy', [[0, -5], [5, -5], [0, 6.123724357]], [0, 0.8001991550, 0])
  })

  it('extends a positive terminal arc along its circle to a signed 135-degree sweep', () => {
    // The root at 45 degrees is in the boundary span; the root at 135 is not.
    const out = extendEntity(hook(), line('b', [10, 8.535533905932738], [20, 8.535533905932738]), 16, 6)
    expectVertices(out, 'k', [[0, 0], [10, 0], [13.535533906, 8.535533906]], [0, B135, 0])
  })

  it('extends a negative terminal arc along its circle with a negative bulge', () => {
    const hookCw = poly('w', [[0, 0], [10, 0], [15, -5]], false, 'A', [0, -B45, 0])
    const out = extendEntity(hookCw, line('b', [10, -8.535533905932738], [20, -8.535533905932738]), 16, -6)
    expectVertices(out, 'w', [[0, 0], [10, 0], [13.535533906, -8.535533906]], [0, -B135, 0])
  })

  it('extends the start of a positive first arc and preserves the remaining vertices', () => {
    const first = poly('f', [[10, 0], [15, 5], [20, 5]], false, 'A', [B45, 0, 0])
    // The root at 225 degrees is 45 degrees before the start; 315 is off the span.
    const out = extendEntity(first, line('b', [0, 1.4644660940672622], [10, 1.4644660940672622]), 9, 0)
    expectVertices(out, 'f', [[6.464466094, 1.464466094], [15, 5], [20, 5]], [B135, 0, 0])
  })

  it('refuses crossings behind the end, a full turn, no root and a closed target without steps', () => {
    const ahead = 'Extend refused: the boundary edge does not lie ahead of that end.'
    const cases = [
      // Only the 315-degree root is on this edge, strictly inside the existing arc.
      [extendEntity(hook(), line('v', [13.535533905932738, -10], [13.535533905932738, 4]), 16, 6), ahead],
      // The tangent is the arc's start, so reaching it would complete a full turn.
      [extendEntity(hook(), line('s', [5, 0], [10, 0]), 16, 6), 'Extend refused: extending that far would close the arc into a full turn.'],
      [extendEntity(hook(), line('n', [0, 20], [20, 20]), 16, 6), ahead],
      [extendEntity(poly('c', [[0, 0], [10, 0], [10, 10]], true, 'A', [1, 0, 0]), H(), 5, 5), 'Extend refused: a closed polyline has no end to extend.'],
    ]
    for (const [out, refusal] of cases) {
      expect(out).toEqual({ refusal })
      expect(out).not.toHaveProperty('steps')
    }
  })

  it('extends a straight start while carrying the curved end unchanged', () => {
    const out = extendEntity(hook(), line('l', [-3, -5], [-3, 5]), 1, 0)
    expectVertices(out, 'k', [[-3, 0], [10, 0], [15, 5]], [0, B45, 0])
  })

  it('keeps every other vertex and bulge exactly when extending the terminal arc', () => {
    const out = extendEntity(hook(), line('b', [10, 8.535533905932738], [20, 8.535533905932738]), 16, 6)
    expect(out.steps).toHaveLength(1)
    const { points, bulges } = out.steps[0]
    expect(points[0]).toEqual([0, 0])
    expect(points[1]).toEqual([10, 0])
    expect(bulges[0]).toBe(0)
    expect(bulges[2]).toBe(0)
  })

  it('reports the full-circle crossing so an ARC entity extends to a curved polyline boundary', () => {
    const arcEntity = arc('a', [0, 0], 5, 0, 90)
    const bowl = poly('q', [[-10, 2], [0, 2]], false, 'A', [1, 0])
    // The lower circle root lies on the bowl and in the target's gap; the upper root is off the bowl.
    const hits = crossings(curveOf(arcEntity), curveOf(bowl))
    expect(hits).toHaveLength(1)
    expect(hits[0].s).toBeCloseTo(215.615884258, 6)
    const out = extendEntity(arcEntity, bowl, -1, 5)
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(1)
    const { a1, ...shape } = out.steps[0]
    expect(shape).toEqual({ op: 'setArc', entityId: 'a', x: 0, y: 0, r: 5, a0: 0 })
    expect(a1).toBeCloseTo(215.615884258, 6)
  })
})

describe('Astra refutations (W4g-6e record 4)', () => {
  it('preserves a tiny chord with a huge bulge through an unrelated end trim', () => {
    // The 0.001 chord and bulge 100000 describe an arc of radius 25 and sagitta 50.
    const target = poly('p', [[0, 0], [0.001, 0], [10, 0], [20, 0]], false, 'A', [100000, 0, 0, 0])
    const out = trimEntity(target, line('e', [15, -1], [15, 1]), 19, 0, 0.01)
    expect(out).toEqual({ steps: [{
      op: 'setVertices', entityId: 'p', points: [[0, 0], [0.001, 0], [10, 0], [15, 0]], closed: false,
      bulges: [100000, 0, 0, 0],
    }] })
  })

  it('keeps crossings that are close in param but far apart in space', () => {
    const target = line('l', [0, 0], [1e9, 0])
    // The lower semicircle about (100,1), r 2, meets y = 0 at x = 100 +/- sqrt(3).
    const edge = poly('q', [[98, 1], [102, 1]], false, 'A', [1, 0])
    expect(crossings(curveOf(target), curveOf(edge))).toHaveLength(2)
    const out = trimEntity(target, edge, 100, 0, 1e-9)
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(2)
    const { points, ...first } = out.steps[0]
    expect(first).toEqual({ op: 'setVertices', entityId: 'l', closed: false })
    expect(points).toHaveLength(2)
    expect(points[0]).toEqual([0, 0])
    expect(points[1]).toHaveLength(2)
    expect(points[1][0]).toBeCloseTo(98.267949192, 6)
    expect(points[1][1]).toBe(0)
    expect(out.steps[0]).not.toHaveProperty('bulges')
    const { inputs, ...second } = out.steps[1]
    expect(second).toEqual({ op: 'createLine' })
    const { x, ...rest } = inputs
    expect(x).toBeCloseTo(101.732050808, 6)
    expect(rest).toEqual({ y: 0, x2: 1e9, y2: 0, layer: 'A' })
    expect(trimEntity(line('l', [0, 0], [1e10, 0]), edge, 100, 0, 1e-9).refusal).toMatch(/beyond 1e9/)
  })

  it('keeps an interior crossing at a coordinate that revisits the start', () => {
    const target = poly('p', [[0, 0], [10, 0], [0, 0], [0, 10]], false, 'A', [1, 0, 0, 0])
    // y = x meets the arc start at s = 0 and the return visit at s = 2.
    const edge = line('e', [-1, -1], [1, 1])
    expect(crossings(curveOf(target), curveOf(edge))).toEqual([{ s: 0, p: [0, 0] }, { s: 2, p: [0, 0] }])
    expect(trimEntity(target, edge, 0, 5, 1e-9)).toEqual({ steps: [{
      op: 'setVertices', entityId: 'p', points: [[0, 0], [10, 0], [0, 0]], closed: false,
      bulges: [1, 0, 0],
    }] })
  })

  it('keeps the endpoint of a retained arc before a short straight segment', () => {
    const target = poly('p', [[0, 0], [0.001, 0], [0.002, 0], [10, 0], [20, 0]], false, 'A', [100000, 0, 0, 0, 0])
    const out = trimEntity(target, line('e', [15, -1], [15, 1]), 19, 0, 0.01)
    expect(out).toEqual({ steps: [{
      op: 'setVertices', entityId: 'p', points: [[0, 0], [0.001, 0], [0.002, 0], [10, 0], [15, 0]], closed: false,
      bulges: [100000, 0, 0, 0, 0],
    }] })
  })

  it('solves a short cutter through an arc and keeps the remaining quarter circle', () => {
    const target = poly('p', [[0, 0], [10, 0]], false, 'A', [1, 0])
    const edge = line('e', [5, -5.000001], [5, -4.999999])
    const hits = crossings(curveOf(target), curveOf(edge))
    expect(hits).toHaveLength(1)
    expect(hits[0].s).toBeCloseTo(0.5, 9)
    const out = trimEntity(target, edge, 2, -4, 1e-9)
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(1)
    const { points, bulges, ...shape } = out.steps[0]
    expect(shape).toEqual({ op: 'setVertices', entityId: 'p', closed: false })
    expect(points).toHaveLength(2)
    ;[[5, -5], [10, 0]].forEach((p, i) => {
      expect(points[i]).toHaveLength(2)
      p.forEach((v, j) => expect(points[i][j]).toBeCloseTo(v, 6))
    })
    expect(bulges).toHaveLength(2)
    expect(bulges[0]).toBeCloseTo(Math.tan(Math.PI / 8), 9)
    expect(bulges[1]).toBe(0)
  })

  it('keeps near-start crossings interior on a very long line', () => {
    const target = line('l', [0, 0], [1e9, 0])
    const edge = poly('q', [[98, 1], [102, 1]], false, 'A', [1, 0])
    const out = trimEntity(target, edge, 100, 0, 1e-9)
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(2)
    const { points, ...first } = out.steps[0]
    expect(first).toEqual({ op: 'setVertices', entityId: 'l', closed: false })
    expect(points).toHaveLength(2)
    expect(points[0]).toEqual([0, 0])
    expect(points[1]).toHaveLength(2)
    expect(points[1][0]).toBeCloseTo(98.267949192, 6)
    expect(points[1][1]).toBe(0)
    expect(out.steps[0]).not.toHaveProperty('bulges')
    const { inputs, ...second } = out.steps[1]
    expect(second).toEqual({ op: 'createLine' })
    const { x, ...rest } = inputs
    expect(x).toBeCloseTo(101.732050808, 6)
    expect(rest).toEqual({ y: 0, x2: 1e9, y2: 0, layer: 'A' })
    expect(trimEntity(line('l', [0, 0], [1e12, 0]), edge, 100, 0, 1e-9).refusal).toMatch(/beyond 1e9/)
  })

  it('keeps both crossings and retained arcs of a tiny semicircle cut by a chord', () => {
    const target = poly('p', [[-0.00001, 0], [0.00001, 0]], false, 'A', [1, 0])
    const edge = line('e', [-0.00002, -0.000006], [0.00002, -0.000006])
    // The radius-1e-5 lower semicircle meets y = -6e-6 at x = +/-8e-6.
    const hits = crossings(curveOf(target), curveOf(edge))
    expect(hits).toHaveLength(2)
    expect(hits[0].s).toBeCloseTo(0.204833, 6)
    expect(hits[1].s).toBeCloseTo(0.795167, 6)
    const out = trimEntity(target, edge, 0, -0.00001, 1e-9)
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(2)
    const { points, bulges, ...first } = out.steps[0]
    expect(first).toEqual({ op: 'setVertices', entityId: 'p', closed: false })
    expect(points).toHaveLength(2)
    ;[[-0.00001, 0], [-0.000008, -0.000006]].forEach((p, i) => {
      expect(points[i]).toHaveLength(2)
      p.forEach((v, j) => expect(points[i][j]).toBeCloseTo(v, 9))
    })
    // Each retained 36.869898-degree arc has bulge sqrt(10) - 3.
    expect(bulges).toHaveLength(2)
    expect(bulges[0]).toBeCloseTo(0.1622776602, 9)
    expect(bulges[1]).toBe(0)
    const { inputs, ...second } = out.steps[1]
    expect(second).toEqual({ op: 'createPolyline' })
    const { bulges: createdBulges, pts, ...rest } = inputs
    expect(rest).toEqual({ closed: false, layer: 'A' })
    const createdPoints = parsePts(pts)
    expect(createdPoints).toHaveLength(2)
    ;[[0.000008, -0.000006], [0.00001, 0]].forEach((p, i) => {
      expect(createdPoints[i]).toHaveLength(2)
      p.forEach((v, j) => expect(createdPoints[i][j]).toBeCloseTo(v, 9))
    })
    expect(createdBulges).toHaveLength(2)
    expect(createdBulges[0]).toBeCloseTo(0.1622776602, 9)
    expect(createdBulges[1]).toBe(0)
  })
})

describe('Astra refutations, round four (W4g-6e record 7)', () => {
  it('keeps both crossings and retained arcs with a cutter at a billion units', () => {
    const target = poly('p', [[-5, 0], [5, 0]], false, 'A', [1, 0])
    const edge = line('e', [-1e9, -3], [1e9, -3])
    // The radius-5 lower semicircle meets y = -3 at x = +/-4.
    const hits = crossings(curveOf(target), curveOf(edge))
    expect(hits).toHaveLength(2)
    expect(hits[0].s).toBeCloseTo(0.204833, 6)
    expect(hits[1].s).toBeCloseTo(0.795167, 6)
    ;[[-4, -3], [4, -3]].forEach((p, i) => {
      expect(hits[i].p).toHaveLength(2)
      p.forEach((v, j) => expect(hits[i].p[j]).toBeCloseTo(v, 6))
    })
    const out = trimEntity(target, edge, -3, -4, 1e-9)
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(2)
    const { points, bulges, ...first } = out.steps[0]
    expect(first).toEqual({ op: 'setVertices', entityId: 'p', closed: false })
    expect(points).toHaveLength(2)
    ;[[-5, 0], [-4, -3]].forEach((p, i) => {
      expect(points[i]).toHaveLength(2)
      p.forEach((v, j) => expect(points[i][j]).toBeCloseTo(v, 6))
    })
    expect(bulges).toHaveLength(2)
    expect(bulges[0]).toBeCloseTo(0.1622776602, 9)
    expect(bulges[1]).toBe(0)
    const { inputs, ...second } = out.steps[1]
    expect(second).toEqual({ op: 'createPolyline' })
    const { bulges: createdBulges, pts, ...rest } = inputs
    expect(rest).toEqual({ closed: false, layer: 'A' })
    const createdPoints = parsePts(pts)
    expect(createdPoints).toHaveLength(2)
    ;[[4, -3], [5, 0]].forEach((p, i) => {
      expect(createdPoints[i]).toHaveLength(2)
      p.forEach((v, j) => expect(createdPoints[i][j]).toBeCloseTo(v, 9))
    })
    expect(createdBulges).toHaveLength(2)
    expect(createdBulges[0]).toBeCloseTo(0.1622776602, 9)
    expect(createdBulges[1]).toBe(0)
  })

  it('refuses the cutter at two to the thirtieth before emitting steps', () => {
    const target = poly('p', [[-5, 0], [5, 0]], false, 'A', [1, 0])
    const out = trimEntity(target, line('e', [-1073741824, -3], [1073741824, -3]), -3, -4, 1e-9)
    expect(out.refusal).toMatch(/beyond 1e9/)
    expect(out).not.toHaveProperty('steps')
  })

  it('keeps a tangent at scale as one snapped vertex crossing', () => {
    const target = poly('k', [[0, 0], [10, 0], [15, 5]], false, 'A', [0, Math.tan(Math.PI / 8), 0])
    const edge = line('s', [-1e8, 0], [10, 0])
    expect(crossings(curveOf(target), curveOf(edge))).toEqual([{ s: 1, p: [10, 0] }])
  })

  it('keeps the exact endpoints of an arc with a chord below the drawing precision', () => {
    const target = poly('p', [[0, 0], [4e-10, 0], [10, 0], [20, 0]], false, 'A', [1e11, 0, 0, 0])
    const out = trimEntity(target, line('e', [15, -1], [15, 1]), 19, 0, 1e-9)
    expect(out).toEqual({ steps: [{
      op: 'setVertices', entityId: 'p', points: [[0, 0], [4e-10, 0], [10, 0], [15, 0]], closed: false,
      bulges: [1e11, 0, 0, 0],
    }] })
    expect(out.steps[0].points[1][0]).toBe(4e-10)
  })

  it('bounds pick coordinates, vertices and round radii at 1e9', () => {
    expect(MAX_COORD).toBe(1e9)
    const target = poly('p', [[0, 0], [10, 0]], false, 'A', [1, 0])
    expect(trimEntity(target, line('e', [5, -6], [5, 6]), 2e9, 0).refusal).toMatch(/within 1e9/)
    expect(curveOf(circle('c', [0, 0], 2e9)).refusal).toMatch(/larger than 1e9/)
    expect(curveOf(line('l', [0, 0], [2e9, 0])).refusal).toMatch(/beyond 1e9/)
  })
})

describe('Astra refutations, round five (W4g-6e record 8)', () => {
  it('accepts cutter endpoints only within a world-unit tolerance of the arc', () => {
    const target = poly('p', [[-5, 0], [5, 0]], false, 'A', [1, 0])
    const edge = line('e', [0, -1e9], [0, -5.5])
    expect(crossings(curveOf(target), curveOf(edge))).toEqual([])
    expect(trimEntity(target, edge, -3, -4, 1e-9).refusal).toBe('Trim refused: the cutting edge does not cross the selection.')
    const edge2 = line('e', [0, -1e9], [0, -4.9999999995])
    const hits = crossings(curveOf(target), curveOf(edge2))
    expect(hits).toHaveLength(1)
    expect(hits[0].s).toBeCloseTo(0.5, 9)
    expect(hits[0].p).toHaveLength(2)
    ;[0, -5].forEach((v, j) => expect(hits[0].p[j]).toBeCloseTo(v, 9))
    const out = trimEntity(target, edge2, -3, -4, 1e-9)
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(1)
    const { points, bulges, ...shape } = out.steps[0]
    expect(shape).toEqual({ op: 'setVertices', entityId: 'p', closed: false })
    expect(points).toHaveLength(2)
    ;[[0, -5], [5, 0]].forEach((p, i) => {
      expect(points[i]).toHaveLength(2)
      p.forEach((v, j) => expect(points[i][j]).toBeCloseTo(v, 9))
    })
    expect(bulges).toHaveLength(2)
    expect(bulges[0]).toBeCloseTo(Math.tan(Math.PI / 8), 9)
    expect(bulges[1]).toBe(0)
  })

  it('keeps the exact endpoints of a tiny chord with a huge bulge', () => {
    const target = poly('p', [[0, 0], [1.4e-9, 0], [10, 0], [20, 0]], false, 'A', [1e10, 0, 0, 0])
    const out = trimEntity(target, line('e', [15, -1], [15, 1]), 19, 0, 1e-9)
    expect(out).toEqual({ steps: [{
      op: 'setVertices', entityId: 'p', points: [[0, 0], [1.4e-9, 0], [10, 0], [15, 0]], closed: false,
      bulges: [1e10, 0, 0, 0],
    }] })
    expect(out.steps[0].points[1][0]).toBe(1.4e-9)
  })

  it('creates the far curved piece with full endpoint precision', () => {
    const target = poly('p', [[0, 0], [10, 0]], false, 'A', [1, 0])
    const edge = line('y', [0, -4.330127018922193], [10, -4.330127018922193])
    const out = trimEntity(target, edge, 5, -5)
    expect(out.refusal).toBeUndefined()
    expect(out.steps).toHaveLength(2)
    const { inputs, ...shape } = out.steps[1]
    expect(shape).toEqual({ op: 'createPolyline' })
    const { pts, bulges, ...rest } = inputs
    expect(rest).toEqual({ closed: false, layer: 'A' })
    const points = parsePts(pts)
    expect(points).toHaveLength(2)
    ;[[7.5, -4.330127018922193], [10, 0]].forEach((p, i) => {
      expect(points[i]).toHaveLength(2)
      p.forEach((v, j) => expect(points[i][j]).toBeCloseTo(v, 12))
    })
    expect(bulges).toHaveLength(2)
    expect(bulges[0]).toBeCloseTo(Math.tan(Math.PI / 12), 9)
    expect(bulges[1]).toBe(0)
  })

  it('refuses a bulged arc larger than the coordinate bound', () => {
    expect(curveOf(poly('p', [[0, 0], [1, 0]], false, 'A', [4e9, 0])).refusal).toMatch(/arc larger than 1e9/)
    expect(curveOf(poly('p', [[0, 0], [1, 0]], false, 'A', [1e3, 0])).refusal).toBeUndefined()
  })
})
