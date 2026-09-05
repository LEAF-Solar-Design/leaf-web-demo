// W4g-5 OFFSET: the parallel copy's geometry, pure rows.
import { describe, expect, it } from 'vitest'

import { MAX_OFFSET_POINTS, MITER_LIMIT, offsetEntity, offsetSide } from './offset.js'

const line = (extra = {}) => ({ id: 'e1', type: 'LINE', layer: 'Panels', closed: false, vertices: [[0, 0, 0], [10, 0, 0]], radius: null, startDeg: null, endDeg: null, ...extra })
const circle = (extra = {}) => ({ id: 'e2', type: 'CIRCLE', layer: 'Round', closed: true, vertices: [[0, 0, 0]], radius: 5, startDeg: null, endDeg: null, ...extra })
const arc = (extra = {}) => ({ id: 'e3', type: 'ARC', layer: 'Round', closed: false, vertices: [[0, 0, 0]], radius: 5, startDeg: 0, endDeg: 90, ...extra })
// A unit square, counter-clockwise: its INSIDE is to the left of travel.
const square = (extra = {}) => ({ id: 'e4', type: 'LWPOLYLINE', layer: 'Outline', closed: true, vertices: [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]], radius: null, startDeg: null, endDeg: null, ...extra })
const open = (extra = {}) => ({ id: 'e5', type: 'LWPOLYLINE', layer: 'Outline', closed: false, vertices: [[0, 0, 0], [10, 0, 0], [10, 10, 0]], radius: null, startDeg: null, endDeg: null, ...extra })

const points = (pts) => pts.split(' ').map((p) => p.split(',').map(Number))

describe('offsetSide', () => {
  it('reads left and right of a line, outside and inside of a circle', () => {
    expect(offsetSide(line(), 5, 2)).toBe(1)
    expect(offsetSide(line(), 5, -2)).toBe(-1)
    expect(offsetSide(circle(), 9, 0)).toBe(1)
    expect(offsetSide(circle(), 1, 0)).toBe(-1)
    expect(offsetSide(arc(), 0, 9)).toBe(1)
  })

  it('names no side for a point ON the entity, or a malformed one', () => {
    expect(offsetSide(line(), 5, 0)).toBe(0)
    expect(offsetSide(circle(), 5, 0)).toBe(0)
    expect(offsetSide(line(), Number.NaN, 0)).toBe(0)
    expect(offsetSide({ type: 'LINE', vertices: [] }, 1, 1)).toBe(0)
  })

  it('reads the side from the NEAREST segment of a polyline', () => {
    // Just outside the square's right edge: right of that segment's travel.
    expect(offsetSide(square(), 12, 5)).toBe(-1)
    // Inside the square, nearest the same edge: left of travel.
    expect(offsetSide(square(), 9, 5)).toBe(1)
  })
})

