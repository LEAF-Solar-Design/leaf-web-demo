import { describe, expect, it } from 'vitest'
import { snapPrimitives, nearestOnPrimitive, perpendicularCandidates, tangentCandidates, intersectionCandidates } from './snapGeometry.js'
import { buildSnapIndex, snapPoint } from './pointPicking.js'

const line = (a = [0, 0], b = [10, 0]) => snapPrimitives({ type: 'LINE', vertices: [a, b] })[0]
const circle = (c = [0, 0], radius = 5) => snapPrimitives({ type: 'CIRCLE', vertices: [c], radius })[0]
const arc = (startDeg = 0, endDeg = 90, c = [0, 0], radius = 5) => snapPrimitives({ type: 'ARC', vertices: [c], radius, startDeg, endDeg })[0]
const bulged = () => snapPrimitives({ type: 'LWPOLYLINE', vertices: [[0, 0], [10, 0]], bulges: [1, 0], closed: false })[0]
const near = (actual, expected) => {
  expect(actual).toHaveLength(expected.length)
  expected.forEach((value, i) => expect(Math.abs(actual[i] - value)).toBeLessThanOrEqual(1e-9))
}
const nearPoints = (actual, expected) => {
  expect(actual).toHaveLength(expected.length)
  expected.forEach((p, i) => near(actual[i], p))
}

