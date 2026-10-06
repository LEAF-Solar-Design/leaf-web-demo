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

describe('W4g-7b-02c: the INSERT ghost', () => {
  const FIXTURE = { name: 'Fixture', base: [1, 2, 0], children: [{ type: 'LINE', vertices: [[1, 2, 0], [4, 2, 0]] }], complete: true, baseUnknown: false, digest: 'd1' }
  const TWO_CHILD = { name: 'Two', base: [1, 2, 0], children: [{ type: 'LINE', vertices: [[1, 2, 0], [4, 2, 0]] }, { type: 'CIRCLE', vertices: [[2, 2, 0]], radius: 1 }], complete: true, baseUnknown: false, digest: 'd2' }

  it('PICK_SEQUENCES.createInsert picks one point', () => {
    expect(PICK_SEQUENCES.createInsert).toEqual([{ kind: 'point', keys: ['x', 'y'] }])
  })

  it('a one-segment definition is degenerate: the box collapses to its chord', () => {
    const s = startPicking('createInsert')
    const inputs = { name: 'Fixture', sx: '2', sy: '3', rot: '90' }
    expect(ghostFor(s, 10, 20, inputs, [FIXTURE])).toEqual({ pts: [[10, 20], [10, 26]], closed: false })
  })

  it('a two-child definition (a line plus a circle) draws the box of both', () => {
    const s = startPicking('createInsert')
    const inputs = { name: 'Two', sx: '', sy: '', rot: '' }
    // Local bbox: the line spans x 1..4, y 2..2; the circle (centre (2,2), r 1)
    // spans x 1..3, y 1..3. Combined: x 1..4, y 1..3. Defaults sx=sy=1, rot=0:
    // the box translates straight to the cursor (10,20).
    expect(ghostFor(s, 10, 20, inputs, [TWO_CHILD])).toEqual({
      pts: [[10, 19], [13, 19], [13, 21], [10, 21]], closed: true,
    })
  })

  it('no definition, an incomplete one, or no typed name draws no ghost', () => {
    const s = startPicking('createInsert')
    expect(ghostFor(s, 10, 20, { name: 'Nope' }, [FIXTURE])).toBeNull()
    expect(ghostFor(s, 10, 20, { name: '' }, [FIXTURE])).toBeNull()
    expect(ghostFor(s, 10, 20, { name: 'Fixture' }, [{ ...FIXTURE, complete: false }])).toBeNull()
    expect(ghostFor(s, 10, 20, { name: 'Fixture' }, [{ ...FIXTURE, baseUnknown: true }])).toBeNull()
  })

  // W4g-7b-02c-f: the catalogue side is trimmed too, so a definition whose own
  // spelling carries trailing whitespace still resolves against the typed name.
  it('a catalogue name with trailing whitespace still resolves against the trimmed typed name', () => {
    const s = startPicking('createInsert')
    const inputs = { name: 'Fixture', sx: '2', sy: '3', rot: '90' }
    const padded = { ...FIXTURE, name: 'Fixture ' }
    expect(ghostFor(s, 10, 20, inputs, [padded])).toEqual({ pts: [[10, 20], [10, 26]], closed: false })
  })
})