describe('offsetEntity', () => {
  it('offsets a line to the picked side, keeping its layer', () => {
    expect(offsetEntity(line(), 2, 5, 3)).toEqual({
      op: 'createLine', inputs: { x: 0, y: 2, x2: 10, y2: 2, layer: 'Panels' },
    })
    expect(offsetEntity(line(), 2, 5, -3)).toEqual({
      op: 'createLine', inputs: { x: 0, y: -2, x2: 10, y2: -2, layer: 'Panels' },
    })
  })

  it('grows a circle outward and shrinks it inward, and keeps an arc sweep', () => {
    expect(offsetEntity(circle(), 2, 9, 0)).toEqual({
      op: 'createCircle', inputs: { x: 0, y: 0, r: 7, layer: 'Round' },
    })
    expect(offsetEntity(circle(), 2, 1, 0)).toEqual({
      op: 'createCircle', inputs: { x: 0, y: 0, r: 3, layer: 'Round' },
    })
    expect(offsetEntity(arc(), 1, 0, 9)).toEqual({
      op: 'createArc', inputs: { x: 0, y: 0, r: 6, a0: 0, a1: 90, layer: 'Round' },
    })
  })

  it('miters a closed polyline inward and outward', () => {
    const inward = offsetEntity(square(), 1, 9, 5)
    expect(inward.op).toBe('createPolyline')
    expect(inward.inputs.closed).toBe(true)
    expect(points(inward.inputs.pts)).toEqual([[1, 1], [9, 1], [9, 9], [1, 9]])
    const outward = offsetEntity(square(), 1, 12, 5)
    expect(points(outward.inputs.pts)).toEqual([[-1, -1], [11, -1], [11, 11], [-1, 11]])
  })

  it('offsets an open polyline, its ends square to their own segments', () => {
    const out = offsetEntity(open(), 1, 5, -1)
    expect(out.op).toBe('createPolyline')
    expect(out.inputs.closed).toBe(false)
    // Right of travel: below the first segment, then right of the second.
    expect(points(out.inputs.pts)).toEqual([[0, -1], [11, -1], [11, 10]])
  })

  it('refuses a distance that is not a positive number, before any arithmetic', () => {
    expect(offsetEntity(line(), 0, 5, 3).refusal).toMatch(/greater than 0/)
    expect(offsetEntity(line(), -2, 5, 3).refusal).toMatch(/greater than 0/)
    expect(offsetEntity(line(), 'wide', 5, 3).refusal).toMatch(/must be a number/)
    expect(offsetEntity(line(), Number.NaN, 5, 3).refusal).toMatch(/must be a number/)
  })

  it('refuses a click with no side, an unsupported kind and a degenerate source', () => {
    expect(offsetEntity(line(), 2, 5, 0).refusal).toMatch(/click to one side/)
    expect(offsetEntity({ type: 'TEXT', layer: '0', vertices: [[0, 0, 0]] }, 2, 1, 1).refusal).toMatch(/TEXT of this kind cannot be offset/)
    expect(offsetEntity({ type: 'LINE', layer: '0', vertices: [[0, 0, 0], [0, 0, 0]] }, 2, 1, 1).refusal).toMatch(/this line has zero length/)
    expect(offsetEntity(circle({ radius: 0 }), 2, 9, 0).refusal).toMatch(/no radius/)
  })

  it('refuses an inward circle offset that would leave no radius', () => {
    expect(offsetEntity(circle(), 5, 1, 0).refusal).toMatch(/would leave no radius \(the source is 5\)/)
    expect(offsetEntity(circle(), 9, 1, 0).refusal).toMatch(/would leave no radius/)
  })

  it('refuses a corner too sharp to miter and a polyline past the vertex bound', () => {
    // A 1-degree spike: the miter runs far past the limit.
    const spike = { type: 'LWPOLYLINE', layer: '0', closed: false, vertices: [[0, 0, 0], [100, 0, 0], [0, 1.7, 0]] }
    const refused = offsetEntity(spike, 1, 50, -1)
    expect(refused.refusal).toMatch(/corner 1 of this polyline is too sharp/)
    expect(MITER_LIMIT).toBe(10)
    const many = { type: 'LWPOLYLINE', layer: '0', closed: false, vertices: Array.from({ length: MAX_OFFSET_POINTS + 1 }, (_, i) => [i, i % 2, 0]) }
    expect(offsetEntity(many, 1, 5, -5).refusal).toMatch(new RegExp(`over the ${MAX_OFFSET_POINTS}`))
  })

  it('refuses a corner that folds back, and keeps a collinear one riding along', () => {
    // Out and straight back: the miter is at infinity, so there is no corner
    // to draw and a near point would be a spike the drafter never drew.
    const fold = { type: 'LWPOLYLINE', layer: '0', closed: false, vertices: [[0, 0, 0], [10, 0, 0], [2, 0, 0]] }
    expect(offsetEntity(fold, 1, 5, -1).refusal).toMatch(/corner 1 of this polyline folds back on itself/)
    // Three points on one line, same direction: two segments, one offset line.
    const straight = { type: 'LWPOLYLINE', layer: '0', closed: false, vertices: [[0, 0, 0], [5, 0, 0], [10, 0, 0]] }
    const out = offsetEntity(straight, 1, 5, -1)
    expect(out.refusal).toBeUndefined()
    expect(points(out.inputs.pts)).toEqual([[0, -1], [5, -1], [10, -1]])
  })

  it('refuses a zero-length segment instead of dividing by it', () => {
    const doubled = { type: 'LWPOLYLINE', layer: '0', closed: false, vertices: [[0, 0, 0], [0, 0, 0], [10, 0, 0]] }
    expect(offsetEntity(doubled, 1, 5, 1).refusal).toMatch(/segment 0 of this polyline has zero length/)
  })
})

describe('W4g-6d: a curved polyline is refused, never offset as its chords', () => {
  it('names the fault before asking for a side', () => {
    const curved = { id: 'c', type: 'LWPOLYLINE', layer: 'A', closed: false, editable: true, vertices: [[0, 0, 0], [10, 0, 0], [10, 10, 0]], bulges: [0, 0.5, 0], radius: null, startDeg: null, endDeg: null }
    expect(offsetEntity(curved, 1, 5, 1).refusal).toBe('Offset refused: this polyline has curved segments; not in this round.')
    expect(offsetEntity({ ...curved, bulges: [0, 0, 0] }, 1, 5, 1).refusal).toBeUndefined()
    expect(offsetEntity({ ...curved, bulges: [0, Number.NaN, 0] }, 1, 5, 1).refusal).toBe('Offset refused: this polyline has curved segments; not in this round.')
  })
})