describe('snap geometry', () => {
  it('OSG01 nearest finite line', () => {
    expect(nearestOnPrimitive(line(), [3, 4])).toEqual([3, 0])
    expect(nearestOnPrimitive(line(), [12, 4])).toEqual([10, 0])
    expect(nearestOnPrimitive(line(), [-2, 1])).toEqual([0, 0])
  })
  it('OSG02 nearest circle and centre', () => {
    near(nearestOnPrimitive(circle(), [6, 8]), [3, 4])
    expect(nearestOnPrimitive(circle(), [0, 0])).toEqual([5, 0])
  })
  it('OSG03 nearest finite and wrapped arc', () => {
    near(nearestOnPrimitive(arc(), [6, 8]), [3, 4])
    near(nearestOnPrimitive(arc(), [-6, 8]), [0, 5])
    near(nearestOnPrimitive(arc(270, 90), [10, 0]), [5, 0])
  })
  it('OSG04 nearest bulged segment', () => near(nearestOnPrimitive(bulged(), [5, -8]), [5, -5]))
  it('OSG05 invalid cursors', () => {
    for (const prim of [line(), circle(), arc(), bulged()]) {
      for (const p of [[NaN, 0], [0, Infinity], [1], null]) expect(nearestOnPrimitive(prim, p)).toBeNull()
    }
  })
  it('OSG06 perpendicular straight feet', () => {
    expect(perpendicularCandidates(line([2, 0], [2, 10]), [0, 5])).toEqual([[2, 5]])
    expect(perpendicularCandidates(line([0, 0], [10, 10]), [0, 10])).toEqual([[5, 5]])
  })
  it('OSG07 perpendicular refusals', () => {
    for (const p of [[0, 12], [2, 5], undefined]) expect(perpendicularCandidates(line([2, 0], [2, 10]), p)).toEqual([])
  })
  it('OSG08 circular perpendicular candidates', () => {
    expect(perpendicularCandidates(circle(), [10, 0])).toEqual([[5, 0], [-5, 0]])
    expect(perpendicularCandidates(arc(), [10, 0])).toEqual([[5, 0]])
    expect(perpendicularCandidates(circle(), [0, 0])).toEqual([])
  })
  it('OSG09 ordered circle tangents', () => {
    nearPoints(tangentCandidates(circle(), [10, 0]), [[2.5, 4.330127018922193], [2.5, -4.330127018922193]])
    nearPoints(tangentCandidates(circle(), [0, 13]), [[-4.615384615384615, 1.9230769230769231], [4.615384615384615, 1.9230769230769231]])
  })
  it('OSG10 finite arc tangent', () => nearPoints(tangentCandidates(arc(), [10, 0]), [[2.5, 4.330127018922193]]))
  it('OSG11 tangent refusals', () => {
    for (const p of [[3, 0], [5, 0], [NaN, 0], [0, Infinity], null]) expect(tangentCandidates(circle(), p)).toEqual([])
    expect(tangentCandidates(line(), [10, 10])).toEqual([])
  })
  it('OSG12 line crossing', () => expect(intersectionCandidates(line([0, 0], [10, 10]), line([0, 10], [10, 0]))).toEqual([[5, 5]]))
  it('OSG13 line circle crossings', () => {
    expect(intersectionCandidates(line([-10, 0], [10, 0]), circle()).sort((a, b) => a[0] - b[0])).toEqual([[-5, 0], [5, 0]])
  })
  it('OSG14 circle circle crossings', () => {
    nearPoints(intersectionCandidates(circle(), circle([6, 0])).sort((a, b) => a[1] - b[1]), [[3, -4], [3, 4]])
  })
  it('OSG15 finite arc crossing in both orders', () => {
    const a = arc(), b = line([-10, 0], [10, 0])
    expect(intersectionCandidates(a, b)).toEqual([[5, 0]])
    expect(intersectionCandidates(b, a)).toEqual([[5, 0]])
  })
  it('OSG16 arc excludes opposite tangent', () => expect(intersectionCandidates(arc(), line([-5, -10], [-5, 10]))).toEqual([]))
  it('OSG17 parallel and collinear lines', () => {
    expect(intersectionCandidates(line(), line([0, 1], [10, 1]))).toEqual([])
    expect(intersectionCandidates(line(), line([5, 0], [15, 0]))).toEqual([])
  })
  it('OSG18 bulged crossing', () => nearPoints(intersectionCandidates(bulged(), line([5, -10], [5, 10])), [[5, -5]]))
  it('OSG19 primitive public fields', () => {
    const ps = snapPrimitives({ type: 'LINE', vertices: [[0, 0], [10, 10]] }, 7)
    expect(ps).toHaveLength(1)
    expect(ps[0]).toMatchObject({ source: 7, part: 0, box: [0, 0, 10, 10], circular: false })
    expect(Object.isFrozen(ps)).toBe(true)
  })
  it('OSG20 polyline closing segment', () => {
    const e = { type: 'LWPOLYLINE', vertices: [[0, 0], [10, 0], [10, 10], [0, 10]], closed: true }
    expect(snapPrimitives(e).map((p) => p.part)).toEqual([0, 1, 2, 3])
    expect(snapPrimitives({ ...e, closed: false }).map((p) => p.part)).toEqual([0, 1, 2])
  })
  it('OSG21 conservative circular boxes and huge angles', () => {
    expect(bulged().circular).toBe(true)
    expect(bulged().box).toEqual([0, -5, 10, 5])
    for (const p of [circle([2, 3], 4), arc(0, 90, [2, 3], 4)]) {
      expect(p.circular).toBe(true)
      expect(p.box).toEqual([-2, -1, 6, 7])
    }
    expect(snapPrimitives({ type: 'ARC', vertices: [[0, 0]], radius: 5, startDeg: 1e19, endDeg: 90 })).toHaveLength(1)
  })
  it('OSG22 refused entity shapes', () => {
    for (const type of ['POINT', 'ELLIPSE', 'INSERT', 'TEXT', 'MTEXT', 'UNKNOWN']) expect(snapPrimitives({ type })).toEqual([])
    expect(snapPrimitives(null)).toEqual([])
    expect(snapPrimitives({ type: 'LINE', vertices: [[NaN, 0], [1, 1]] })).toEqual([])
    expect(snapPrimitives({ type: 'LWPOLYLINE', vertices: Array.from({ length: 1001 }, (_, i) => [i, 0]) })).toEqual([])
    expect(snapPrimitives({ type: 'LINE', vertices: [[2e9, 0], [1, 1]] })).toEqual([])
  })
  it('OSG23 invalid arc angle and zero line', () => {
    expect(snapPrimitives({ type: 'ARC', vertices: [[0, 0]], radius: 5, startDeg: Infinity, endDeg: 90 })).toEqual([])
    expect(snapPrimitives({ type: 'LINE', vertices: [[0, 0], [0, 0]] })).toEqual([])
  })
})

