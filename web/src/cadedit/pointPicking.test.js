import { describe, expect, it } from 'vitest'
import { entityToPolyline } from './engineIntake.js'

import {
  MAX_SNAP_POINTS, PICK_SEQUENCES, SNAP_KIND, applyPick, buildSnapIndex, currentStep, ghostFor, orthoAnchor, orthoPoint, snapPoint, startPicking, wantsPick,
} from './pointPicking.js'

describe('pointPicking (W4f slice A1): clicks on the drawing answer the prompts', () => {
  it('a line takes two points, then wants nothing more; the ghost runs from the first point to the cursor', () => {
    let s = startPicking('createLine')
    expect(currentStep(s).keys).toEqual(['x', 'y'])
    expect(ghostFor(s, 5, 5)).toBeNull()
    let r = applyPick(s, 10.12345, -3, {})
    expect(r.writes).toEqual([['x', '10.123'], ['y', '-3']])
    s = r.state
    expect(ghostFor(s, 20, 4)).toEqual({ pts: [[10.12345, -3], [20, 4]], closed: false })
    r = applyPick(s, 20, 4, {})
    expect(r.writes).toEqual([['x2', '20'], ['y2', '4']])
    s = r.state
    expect(wantsPick(s)).toBe(false)
    expect(ghostFor(s, 30, 30)).toBeNull()
    expect(applyPick(s, 1, 1, {}).writes).toEqual([])
  })

  it('a circle takes its centre then a radius point; the ghost is the circle under the cursor', () => {
    let s = startPicking('createCircle')
    s = applyPick(s, 0, 0, {}).state
    const ghost = ghostFor(s, 3, 4)
    expect(ghost.closed).toBe(true)
    expect(ghost.pts).toHaveLength(48)
    for (const [x, y] of ghost.pts) expect(Math.abs(Math.hypot(x, y) - 5)).toBeLessThan(1e-9)
    const r = applyPick(s, 3, 4, {})
    expect(r.writes).toEqual([['r', '5']])
    expect(wantsPick(r.state)).toBe(false)
    // A radius click ON the centre is refused (nothing written, same step).
    expect(applyPick(s, 0, 0, {})).toEqual({ state: s, writes: [] })
    expect(PICK_SEQUENCES.createArc[1].kind).toBe('radius')
  })

  it('a polyline appends every click, the first click replacing the sample list, and always wants more', () => {
    let s = startPicking('createPolyline')
    let r = applyPick(s, 0, 0, { pts: '0,0 100,0 100,50' })
    expect(r.writes).toEqual([['pts', '0,0']])
    s = r.state
    r = applyPick(s, 10, 0, { pts: '0,0' })
    expect(r.writes).toEqual([['pts', '0,0 10,0']])
    s = r.state
    r = applyPick(s, 10, 4.5, { pts: '0,0 10,0' })
    expect(r.writes).toEqual([['pts', '0,0 10,0 10,4.5']])
    expect(wantsPick(r.state)).toBe(true)
    expect(ghostFor(r.state, 0, 4)).toEqual({ pts: [[0, 0], [10, 0], [10, 4.5], [0, 4]], closed: false })
  })

  it('a move takes a base point then a destination and writes the displacement; the ghost is base to cursor', () => {
    let s = startPicking('move')
    let r = applyPick(s, 5, 5, {})
    expect(r.writes).toEqual([])
    s = r.state
    expect(ghostFor(s, 8, 9)).toEqual({ pts: [[5, 5], [8, 9]], closed: false })
    r = applyPick(s, 8, 9, {})
    expect(r.writes).toEqual([['dx', '3'], ['dy', '4']])
    expect(wantsPick(r.state)).toBe(false)
    expect(startPicking('moveVertex').sequence).toEqual(PICK_SEQUENCES.moveVertex)
  })

  it('ops with nothing to pick, and non-finite clicks, are refused without writes', () => {
    const none = startPicking('deleteVertex')
    expect(none.sequence).toBeNull()
    expect(wantsPick(none)).toBe(false)
    expect(applyPick(none, 1, 1, {}).writes).toEqual([])
    const line = startPicking('createLine')
    expect(applyPick(line, NaN, 1, {})).toEqual({ state: line, writes: [] })
    expect(applyPick(line, 1, Infinity, {})).toEqual({ state: line, writes: [] })
    expect(ghostFor(line, NaN, 1)).toBeNull()
    expect(applyPick(startPicking('createLine'), -0.00001, 2, {}).writes).toEqual([['x', '0'], ['y', '2']])
  })

  it('a chain point opens LINE at its next-point step, the ghost runs from it, one click finishes (W4f-3)', () => {
    const s = startPicking('createLine', [10, 20])
    expect(s.step).toBe(1)
    expect(currentStep(s)).toEqual({ kind: 'point', keys: ['x2', 'y2'] })
    expect(ghostFor(s, 30, 40)).toEqual({ pts: [[10, 20], [30, 40]], closed: false })
    const { state, writes } = applyPick(s, 30, 40, {})
    expect(writes).toEqual([['x2', '30'], ['y2', '40']])
    expect(wantsPick(state)).toBe(false)
    // A chain point that is not finite, or not a pair, or an op whose first
    // step is not a point, opens normally.
    expect(startPicking('createLine', [NaN, 1]).step).toBe(0)
    expect(startPicking('createLine', [1]).step).toBe(0)
    expect(startPicking('createLine', null).step).toBe(0)
    expect(startPicking('move', [1, 2]).step).toBe(0)
    expect(startPicking('createPolyline', [1, 2]).step).toBe(0)
  })

  it('ORTHO snaps the cursor to the axis of the larger delta from the last point or the base; a first point is free (W4f-4)', () => {
    // A first point has nothing to be orthogonal to.
    let s = startPicking('createLine')
    expect(orthoAnchor(s)).toBeNull()
    expect(orthoPoint(s, 3.5, -2)).toEqual([3.5, -2])
    s = applyPick(s, 10, 10, {}).state
    expect(orthoAnchor(s)).toEqual([10, 10])
    // Larger horizontal move: keep x, hold y; larger vertical: hold x, keep y; a tie is horizontal.
    expect(orthoPoint(s, 25, 13)).toEqual([25, 10])
    expect(orthoPoint(s, 12, 30)).toEqual([10, 30])
    expect(orthoPoint(s, 15, 5)).toEqual([15, 10])
    // A chained LINE is anchored at its chain point.
    expect(orthoPoint(startPicking('createLine', [4, 4]), 4.5, 9)).toEqual([4, 9])
    // A displacement is anchored at its base.
    let m = startPicking('move')
    expect(orthoAnchor(m)).toBeNull()
    m = applyPick(m, 2, 2, {}).state
    expect(orthoAnchor(m)).toEqual([2, 2])
    expect(orthoPoint(m, 9, 3)).toEqual([9, 2])
    // Non-finite cursors pass through untouched (applyPick refuses them itself).
    expect(orthoPoint(s, NaN, 1)).toEqual([NaN, 1])
    expect(orthoPoint({ op: 'deleteVertex', sequence: null, step: 0, picked: [], base: null }, 1, 2)).toEqual([1, 2])
  })

  it('OSNAP packs endpoints, midpoints and centres once, bounded, and finds the nearest within reach, endpoints first (W4f-5)', () => {
    const entities = [
      { id: 'l', type: 'LINE', layer: '0', vertices: [[0, 0], [10, 0]] },
      { id: 'p', type: 'LWPOLYLINE', layer: '0', closed: true, vertices: [[20, 0], [30, 0], [30, 10]] },
      { id: 'c', type: 'CIRCLE', layer: '0', vertices: [[50, 50]], radius: 5 },
      { id: 'o', type: 'OTHER', layer: '0', vertices: [[99, 99]] },
      { id: 'bad', type: 'LINE', layer: '0', vertices: [[NaN, 1], [2, Infinity]] },
    ]
    const index = buildSnapIndex(entities)
    // LINE: 2 ends + 1 mid; closed triangle: 3 ends + 3 mids; circle: 1
    // centre + 4 quadrants (W4f-5b); OTHER and non-finite: nothing.
    expect(index.n).toBe(14)
    expect(index.truncated).toBe(false)
    expect(snapPoint(index, 9.6, 0.3, 1)).toEqual({ x: 10, y: 0, kind: 'endpoint' })
    expect(snapPoint(index, 5.2, -0.4, 1)).toEqual({ x: 5, y: 0, kind: 'midpoint' })
    expect(snapPoint(index, 25.1, 4.9, 1)).toEqual({ x: 25, y: 5, kind: 'midpoint' })
    expect(snapPoint(index, 49, 51, 2)).toEqual({ x: 50, y: 50, kind: 'centre' })
    expect(snapPoint(index, 55.3, 49.8, 1)).toEqual({ x: 55, y: 50, kind: 'quadrant' })
    expect(snapPoint(index, 50.2, 44.9, 1)).toEqual({ x: 50, y: 45, kind: 'quadrant' })
    // A circle with no usable radius keeps only its centre; an arc has its
    // centre, both endpoints and its midpoint, sweeping counter-clockwise
    // (an end below the start wraps through 360).
    expect(buildSnapIndex([{ id: 'c0', type: 'CIRCLE', layer: '0', vertices: [[1, 1]], radius: 0 }]).n).toBe(1)
    expect(buildSnapIndex([{ id: 'cn', type: 'CIRCLE', layer: '0', vertices: [[1, 1]] }]).n).toBe(1)
    const arc = buildSnapIndex([{ id: 'a', type: 'ARC', layer: '0', vertices: [[0, 0]], radius: 10, startDeg: 0, endDeg: 90 }])
    expect(arc.n).toBe(4)
    expect(snapPoint(arc, 9.8, 0.3, 1)).toEqual({ x: 10, y: 0, kind: 'endpoint' })
    expect(snapPoint(arc, 0.2, 9.7, 1)).toEqual({ x: expect.closeTo(0, 9), y: 10, kind: 'endpoint' })
    const mid = snapPoint(arc, 7, 7, 1)
    expect(mid.kind).toBe('midpoint')
    expect(mid.x).toBeCloseTo(10 * Math.SQRT1_2, 9)
    expect(mid.y).toBeCloseTo(10 * Math.SQRT1_2, 9)
    const wrap = buildSnapIndex([{ id: 'w', type: 'ARC', layer: '0', vertices: [[0, 0]], radius: 10, startDeg: 270, endDeg: 90 }])
    expect(snapPoint(wrap, 9.9, 0.1, 1)).toEqual({ x: 10, y: expect.closeTo(0, 9), kind: 'midpoint' })
    // Out of reach, or nothing to search, or a bad tolerance: nothing.
    expect(snapPoint(index, 9.6, 0.3, 0.3)).toBeNull()
    expect(snapPoint(index, 70, 70, 1)).toBeNull()
    expect(snapPoint(buildSnapIndex([]), 0, 0, 1)).toBeNull()
    expect(snapPoint(index, 0, 0, 0)).toBeNull()
    expect(snapPoint(index, NaN, 0, 1)).toBeNull()
    expect(snapPoint(null, 0, 0, 1)).toBeNull()
    // An endpoint beats a midpoint at equal distance: the point (5, 0) is a
    // midpoint; a candidate LINE ending there too makes it an endpoint too.
    const tie = buildSnapIndex([...entities, { id: 't', type: 'LINE', layer: '0', vertices: [[5, 0], [5, 9]] }])
    expect(snapPoint(tie, 5, 0.1, 1)).toEqual({ x: 5, y: 0, kind: 'endpoint' })
    // Bounded: a document past the cap keeps the first MAX_SNAP_POINTS candidates and says so.
    const many = Array.from({ length: MAX_SNAP_POINTS }, (_, i) => ({ id: `m${i}`, type: 'LINE', layer: '0', vertices: [[i, 0], [i, 1]] }))
    const capped = buildSnapIndex(many)
    expect(capped.n).toBe(MAX_SNAP_POINTS)
    expect(capped.truncated).toBe(true)
  })
})