describe('W4g-7b-04c: the DIMLINEAR/DIMALIGNED pick sequence and ghost', () => {
  it('PICK_SEQUENCES: three point steps for both dimtypes', () => {
    expect(PICK_SEQUENCES.dimLinear).toEqual([
      { kind: 'point', keys: ['x', 'y'] }, { kind: 'point', keys: ['x2', 'y2'] }, { kind: 'point', keys: ['dx', 'dy'] },
    ])
    expect(PICK_SEQUENCES.dimAligned).toEqual(PICK_SEQUENCES.dimLinear)
  })

  it('the ghost runs point-to-cursor for the second pick, then the schematic once both def points are picked', () => {
    let s = startPicking('dimLinear')
    expect(ghostFor(s, 5, 5)).toBeNull()
    s = applyPick(s, 0, 0, {}).state
    expect(ghostFor(s, 3, 4)).toEqual({ pts: [[0, 0], [3, 4]], closed: false })
    s = applyPick(s, 3, 4, {}).state
    const ghost = ghostFor(s, 1.5, 6, { rot: '0' })
    // W4g-7b-04c-8: the crossbar joins the feet on the dimension line,
    // y=6 from x=0 to x=3, never the extension lines' ends at y=8.
    expect(ghost.closed).toBe(false)
    expect(ghost.pts).toEqual([[0, 0, 0], [0, 6, 0], [3, 6, 0], [3, 4, 0]])
    const r = applyPick(s, 1.5, 6, {})
    expect(r.writes).toEqual([['dx', '1.5'], ['dy', '6']])
    expect(wantsPick(r.state)).toBe(false)
  })

  it('ALIGNED ignores a typed rotation for its ghost', () => {
    let s = startPicking('dimAligned')
    s = applyPick(s, 0, 0, {}).state
    s = applyPick(s, 3, 4, {}).state
    const ghost = ghostFor(s, 1.5, 6, { rot: '45' })
    expect(ghost.pts[0]).toEqual([0, 0, 0])
    expect(ghost.pts[3]).toEqual([3, 4, 0])
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

import { bulgeArc } from './engineIntake.js'
import { DEFAULT_SNAP_MODES, ALL_SNAP_MODES } from './snapModes.js'

const finite = (v) => typeof v === 'number' && Number.isFinite(v)
const DEG = Math.PI / 180
const LEGACY_SNAP_KIND_NAME = Object.freeze(['endpoint', 'midpoint', 'centre', 'quadrant'])

function legacyArcSweepDeg(startDeg, endDeg) {
  let sweep = endDeg - startDeg
  while (sweep <= 0) sweep += 360
  while (sweep > 360) sweep -= 360
  return sweep
}

function legacyBuildSnapIndex(entities) {
  const xs = []
  const ys = []
  const kinds = []
  const push = (x, y, kind) => {
    if (xs.length >= MAX_SNAP_POINTS || !finite(x) || !finite(y)) return
    xs.push(x); ys.push(y); kinds.push(kind)
  }
  for (const e of Array.isArray(entities) ? entities : []) {
    const v = Array.isArray(e?.vertices) ? e.vertices : []
    if (e?.type === 'CIRCLE' || e?.type === 'ARC') {
      // The centre; then, with a finite positive radius, a circle's four
      // quadrants, or an arc's two endpoints and its midpoint (W4f-5b).
      const c = v[0]
      if (!c) continue
      const cx = c[0]
      const cy = c[1]
      push(cx, cy, SNAP_KIND.CENTRE)
      const r = e.radius
      if (!finite(r) || r <= 0) continue
      if (e.type === 'CIRCLE') {
        push(cx + r, cy, SNAP_KIND.QUADRANT)
        push(cx, cy + r, SNAP_KIND.QUADRANT)
        push(cx - r, cy, SNAP_KIND.QUADRANT)
        push(cx, cy - r, SNAP_KIND.QUADRANT)
      } else if (finite(e.startDeg) && finite(e.endDeg)) {
        const sweep = legacyArcSweepDeg(e.startDeg, e.endDeg)
        const at = (deg) => [cx + r * Math.cos(deg * DEG), cy + r * Math.sin(deg * DEG)]
        const a = at(e.startDeg)
        const b = at(e.startDeg + sweep)
        const m = at(e.startDeg + sweep / 2)
        push(a[0], a[1], SNAP_KIND.END)
        push(b[0], b[1], SNAP_KIND.END)
        push(m[0], m[1], SNAP_KIND.MID)
      }
      continue
    }
    // W4g-4b: a POINT is its own endpoint; an ELLIPSE offers its centre.
    if (e?.type === 'POINT') {
      if (v[0]) push(v[0][0], v[0][1], SNAP_KIND.END)
      continue
    }
    if (e?.type === 'ELLIPSE') {
      if (v[0]) push(v[0][0], v[0][1], SNAP_KIND.CENTRE)
      continue
    }
    if (e?.type !== 'LINE' && e?.type !== 'LWPOLYLINE') continue
    for (let i = 0; i < v.length; i += 1) push(v[i][0], v[i][1], SNAP_KIND.END)
    const bulges = Array.isArray(e.bulges) && e.bulges.length === v.length ? e.bulges : null
    // A closed polyline visits its closing segment. Two vertices closed by two
    // STRAIGHT sides would offer one chord midpoint twice, so that case alone
    // keeps one segment; when either side curves (a bulge on either vertex)
    // both sides are real and the drawing shows both.
    const curvedAt = (i) => !!(bulges && Number.isFinite(bulges[i]) && Math.abs(bulges[i]) > 1e-10)
    const twoSides = v.length === 2 && (curvedAt(0) || curvedAt(1))
    const segments = e.type === 'LWPOLYLINE' && e.closed === true && (v.length > 2 || twoSides) ? v.length : v.length - 1
    // A curved segment (a bulge on its start vertex, one per vertex, the
    // mapper's rule) offers the ARC's midpoint, not the chord's, and its
    // centre; a list that does not match the points reads as straight, as
    // it does for the drawing.
    for (let i = 0; i < segments; i += 1) {
      const a = v[i]
      const b = v[(i + 1) % v.length]
      if (!a || !b) continue
      const arc = bulges ? bulgeArc(a, b, bulges[i]) : null
      if (arc) {
        const mid = arc.a0 + arc.sweep / 2
        push(arc.cx + arc.r * Math.cos(mid), arc.cy + arc.r * Math.sin(mid), SNAP_KIND.MID)
        push(arc.cx, arc.cy, SNAP_KIND.CENTRE)
      } else push((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, SNAP_KIND.MID)
    }
  }
  return Object.freeze({ n: xs.length, xs: Float64Array.from(xs), ys: Float64Array.from(ys), kinds: Uint8Array.from(kinds), truncated: xs.length >= MAX_SNAP_POINTS })
}

/**
 * The nearest candidate within `tol` (world units) of (x, y), or null. One
 * linear pass over at most MAX_SNAP_POINTS (about 0.1 ms at the cap), no
 * allocation on a miss; an endpoint beats a midpoint or a centre at equal
 * distance. Non-finite input or tolerance finds nothing.
 */
function legacySnapPoint(index, x, y, tol) {
  if (!index || !index.n || !finite(x) || !finite(y) || !finite(tol) || tol <= 0) return null
  const { n, xs, ys, kinds } = index
  const tol2 = tol * tol
  let best = -1
  let bestD = Infinity
  for (let i = 0; i < n; i += 1) {
    const dx = xs[i] - x
    const dy = ys[i] - y
    const d = dx * dx + dy * dy
    if (d > tol2) continue
    if (d < bestD || (d === bestD && best >= 0 && kinds[i] < kinds[best])) { best = i; bestD = d }
  }
  if (best < 0) return null
  return { x: xs[best], y: ys[best], kind: LEGACY_SNAP_KIND_NAME[kinds[best]] }
}


const osLine = (a = [0, 0], b = [10, 0]) => ({ type: 'LINE', vertices: [a, b] })
const osCircle = (c = [0, 0], radius = 5) => ({ type: 'CIRCLE', vertices: [c], radius })
function osRandom(seed) {
  let state = seed
  return () => {
    state = (Math.imul(1664525, state) + 1013904223) >>> 0
    return state / 4294967296
  }
}
function oracleDocument(k) {
  const r = osRandom(0x1000 + k)
  const entities = []
  for (let i = 0; i < 200; i += 1) {
    const type = Math.floor(r() * 10)
    let e
    if (type <= 2) e = osLine([100 * r(), 100 * r()], [100 * r(), 100 * r()])
    else if (type === 3) {
      const count = 2 + Math.floor(r() * 5)
      const vertices = Array.from({ length: count }, () => [100 * r(), 100 * r()])
      const closed = r() < 0.5
      const bulges = r() < 0.5 ? vertices.map(() => r() < 0.5 ? 0 : 2 * r() - 1) : undefined
      e = { type: 'LWPOLYLINE', vertices, closed, bulges }
    } else if (type === 4) e = osCircle([100 * r(), 100 * r()], 10 * r())
    else if (type === 5) e = { type: 'ARC', vertices: [[100 * r(), 100 * r()]], radius: 10 * r() + 0.1, startDeg: 1440 * r() - 720, endDeg: 1440 * r() - 720 }
    else {
      const x = 100 * r(), y = 100 * r()
      e = type === 8 ? { type: 'INSERT', ip: [x, y, 0] }
        : { type: ['POINT', 'ELLIPSE', 'INSERT', 'TEXT'][type - 6], vertices: [[x, y, 0]] }
    }
    if (i % 50 === 0) e = [null, {}, { type: 'LINE' }, { type: 'LINE', vertices: [[NaN, 0], [1, 1]] }][i / 50]
    entities.push(e)
  }
  const legacy = legacyBuildSnapIndex(entities)
  const queries = Array.from({ length: 100 }, (_, q) => {
    if (q < 70) return [120 * r() - 10, 120 * r() - 10, [0.5, 2, 10][q % 3]]
    const i = q - 70
    return [legacy.xs[i] + (i % 2 ? 0.01 : 0), legacy.ys[i], [0.5, 2, 10][q % 3]]
  })
  return { entities, legacy, queries }
}
const osOracles = Array.from({ length: 20 }, (_, k) => oracleDocument(k))
function osHatch() {
  const entities = []
  for (let j = 0; j < 250; j += 1) {
    const v = -24.9 + 0.2 * j
    entities.push(osLine([v, -500], [v, 500]), osLine([-500, v], [500, v]))
  }
  return entities
}
function expectOsNear(hit, x, y, kind) {
  expect(hit?.kind).toBe(kind)
  expect(Math.abs(hit.x - x)).toBeLessThanOrEqual(1e-9)
  expect(Math.abs(hit.y - y)).toBeLessThanOrEqual(1e-9)
}

describe('bounded object snap modes', () => {
  it('OSQ01 legacy index fields match the base oracle', () => {
    for (const { entities, legacy } of osOracles) {
      const index = buildSnapIndex(entities)
      for (const key of ['n', 'xs', 'ys', 'kinds', 'truncated']) expect(index[key]).toEqual(legacy[key])
    }
  })
  it('OSQ02 four argument query matches all legacy queries', () => {
    for (const { entities, legacy, queries } of osOracles) {
      const index = buildSnapIndex(entities)
      for (const [x, y, tol] of queries) expect(snapPoint(index, x, y, tol)).toEqual(legacySnapPoint(legacy, x, y, tol))
    }
  })
  it('OSQ03 explicit default modes and anchor preserve the query', () => {
    for (const { entities, queries } of osOracles) {
      const index = buildSnapIndex(entities)
      for (const [x, y, tol] of queries) expect(snapPoint(index, x, y, tol, { modes: DEFAULT_SNAP_MODES, anchor: [1, 2] })).toEqual(snapPoint(index, x, y, tol))
    }
  })
  it('OSQ04 huge and overflowed arc sweeps terminate', () => {
    // Both angles of each arc are integer-valued doubles, so BigInt gives their exact residues
    // independently of the product's float arithmetic. 1e19 mod 360 is 280 (by hand: 0 mod 40,
    // 1 mod 9), so the first arc runs 280 -> 90 (sweep 170, midpoint at 5 degrees).
    const pt = (deg) => [5 * Math.cos((deg * Math.PI) / 180), 5 * Math.sin((deg * Math.PI) / 180)]
    const residue = (deg) => Number(BigInt(deg) % 360n)
    expect(residue(1e19)).toBe(280)
    for (const [startDeg, endDeg] of [[1e19, 90], [-1.7e308, 1.7e308]]) {
      const index = buildSnapIndex([{ type: 'ARC', vertices: [[0, 0]], radius: 5, startDeg, endDeg }])
      expect(index.n).toBe(4)
      expect(index.xs[0]).toBe(0)
      expect(index.ys[0]).toBe(0)
      for (const v of [...index.xs, ...index.ys]) expect(Number.isFinite(v)).toBe(true)
      expect([...index.kinds].slice(1, 4)).toEqual([SNAP_KIND.END, SNAP_KIND.END, SNAP_KIND.MID])
      const s = residue(startDeg)
      const e = residue(endDeg)
      let sweep = e - s
      while (sweep <= 0) sweep += 360
      while (sweep > 360) sweep -= 360
      const want = [pt(s), pt(e), pt(s + sweep / 2)]
      for (let i = 0; i < 3; i += 1) {
        expect(Math.abs(index.xs[i + 1] - want[i][0])).toBeLessThan(1e-9)
        expect(Math.abs(index.ys[i + 1] - want[i][1])).toBeLessThan(1e-9)
      }
    }
    // The 1e19 arc's end is its 90-degree point and its midpoint the 5-degree point, not the start.
    const big = buildSnapIndex([{ type: 'ARC', vertices: [[0, 0]], radius: 5, startDeg: 1e19, endDeg: 90 }])
    expect(Math.abs(big.xs[2] - 0)).toBeLessThan(1e-9)
    expect(Math.abs(big.ys[2] - 5)).toBeLessThan(1e-9)
    expect(Math.abs(big.xs[3] - pt(5)[0])).toBeLessThan(1e-9)
    expect(Math.abs(big.ys[3] - pt(5)[1])).toBeLessThan(1e-9)
  })
  it('OSQ05 fixed modes filter candidates', () => {
    const index = buildSnapIndex([osLine(), osCircle([20, 0], 2)])
    expect(snapPoint(index, 5.1, 0.1, 1)).toEqual({ x: 5, y: 0, kind: 'midpoint' })
    expect(snapPoint(index, 5.1, 0.1, 1, { modes: 1 })).toBeNull()
    expect(snapPoint(index, 5.1, 0.1, 1, { modes: 2 })).toEqual({ x: 5, y: 0, kind: 'midpoint' })
    expect(snapPoint(index, 20.1, 0, 1)).toEqual({ x: 20, y: 0, kind: 'centre' })
    expect(snapPoint(index, 20.1, 0, 1, { modes: 16 })).toBeNull()
    expect(snapPoint(index, 20.1, 0, 1, { modes: 0 })).toBeNull()
  })
  it('OSQ06 invalid mode masks', () => {
    const index = buildSnapIndex([osLine()])
    for (const modes of [8, 1024, -1, 1.5, '23', null]) {
      const diagnostics = {}
      expect(snapPoint(index, 0, 0, 1, { modes, diagnostics })).toBeNull()
      expect(diagnostics.invalid).toBe(true)
    }
  })
  it('OSQ07 insertion points and independent budget', () => {
    const index = buildSnapIndex([
      { type: 'INSERT', ip: [3, 4, 0] },
      { type: 'TEXT', vertices: [[7, -2, 0]] },
      { type: 'INSERT', ip: [-1, 6, 0], scale: [2, 3, 1], rotationDeg: 90, editable: false },
      { type: 'MTEXT', vertices: [[50, 50, 0]] },
    ])
    expect(snapPoint(index, 3.1, 4, 1)).toBeNull()
    expect(snapPoint(index, 3.1, 4, 1, { modes: 64 })).toEqual({ x: 3, y: 4, kind: 'insertion' })
    expect(snapPoint(index, 7.1, -2, 1, { modes: 64 })).toEqual({ x: 7, y: -2, kind: 'insertion' })
    expect(snapPoint(index, -1.1, 6, 1, { modes: 64 })).toEqual({ x: -1, y: 6, kind: 'insertion' })
    expect(snapPoint(index, 50, 50.1, 1, { modes: 64 })).toBeNull()
    for (const count of [20000, 20001]) {
      const capped = buildSnapIndex([...Array.from({ length: count }, () => ({ type: 'TEXT', vertices: [[3, 4]] })), osLine()])
      expect(capped.n).toBe(3)
      expect(capped.truncated).toBe(false)
      expect(capped.insertions.n).toBe(20000)
      expect(capped.insertions.truncated).toBe(count > 20000)
    }
  })
  it('OSQ08 intersection query and endpoint priority', () => {
    const entities = [osLine([0, 0], [10, 10]), osLine([0, 10], [10, 0])]
    const index = buildSnapIndex(entities)
    expect(snapPoint(index, 5.3, 4.8, 1, { modes: 32 })).toEqual({ x: 5, y: 5, kind: 'intersection' })
    // Both specified diagonals already offer (5,5) as a legacy midpoint.
    expect(snapPoint(index, 5.3, 4.8, 1)).toEqual({ x: 5, y: 5, kind: 'midpoint' })
    const crossing = buildSnapIndex([osLine([0, 0], [12, 12]), osLine([0, 10], [12, -2])])
    expect(snapPoint(crossing, 5.3, 4.8, 1)).toBeNull()
    expect(snapPoint(crossing, 5.3, 4.8, 1, { modes: 32 })).toEqual({ x: 5, y: 5, kind: 'intersection' })
    expect(snapPoint(buildSnapIndex([...entities, osLine([5, 5], [5, 9])]), 5.2, 5, 1, { modes: 1 | 32 })).toEqual({ x: 5, y: 5, kind: 'endpoint' })
  })
  it('OSQ09 perpendicular requires a finite anchor', () => {
    const index = buildSnapIndex([osLine([2, 0], [2, 10])])
    expect(snapPoint(index, 2.2, 5.1, 1, { modes: 128 })).toBeNull()
    expect(snapPoint(index, 2.2, 5.1, 1, { modes: 128, anchor: [0, 5] })).toEqual({ x: 2, y: 5, kind: 'perpendicular' })
    expect(snapPoint(index, 2.2, 5.1, 1, { modes: 128, anchor: [NaN, 5] })).toBeNull()
  })
  it('OSQ10 tangent query aperture', () => {
    const index = buildSnapIndex([osCircle()])
    const opts = { modes: 256, anchor: [10, 0] }
    expectOsNear(snapPoint(index, 2.6, 4.4, 1, opts), 2.5, 4.330127018922193, 'tangent')
    expectOsNear(snapPoint(index, 2.6, -4.4, 1, opts), 2.5, -4.330127018922193, 'tangent')
    expect(snapPoint(index, 2.6, 4.4, 0.01, opts)).toBeNull()
    expect(snapPoint(index, 2.6, 4.4, 1, { modes: 256, anchor: [3, 0] })).toBeNull()
  })
  it('OSQ11 nearest distance and endpoint tie', () => {
    const index = buildSnapIndex([osLine()])
    expect(snapPoint(index, 0.2, 0.1, 1, { modes: 1 | 512 })).toEqual({ x: 0.2, y: 0, kind: 'nearest' })
    expect(snapPoint(index, 0.2, 0.1, 1, { modes: 1 })).toEqual({ x: 0, y: 0, kind: 'endpoint' })
    expect(snapPoint(index, 0, 0.5, 1, { modes: 1 | 512 })).toEqual({ x: 0, y: 0, kind: 'endpoint' })
  })
  it('OSQ12 diagnostics reset on a miss', () => {
    const diagnostics = { invalid: true, truncated: true, localOverflow: true, admitted: 99, omitted: 9, pairs: 5000 }
    expect(snapPoint(buildSnapIndex([osLine()]), 1000, 1000, 1, { modes: ALL_SNAP_MODES, diagnostics })).toBeNull()
    expect(diagnostics).toEqual({ invalid: false, truncated: false, localOverflow: false, admitted: 0, omitted: 0, pairs: 0 })
  })
  it('OSQ13 hatch local heap and pair bounds', () => {
    const index = buildSnapIndex(osHatch()), diagnostics = {}
    const hit = snapPoint(index, 0, 0, 10, { modes: ALL_SNAP_MODES, anchor: [60, 60], diagnostics })
    expect(diagnostics).toEqual({ invalid: false, truncated: false, localOverflow: true, admitted: 64, omitted: 136, pairs: 2016 })
    expect(hit).not.toBeNull()
    expect(Math.hypot(hit.x, hit.y)).toBeLessThanOrEqual(10)
    for (let q = 0; q < 30; q += 1) {
      const i = q * 33
      snapPoint(index, -20 + 0.4 * (i % 100), -20 + 4 * Math.floor(i / 100), 10, { modes: ALL_SNAP_MODES, anchor: [60, 60], diagnostics })
      expect(diagnostics.admitted).toBeLessThanOrEqual(64)
      expect(diagnostics.pairs).toBeLessThanOrEqual(2016)
    }
  })
  it('OSQ14 primitive and vertex caps', () => {
    const lines = buildSnapIndex(Array.from({ length: 20001 }, (_, i) => osLine([i, 0], [i, 1])))
    expect(lines.prims).toHaveLength(20000)
    expect(lines.primsTruncated).toBe(true)
    const poly = { type: 'LWPOLYLINE', vertices: Array.from({ length: 999 }, (_, i) => [i, 0]) }
    const polys = buildSnapIndex(Array.from({ length: 25 }, () => poly))
    expect(polys.prims).toHaveLength(19960)
    expect(polys.primsTruncated).toBe(true)
    const bad = { type: 'LWPOLYLINE', vertices: [...Array.from({ length: 999 }, (_, i) => [i, 0]), [NaN, 0]] }
    const bads = buildSnapIndex(Array.from({ length: 61 }, () => bad))
    expect(bads.prims).toHaveLength(0)
    expect(bads.primsTruncated).toBe(true)
    const refused = buildSnapIndex([{ type: 'LWPOLYLINE', vertices: Array.from({ length: 1001 }, (_, i) => [i, 0]) }])
    expect(refused.prims).toHaveLength(0)
    expect(refused.primsTruncated).toBe(false)
  })
  it('OSQ15 source cap does not stop the legacy loop', () => {
    const index = buildSnapIndex([...Array.from({ length: 20001 }, () => ({ type: 'HATCH' })), osLine()])
    expect(index.n).toBe(3)
    expect(index.truncated).toBe(false)
    expect(index.prims).toHaveLength(0)
    expect(index.primsTruncated).toBe(true)
    const diagnostics = {}
    expect(snapPoint(index, 5, 0.5, 1, { diagnostics })).toEqual({ x: 5, y: 0, kind: 'midpoint' })
    expect(diagnostics.truncated).toBe(false)
    expect(snapPoint(index, 5, 0.5, 1, { modes: 512, diagnostics })).toBeNull()
    expect(diagnostics.truncated).toBe(true)
  })
  it('OSQ16 ellipse centre without dynamic approximation', () => {
    const index = buildSnapIndex([{ type: 'ELLIPSE', vertices: [[5, 5]] }])
    expect(snapPoint(index, 5.1, 5, 1)).toEqual({ x: 5, y: 5, kind: 'centre' })
    expect(snapPoint(index, 5.1, 5, 1, { modes: 512 })).toBeNull()
  })
})