// B1b: the four mutation gaps the record owed, through the public index and
// query (pointPicking.js). Each expectation is the record's measured base
// result (section 5) and fails under its named mutant.
describe('snap query ordering and bounds (B1b)', () => {
  const L = (a, b) => ({ type: 'LINE', vertices: [a, b] })
  const query = (entities, xy, tol, modes) => {
    const diagnostics = {}
    const hit = snapPoint(buildSnapIndex(entities), xy[0], xy[1], tol, { modes, diagnostics })
    return { hit, diagnostics }
  }
  const diag = (over) => ({ invalid: false, truncated: false, localOverflow: false, admitted: 0, omitted: 0, pairs: 0, ...over })

  it('B1B-G01 source tie-break', () => {
    // Two lines one unit either side of the cursor: the earlier SOURCE wins
    // the exact tie (mutant without the tie-break: (0, -1)).
    const { hit, diagnostics } = query([L([-10, 1], [10, 1]), L([-10, -1], [10, -1])], [0, 0], 2, 512)
    expect(hit).toEqual({ x: 0, y: 1, kind: 'nearest' })
    expect(diagnostics).toEqual(diag({ admitted: 2 }))
  })

  it('B1B-G02 part tie-break', () => {
    // One open polyline whose first and third parts tie: the earlier PART wins.
    const poly = { type: 'LWPOLYLINE', vertices: [[-10, 1], [10, 1], [10, -1], [-10, -1]], closed: false }
    const { hit, diagnostics } = query([poly], [0, 0], 2, 512)
    expect(hit).toEqual({ x: 0, y: 1, kind: 'nearest' })
    expect(diagnostics).toEqual(diag({ admitted: 2 }))
  })

  it('B1B-G03 closest 64', () => {
    // 64 lines at 0.5 fill the local heap first; the 65th, closer, must
    // displace one (mutant keeping the first 64: (0, 0.5)).
    const entities = [...Array.from({ length: 64 }, () => L([-10, 0.5], [10, 0.5])), L([-10, 0.125], [10, 0.125])]
    const { hit, diagnostics } = query(entities, [0, 0], 1, 512)
    expect(hit).toEqual({ x: 0, y: 0.125, kind: 'nearest' })
    expect(diagnostics).toEqual(diag({ admitted: 64, omitted: 1, localOverflow: true }))
  })

  it('B1B-G04 insertion truncation hit and miss', () => {
    // 20,001 single-line texts overflow the insertion budget by one: the
    // query reports truncation on a hit and on a miss alike.
    const texts = Array.from({ length: 20001 }, () => ({ type: 'TEXT', vertices: [[3, 4]] }))
    const index = buildSnapIndex(texts)
    expect(index.n).toBe(0)
    expect(index.insertions.truncated).toBe(true)
    const hitDiagnostics = {}
    expect(snapPoint(index, 3, 4, 1, { modes: 64, diagnostics: hitDiagnostics })).toEqual({ x: 3, y: 4, kind: 'insertion' })
    expect(hitDiagnostics).toEqual(diag({ truncated: true }))
    const missDiagnostics = {}
    expect(snapPoint(index, 30, 40, 1, { modes: 64, diagnostics: missDiagnostics })).toBeNull()
    expect(missDiagnostics).toEqual(diag({ truncated: true }))
    // Insertion off (endpoints only): that budget is not this query's limit.
    const off = {}
    expect(snapPoint(index, 3, 4, 1, { modes: 1, diagnostics: off })).toBeNull()
    expect(off.truncated).toBe(false)
  })

  it('B1B-G05 huge arc residue', () => {
    // A start angle of 1e19 degrees is reduced exactly, never collapsed to
    // zero (mutant: nearest (5, 0)).
    const { hit, diagnostics } = query([{ type: 'ARC', vertices: [[0, 0]], radius: 5, startDeg: 1e19, endDeg: 90 }], [3, -4], 10, 512)
    expect(hit.kind).toBe('nearest')
    expect(Math.abs(hit.x - 3.000000000000002)).toBeLessThanOrEqual(1e-9)
    expect(Math.abs(hit.y - -3.9999999999999982)).toBeLessThanOrEqual(1e-9)
    expect(Math.hypot(hit.x - 5, hit.y)).toBeGreaterThan(1)
    expect(diagnostics).toEqual(diag({ admitted: 1 }))
  })
})