describe('OSNAP on curved polyline segments (W4g-6d follow-up)', () => {
  it('a bulged segment offers the ARC midpoint and its centre; a straight one the chord midpoint; a bad list reads as straight', () => {
    // Bulge 1 from (0,0) to (10,0) is the LOWER semicircle about (5,0) (the crate's convention): its midpoint is (5,-5).
    const curved = { id: '1', type: 'LWPOLYLINE', layer: '0', closed: false, editable: true, vertices: [[0, 0, 0], [10, 0, 0], [10, 10, 0]], bulges: [1, 0, 0], radius: null, startDeg: null, endDeg: null }
    const index = buildSnapIndex([curved])
    const at = []
    for (let i = 0; i < index.n; i += 1) at.push([index.xs[i], index.ys[i], index.kinds[i]])
    expect(at).toContainEqual([0, 0, SNAP_KIND.END])
    expect(at).toContainEqual([10, 0, SNAP_KIND.END])
    expect(at).toContainEqual([10, 10, SNAP_KIND.END])
    const mids = at.filter((p) => p[2] === SNAP_KIND.MID)
    expect(mids).toHaveLength(2)
    expect(mids[0][0]).toBeCloseTo(5, 9)
    expect(mids[0][1]).toBeCloseTo(-5, 9)
    expect(mids[1]).toEqual([10, 5, SNAP_KIND.MID])
    const centres = at.filter((p) => p[2] === SNAP_KIND.CENTRE)
    expect(centres).toHaveLength(1)
    expect(centres[0][0]).toBeCloseTo(5, 9)
    expect(centres[0][1]).toBeCloseTo(0, 9)
    // The same polyline read straight (no list, or a list that does not match) keeps the chord midpoints and no centre.
    for (const bulges of [undefined, [1], [1, 0]]) {
      const flat = buildSnapIndex([{ ...curved, bulges }])
      const kinds = [...flat.kinds]
      expect(kinds.filter((k) => k === SNAP_KIND.CENTRE)).toHaveLength(0)
      expect(kinds.filter((k) => k === SNAP_KIND.MID)).toHaveLength(2)
      expect([flat.xs[3], flat.ys[3]]).toEqual([5, 0])
    }
    // A closed polyline's closing segment carries the LAST vertex's bulge.
    const ring = { ...curved, closed: true, vertices: [[0, 0, 0], [10, 0, 0], [10, 10, 0]], bulges: [0, 0, -1] }
    const ringIndex = buildSnapIndex([ring])
    const ringAt = []
    for (let i = 0; i < ringIndex.n; i += 1) ringAt.push([ringIndex.xs[i], ringIndex.ys[i], ringIndex.kinds[i]])
    // The closing segment (10,10) -> (0,0) with bulge -1 is a clockwise semicircle about (5,5).
    const closingCentre = ringAt.find((p) => p[2] === SNAP_KIND.CENTRE)
    expect(closingCentre[0]).toBeCloseTo(5, 9)
    expect(closingCentre[1]).toBeCloseTo(5, 9)
    const closingMid = ringAt.filter((p) => p[2] === SNAP_KIND.MID)[2]
    expect(Math.hypot(closingMid[0] - 5, closingMid[1] - 5)).toBeCloseTo(Math.hypot(5, 5), 9)
    expect(closingMid[0]).toBeCloseTo(10, 9)
    expect(closingMid[1]).toBeCloseTo(0, 9)
  })

  it('a closed two-vertex polyline offers its closing ARC (Astra, adversarial read): drawn by the mapper, snapped here too; two straight sides still offer one chord midpoint', () => {
    const lens = { id: '2', type: 'LWPOLYLINE', layer: '0', closed: true, editable: true, vertices: [[0, 0, 0], [10, 0, 0]], bulges: [0, 1], radius: null, startDeg: null, endDeg: null }
    const index = buildSnapIndex([lens])
    const at = []
    for (let i = 0; i < index.n; i += 1) at.push([index.xs[i], index.ys[i], index.kinds[i]])
    expect(at.filter((p) => p[2] === SNAP_KIND.END)).toHaveLength(2)
    const mids = at.filter((p) => p[2] === SNAP_KIND.MID)
    expect(mids).toHaveLength(2)
    expect(mids[0]).toEqual([5, 0, SNAP_KIND.MID])
    expect(mids[1][0]).toBeCloseTo(5, 9)
    expect(mids[1][1]).toBeCloseTo(5, 9)
    const centres = at.filter((p) => p[2] === SNAP_KIND.CENTRE)
    expect(centres).toHaveLength(1)
    expect(centres[0][0]).toBeCloseTo(5, 9)
    expect(centres[0][1]).toBeCloseTo(0, 9)
    // Two straight sides: one chord midpoint, never the same point twice.
    const flat = buildSnapIndex([{ ...lens, bulges: [0, 0] }])
    expect([...flat.kinds].filter((k) => k === SNAP_KIND.MID)).toHaveLength(1)
    expect([...flat.kinds].filter((k) => k === SNAP_KIND.CENTRE)).toHaveLength(0)
    // The mapper draws that closing arc, so the two agree: its samples pass through (5, 5).
    const drawn = entityToPolyline(lens)
    expect(drawn.closed).toBe(true)
    expect(drawn.pts.some((p) => Math.abs(p[0] - 5) < 1e-9 && Math.abs(p[1] - 5) < 1e-9)).toBe(true)
    // The FIRST side curved and the closing side straight (Astra, round two): the arc's MID and CENTRE
    // AND the straight side's chord midpoint. Bulge 0.5 on a chord of 10: radius 6.25, centre (5, 3.75),
    // midpoint (5, -2.5).
    const firstCurved = buildSnapIndex([{ ...lens, bulges: [0.5, 0] }])
    const fc = []
    for (let i = 0; i < firstCurved.n; i += 1) fc.push([firstCurved.xs[i], firstCurved.ys[i], firstCurved.kinds[i]])
    const fcMids = fc.filter((p) => p[2] === SNAP_KIND.MID)
    expect(fcMids).toHaveLength(2)
    expect(fcMids[0][0]).toBeCloseTo(5, 9)
    expect(fcMids[0][1]).toBeCloseTo(-2.5, 9)
    expect(fcMids[1]).toEqual([5, 0, SNAP_KIND.MID])
    const fcCentres = fc.filter((p) => p[2] === SNAP_KIND.CENTRE)
    expect(fcCentres).toHaveLength(1)
    expect(fcCentres[0][0]).toBeCloseTo(5, 9)
    expect(fcCentres[0][1]).toBeCloseTo(3.75, 9)
    // Both sides curved: two arcs, two centres.
    const both = buildSnapIndex([{ ...lens, bulges: [1, 1] }])
    expect([...both.kinds].filter((k) => k === SNAP_KIND.MID)).toHaveLength(2)
    expect([...both.kinds].filter((k) => k === SNAP_KIND.CENTRE)).toHaveLength(2)
    // A non-boolean closed flag reads as open, as it does for the drawing.
    const truthy = buildSnapIndex([{ ...lens, closed: 1, bulges: [0, 1] }])
    expect([...truthy.kinds].filter((k) => k === SNAP_KIND.MID)).toHaveLength(1)
    expect([...truthy.kinds].filter((k) => k === SNAP_KIND.CENTRE)).toHaveLength(0)
  })
})
