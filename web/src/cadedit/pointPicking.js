// W4f slice A1: canvas point picking for the command line's prompts, as pure
// rules. A prompted op has a PICK SEQUENCE (what a click on the drawing
// means, in order): a point writes two operand keys, a radius writes r as the
// distance from the picked centre, a delta writes dx/dy as the vector from a
// base point, an append adds "x,y" to the point list. `applyPick` turns one
// world click into operand writes; `ghostFor` is the rubber band from what
// was picked to the cursor. Bounded: coordinates are rounded to three
// decimals (the engine parses strings), non-finite input is refused, and a
// polyline list is capped by the store's own point-list limit downstream.
const round3 = (v) => {
  const r = Math.round(v * 1000) / 1000
  return Object.is(r, -0) ? '0' : String(r)
}
const finite = (v) => typeof v === 'number' && Number.isFinite(v)
const num = (s) => { const n = Number.parseFloat(s); return Number.isFinite(n) ? n : null }

import { nearestEntity, locate } from './intersect.js'
import { snapPrimitives, nearestOnPrimitive, perpendicularCandidates, tangentCandidates, intersectionCandidates } from './snapGeometry.js'
import { DEFAULT_SNAP_MODES, isSnapModeMask, snapModeBit } from './snapModes.js'
import { bulgeArc, dimensionSchematic } from './engineIntake.js'
/** The pick sequence per op, or null for ops with nothing to pick. */
export const PICK_SEQUENCES = Object.freeze({
  group: [{ kind: 'edge', key: 'members', repeat: true }],
  createBlock: [{ kind: 'edge', key: 'members', repeat: true }, { kind: 'point', keys: ['x', 'y'] }],
  createLine: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'point', keys: ['x2', 'y2'] }],
  createMleader: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'point', keys: ['x2', 'y2'] }],
  createCircle: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'radius', key: 'r', from: ['x', 'y'] }],
  createArc: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'radius', key: 'r', from: ['x', 'y'] }],
  createPolyline: [{ kind: 'append', key: 'pts' }],
  move: [{ kind: 'base' }, { kind: 'delta', keys: ['dx', 'dy'] }],
  moveVertex: [{ kind: 'base' }, { kind: 'delta', keys: ['dx', 'dy'] }],
  addVertex: [{ kind: 'base' }, { kind: 'delta', keys: ['dx', 'dy'] }],
  // W4g-4: COPY picks like MOVE (base, then the displacement); MIRROR picks
  // its line's two points; ROTATE and SCALE pick the base point (the angle
  // and the factor are typed); RECTANG picks two corners.
  copy: [{ kind: 'base' }, { kind: 'delta', keys: ['dx', 'dy'] }],
  mirror: [{ kind: 'point', keys: ['x1', 'y1'] }, { kind: 'point', keys: ['x2', 'y2'] }],
  rotate: [{ kind: 'point', keys: ['cx', 'cy'] }],
  scale: [{ kind: 'point', keys: ['cx', 'cy'] }],
  createRectangle: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'point', keys: ['x2', 'y2'] }],
  // W4g-5 OFFSET: one pick, the side the parallel copy goes on.
  offset: [{ kind: 'point', keys: ['x', 'y'] }],
  // W4g-5b: a polar array picks its centre; a rectangular array's operands
  // are counts and distances, so it has nothing on the canvas to pick.
  arrayPolar: [{ kind: 'point', keys: ['cx', 'cy'] }],
  // W4g-5d: TEXT picks its start point.
  createText: [{ kind: 'point', keys: ['x', 'y'] }],
  // W4g-5c: a paste picks where it goes.
  pasteClip: [{ kind: 'point', keys: ['x', 'y'] }],
  // W4g-6: each intersection verb names a second ENTITY first (the click
  // lands on it and the point on it rides along), then a point on the
  // selection that says which part to remove, extend or keep.
  trim: [{ kind: 'edge', keys: ['edge', 'ex', 'ey'] }, { kind: 'point', keys: ['x', 'y'], aperture: true }],
  extend: [{ kind: 'edge', keys: ['edge', 'ex', 'ey'] }, { kind: 'point', keys: ['x', 'y'] }],
  fillet: [{ kind: 'edge', keys: ['edge', 'ex', 'ey'] }, { kind: 'point', keys: ['x', 'y'] }],
  chamfer: [{ kind: 'edge', keys: ['edge', 'ex', 'ey'] }, { kind: 'point', keys: ['x', 'y'] }],
  // W4g-4b: a POINT is one pick; an ELLIPSE picks its centre then the axis
  // endpoint (the ratio is typed); MATCHPROP picks the destination entity.
  createPoint: [{ kind: 'point', keys: ['x', 'y'] }],
  createEllipse: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'point', keys: ['x2', 'y2'] }],
  matchprop: [{ kind: 'edge', keys: ['edge', 'ex', 'ey'] }],
  // W4g-7b-02c: INSERT picks its insertion point; the name, scale and
  // rotation are typed, read by the ghost below off the live inputs.
  createInsert: [{ kind: 'point', keys: ['x', 'y'] }],
  // W4g-7b-04c: DIMLINEAR/DIMALIGNED pick the two definition points, then the
  // dimension line's location; the style (and LINEAR's rotation) are typed.
  dimLinear: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'point', keys: ['x2', 'y2'] }, { kind: 'point', keys: ['dx', 'dy'] }],
  dimAligned: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'point', keys: ['x2', 'y2'] }, { kind: 'point', keys: ['dx', 'dy'] }],
})

/**
 * Fresh pick state for an armed op: which step is next, plus the points
 * picked so far. W4f-3: a chain point `from` ([x, y]) answers the first
 * point step up front (LINE's next segment starts where the last one ended),
 * so the sequence opens at step 1 with that point picked and the rubber band
 * runs from it; a non-finite point, or an op whose first step is not a
 * point, opens normally.
 */
export function startPicking(op, from = null) {
  const sequence = PICK_SEQUENCES[op] || null
  const chained = !!(sequence && sequence[0].kind === 'point' && Array.isArray(from) && finite(from[0]) && finite(from[1]))
  return chained
    ? { op, sequence, step: 1, picked: [[from[0], from[1]]], base: null }
    : { op, sequence, step: 0, picked: [], base: null }
}

/** The step the next click answers, or null when the sequence is done (an append repeats forever). */
export function currentStep(state) {
  if (!state?.sequence) return null
  const { sequence, step } = state
  if (step < sequence.length) return sequence[step]
  const last = sequence[sequence.length - 1]
  return last.kind === 'append' ? last : null
}

/**
 * One click at world (x, y) -> { state, writes: [[key, value], ...] }.
 * Refuses non-finite input (no writes, same state). `inputs` is the operator
 * record (strings), read for the current polyline list on an append.
 * On an aperture step, `context.tol` describes TRIM's removal point click
 * and is written after the point coordinates.
 */
export function applyPick(state, x, y, inputs = {}, context = null) {
  const step = currentStep(state)
  if (!step || !finite(x) || !finite(y)) return { state, writes: [] }
  const picked = [...state.picked, [x, y]]
  const next = { ...state, picked, step: state.step + 1 }
  if (step.kind === 'edge') {
    // W4g-6: the click names an ENTITY (the cutting edge, the boundary,
    // the second object): resolve the nearest one within the aperture, then
    // exclude the selection from `context` { entities, tol, exceptId }. A click
    // that lands on nothing writes nothing and the step waits.
    const members = step.repeat ? String(inputs.members || '').split(/\s+/).filter(Boolean) : []
    const hit = context ? nearestEntity(context.entities, x, y, context.tol) : null
    if (!hit || String(hit.id) === String(context.exceptId)) return { state, writes: [] }
    if (members.includes(String(hit.id))) return state.op === 'createBlock'
      ? { state, writes: [['members', members.filter((id) => id !== String(hit.id)).join(' ')]] }
      : { state, writes: [] }
    if (step.repeat) return { state, writes: [['members', [...members, String(hit.id)].join(' ')]] }
    const writes = [[step.keys[0], String(hit.id)], [step.keys[1], round3(x)], [step.keys[2], round3(y)]]
    return { state: next, writes }
  }
  if (step.kind === 'point') {
    const writes = [[step.keys[0], round3(x)], [step.keys[1], round3(y)]]
    if (step.aperture === true && Number.isFinite(context?.tol) && context.tol > 0) writes.push(['etol', String(context.tol)])
    return { state: next, writes }
  }
  if (step.kind === 'radius') {
    const [cx, cy] = state.picked[state.picked.length - 1] || [num(inputs[step.from[0]]) ?? 0, num(inputs[step.from[1]]) ?? 0]
    const r = Math.hypot(x - cx, y - cy)
    if (r <= 0) return { state, writes: [] }
    return { state: next, writes: [[step.key, round3(r)]] }
  }
  if (step.kind === 'base') return { state: { ...next, base: [x, y] }, writes: [] }
  if (step.kind === 'delta') {
    const base = state.base || [0, 0]
    return { state: next, writes: [[step.keys[0], round3(x - base[0])], [step.keys[1], round3(y - base[1])]] }
  }
  if (step.kind === 'append') {
    const current = String(inputs[step.key] || '').trim()
    // The first click REPLACES the default list (the operator is drawing
    // this polyline, not extending the sample); later clicks append.
    const list = picked.length === 1 ? `${round3(x)},${round3(y)}` : `${current} ${round3(x)},${round3(y)}`.trim()
    return { state: next, writes: [[step.key, list]] }
  }
  return { state, writes: [] }
}

/**
 * W4f-4: ORTHO. The anchor a pick is measured from (the last picked point,
 * or the base of a displacement), or null when the next click has nothing to
 * be orthogonal to (a first point, a polyline's first vertex).
 */
export function orthoAnchor(state) {
  if (!state?.sequence) return null
  const last = state.picked[state.picked.length - 1]
  if (last) return last
  return state.base || null
}

/**
 * The cursor at world (x, y) constrained to the axis of the larger delta from
 * the anchor: [x, anchor.y] or [anchor.x, y]. Without an anchor, or with a
 * non-finite cursor, the point is returned as it is (a pick never turns into
 * a refusal because ORTHO is on). Ties go to the horizontal, as the
 * reference does.
 */
export function orthoPoint(state, x, y) {
  const anchor = orthoAnchor(state)
  if (!anchor || !finite(x) || !finite(y)) return [x, y]
  return Math.abs(x - anchor[0]) >= Math.abs(y - anchor[1]) ? [x, anchor[1]] : [anchor[0], y]
}

/**
 * W4f-5: OSNAP. The snap candidates of the engine document, packed once per
 * document change into typed arrays (no per-frame allocation): every
 * segment endpoint and midpoint of LINE / LWPOLYLINE entities and the
 * centre of CIRCLE / ARC. Bounded by MAX_SNAP_POINTS (the rest of a huge
 * document simply has no snaps: never a refusal, never an unbounded scan).
 * Kinds: 0 endpoint, 1 midpoint, 2 centre, 3 quadrant (a circle's four axis
 * points, W4f-5b; arcs contribute their endpoints and midpoint instead).
 */
export const MAX_SNAP_POINTS = 20000
export const MAX_INSERTION_POINTS = 20000
export const MAX_SNAP_PRIMITIVES = 20000
export const MAX_SNAP_SOURCES = 20000
export const MAX_SNAP_VERTICES = 60000
export const MAX_LOCAL_PRIMITIVES = 64
export const SNAP_KIND = Object.freeze({ END: 0, MID: 1, CENTRE: 2, QUADRANT: 3,
  INTERSECTION: 4, PERPENDICULAR: 5, TANGENT: 6, NEAREST: 7, INSERTION: 8 })
const SNAP_KIND_NAME = Object.freeze(['endpoint', 'midpoint', 'centre', 'quadrant',
  'intersection', 'perpendicular', 'tangent', 'nearest', 'insertion'])
const SNAP_BITS = SNAP_KIND_NAME.map(snapModeBit)
const DEG = Math.PI / 180

// A DXF arc sweeps counter-clockwise from start to end; an end below the
// start wraps through 360 (the same rule the viewer intake draws it with).
function arcSweepDeg(startDeg, endDeg) {
  let sweep = endDeg - startDeg
  // Constant time and exact at any magnitude: past four turns the sweep is the difference of the
  // two exact residues (% is exact for doubles), never of the rounded difference, which loses the
  // smaller angle (90 - 1e19 is -1e19) or overflows. Two finite angles always have a sweep, and the
  // loops then run at most twice.
  if (!(Math.abs(sweep) <= 1440)) sweep = (endDeg % 360) - (startDeg % 360)
  while (sweep <= 0) sweep += 360
  while (sweep > 360) sweep -= 360
  return sweep
}

export function buildSnapIndex(entities) {
  const xs = []
  const ys = []
  const kinds = []
  const insertionXs = [], insertionYs = []
  let insertionsTruncated = false
  const prims = []
  let primsTruncated = false
  let sources = 0, vertices = 0, ordinal = 0
  const push = (x, y, kind) => {
    if (xs.length >= MAX_SNAP_POINTS || !finite(x) || !finite(y)) return
    xs.push(x); ys.push(y); kinds.push(kind)
  }
  for (const e of Array.isArray(entities) ? entities : []) {
    const v = Array.isArray(e?.vertices) ? e.vertices : []
    const source = ordinal++
    const ip = e?.type === 'INSERT' ? e.ip : e?.type === 'TEXT' ? v[0] : null
    if (ip && finite(ip[0]) && finite(ip[1])) {
      if (insertionXs.length < MAX_INSERTION_POINTS) {
        insertionXs.push(ip[0]); insertionYs.push(ip[1])
      } else insertionsTruncated = true
    }
    if (!primsTruncated) {
      const charge = Math.min(v.length, 1001)
      if (sources + 1 > MAX_SNAP_SOURCES || vertices + charge > MAX_SNAP_VERTICES) primsTruncated = true
      else {
        sources += 1; vertices += charge
        const next = snapPrimitives(e, source)
        if (prims.length + next.length > MAX_SNAP_PRIMITIVES) primsTruncated = true
        else for (const prim of next) prims.push(prim)
      }
    }
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
        const sweep = arcSweepDeg(e.startDeg, e.endDeg)
        // Past four turns the start is reduced exactly first, so a huge start cannot absorb the
        // sweep (1e19 + 170 is 1e19) and put the end and the midpoint on the start point.
        const s0 = Math.abs(e.startDeg) <= 1440 ? e.startDeg : e.startDeg % 360
        const at = (deg) => [cx + r * Math.cos(deg * DEG), cy + r * Math.sin(deg * DEG)]
        const a = at(s0)
        const b = at(s0 + sweep)
        const m = at(s0 + sweep / 2)
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
  return Object.freeze({ n: xs.length, xs: Float64Array.from(xs), ys: Float64Array.from(ys), kinds: Uint8Array.from(kinds), truncated: xs.length >= MAX_SNAP_POINTS,
    insertions: { n: insertionXs.length, xs: Float64Array.from(insertionXs), ys: Float64Array.from(insertionYs), truncated: insertionsTruncated },
    prims: Object.freeze(prims), primsTruncated })
}

/**
 * The nearest candidate within `tol` (world units) of (x, y), or null. One
 * linear pass over at most MAX_SNAP_POINTS (about 0.1 ms at the cap), no
 * allocation on a miss; an endpoint beats a midpoint or a centre at equal
 * distance. Non-finite input or tolerance finds nothing.
 */
// Reused, bounded scratch: the heap root is the worst retained primitive.
const localHeap = Array.from({ length: MAX_LOCAL_PRIMITIVES }, () => ({ prim: null, d: 0 }))
const queryCursor = [0, 0]
const candidateBuffer = []
const worseLocal = (a, b) => a.d > b.d || (a.d === b.d &&
  (a.prim.source > b.prim.source || (a.prim.source === b.prim.source && a.prim.part > b.prim.part)))
function siftLocal(size, at) {
  while (at * 2 + 1 < size) {
    let child = at * 2 + 1
    if (child + 1 < size && worseLocal(localHeap[child + 1], localHeap[child])) child += 1
    if (!worseLocal(localHeap[child], localHeap[at])) break
    const swap = localHeap[at]; localHeap[at] = localHeap[child]; localHeap[child] = swap
    at = child
  }
}
export function snapPoint(index, x, y, tol, { modes = DEFAULT_SNAP_MODES, anchor, diagnostics } = {}) {
  const diag = diagnostics !== null && typeof diagnostics === 'object' ? diagnostics : null
  if (diag) Object.assign(diag, { invalid: false, truncated: false, localOverflow: false, admitted: 0, omitted: 0, pairs: 0 })
  if (!isSnapModeMask(modes)) {
    if (diag) diag.invalid = true
    return null
  }
  if (diag && index) diag.truncated = !!((modes & 23 && index.truncated) ||
    (modes & 64 && index.insertions?.truncated) || (modes & 928 && index.primsTruncated))
  if (!index || !finite(x) || !finite(y) || !finite(tol) || tol <= 0) return null
  const { n, xs, ys, kinds } = index
  const tol2 = tol * tol
  let bestD = Infinity
  let bestKind = -1, bestSource = Infinity, bestPart = Infinity, bestX, bestY
  const offer = (cx, cy, kind, source = Infinity, part = Infinity) => {
    const dx = cx - x, dy = cy - y, d = dx * dx + dy * dy
    if (d > tol2) return
    if (d < bestD || (d === bestD && bestKind >= 0 && (kind < bestKind ||
      (kind === bestKind && (source < bestSource || (source === bestSource && part < bestPart)))))) {
      bestD = d; bestKind = kind; bestSource = source; bestPart = part; bestX = cx; bestY = cy
    }
  }
  for (let i = 0; i < n; i += 1) {
    if (modes & SNAP_BITS[kinds[i]]) offer(xs[i], ys[i], kinds[i])
  }
  if (modes & 64) {
    const insertions = index.insertions
    for (let i = 0; i < (insertions?.n ?? 0); i += 1) offer(insertions.xs[i], insertions.ys[i], SNAP_KIND.INSERTION)
  }
  const anchored = Array.isArray(anchor) && finite(anchor[0]) && finite(anchor[1])
  const dynamic = (modes & (32 | 512)) || (anchored && (modes & (128 | 256)))
  let size = 0, considered = 0
  if (dynamic) {
    queryCursor[0] = x; queryCursor[1] = y
    for (const prim of index.prims ?? []) {
      if (!(modes & (32 | 512 | (anchored ? 128 : 0))) && !(anchored && prim.circular && (modes & 256))) continue
      const box = prim.box
      if (x < box[0] - tol || y < box[1] - tol || x > box[2] + tol || y > box[3] + tol) continue
      const d = locate(prim.curve, queryCursor).d
      if (!(d <= tol)) continue
      considered += 1
      if (size < MAX_LOCAL_PRIMITIVES) {
        let at = size++
        localHeap[at].prim = prim; localHeap[at].d = d
        while (at > 0) {
          const parent = (at - 1) >> 1
          if (!worseLocal(localHeap[at], localHeap[parent])) break
          const swap = localHeap[at]; localHeap[at] = localHeap[parent]; localHeap[parent] = swap
          at = parent
        }
      } else {
        const root = localHeap[0]
        if (d < root.d || (d === root.d && (prim.source < root.prim.source ||
          (prim.source === root.prim.source && prim.part < root.prim.part)))) {
          root.prim = prim; root.d = d; siftLocal(size, 0)
        }
      }
    }
    if (diag) { diag.admitted = size; diag.omitted = considered - size; diag.localOverflow = considered > size }
    const offerPoints = (points, kind, prim) => {
      candidateBuffer.length = 0
      for (const p of points) candidateBuffer.push(p)
      for (const p of candidateBuffer) offer(p[0], p[1], kind, prim.source, prim.part)
    }
    for (let i = 0; i < size; i += 1) {
      const prim = localHeap[i].prim
      if (modes & 512) {
        const p = nearestOnPrimitive(prim, queryCursor)
        if (p) offer(p[0], p[1], SNAP_KIND.NEAREST, prim.source, prim.part)
      }
      if (anchored && (modes & 128)) offerPoints(perpendicularCandidates(prim, anchor), SNAP_KIND.PERPENDICULAR, prim)
      if (anchored && prim.circular && (modes & 256)) offerPoints(tangentCandidates(prim, anchor), SNAP_KIND.TANGENT, prim)
      if (modes & 32) for (let j = i + 1; j < size; j += 1) {
        const other = localHeap[j].prim
        const first = prim.source < other.source || (prim.source === other.source && prim.part <= other.part) ? prim : other
        const second = first === prim ? other : prim
        if (diag) diag.pairs += 1
        offerPoints(intersectionCandidates(first, second), SNAP_KIND.INTERSECTION, first)
      }
    }
  }
  for (let i = 0; i < size; i += 1) localHeap[i].prim = null
  candidateBuffer.length = 0
  if (bestKind < 0) return null
  return { x: bestX, y: bestY, kind: SNAP_KIND_NAME[bestKind] }
}

// W4g-4b: the FIXED ratio the ellipse ghost is drawn at. The ratio input has
// no default (a step still waiting until typed), so the preview and the
// result agree only when the drafter types this value; the ghost shows the
// axis, the typed ratio decides the shape.
export const ELLIPSE_GHOST_RATIO = 0.5

// W4g-7b-02c: the definition's bounding box (its children's vertices and, for
// a CIRCLE or ARC child, the extent its radius adds), scaled about the
// base by the typed sx/sy (default 1, then sx), rotated about the base by
// the typed rot (default 0), translated to the cursor. A definition
// degenerate in one axis (a single straight child) draws as its chord
// instead of a zero-width box; degenerate in both draws nothing.
function insertGhost(inputs, blocks, cursorX, cursorY) {
  const name = String(inputs?.name ?? '').trim()
  if (!name) return null
  const catalogue = Array.isArray(blocks) ? blocks : []
  const definition = catalogue.find((b) => String(b?.name ?? '').trim().toLowerCase() === name.toLowerCase())
  if (!definition || definition.complete !== true || definition.baseUnknown === true) return null
  const base = Array.isArray(definition.base) ? definition.base : [0, 0, 0]
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const child of Array.isArray(definition.children) ? definition.children : []) {
    const verts = Array.isArray(child?.vertices) ? child.vertices : []
    const r = (child?.type === 'CIRCLE' || child?.type === 'ARC') && finite(child.radius) && child.radius > 0 ? child.radius : 0
    for (const v of verts) {
      if (!Array.isArray(v) || !finite(v[0]) || !finite(v[1])) continue
      if (v[0] - r < minX) minX = v[0] - r
      if (v[0] + r > maxX) maxX = v[0] + r
      if (v[1] - r < minY) minY = v[1] - r
      if (v[1] + r > maxY) maxY = v[1] + r
    }
  }
  if (!(minX <= maxX) || !(minY <= maxY)) return null
  const sxRaw = num(inputs?.sx)
  const sx = sxRaw !== null && sxRaw !== 0 ? sxRaw : 1
  const syRaw = num(inputs?.sy)
  const sy = syRaw !== null && syRaw !== 0 ? syRaw : sx
  const rotRaw = num(inputs?.rot)
  const rad = (rotRaw !== null ? rotRaw : 0) * (Math.PI / 180)
  const cos = Math.cos(rad)
  const sin = Math.sin(rad)
  const transform = (cx, cy) => {
    const dx = (cx - base[0]) * sx
    const dy = (cy - base[1]) * sy
    return [cursorX + dx * cos - dy * sin, cursorY + dx * sin + dy * cos]
  }
  const corners = [transform(minX, minY), transform(maxX, minY), transform(maxX, maxY), transform(minX, maxY)]
  const wDegenerate = Math.abs(maxX - minX) < 1e-9
  const hDegenerate = Math.abs(maxY - minY) < 1e-9
  if (wDegenerate && hDegenerate) return null
  if (hDegenerate) return { pts: [corners[0], corners[1]], closed: false }
  if (wDegenerate) return { pts: [corners[0], corners[3]], closed: false }
  return { pts: corners, closed: true }
}

/**
 * W4g-7b-04c: the DIMLINEAR/DIMALIGNED ghost once both definition points are
 * picked, one point: the schematic (dimensionSchematic) at the cursor's
 * dimline, LINEAR at the typed rotation (default 0; ALIGNED ignores it),
 * collapsed to the ONE path the rubber band can draw (def1 -> its extension
 * line's far point -> the matching far point off def2 -> def2): the same two
 * extension lines the finished schematic draws, joined through where its
 * dimension line runs, so the preview traces the real geometry, not a
 * fabricated shortcut.
 */
function dimensionGhost(op, def1, def2, x, y, inputs) {
  const rotationDeg = op === 'dimLinear' ? (num(inputs?.rot) ?? 0) : 0
  const pieces = dimensionSchematic({ dimtype: op === 'dimLinear' ? 'LINEAR' : 'ALIGNED', def1, def2, dimline: [x, y], rotationDeg })
  if (pieces.length < 3) return null
  const [ext1, ext2, dimensionLine] = pieces
  return { pts: [ext1.pts[0], ...dimensionLine.pts, ext2.pts[0]], closed: false }
}

/**
 * The rubber band for the cursor at world (x, y): [[x,y],...] plus closed, or
 * null. `inputs` and `blocks` (the document's block catalogue) are read only
 * by `createInsert`; every other op ignores them.
 */
export function ghostFor(state, x, y, inputs = null, blocks = null) {
  if (!state?.sequence || !finite(x) || !finite(y)) return null
  const { op, picked, base } = state
  if (op === 'createInsert') return insertGhost(inputs, blocks, x, y)
  const last = picked[picked.length - 1]
  if (op === 'createCircle' || op === 'createArc') {
    if (!last) return null
    const r = Math.hypot(x - last[0], y - last[1])
    if (r <= 0) return null
    const n = 48
    const pts = new Array(n)
    for (let i = 0; i < n; i += 1) {
      const a = (i / n) * Math.PI * 2
      pts[i] = [last[0] + r * Math.cos(a), last[1] + r * Math.sin(a)]
    }
    return { pts, closed: true }
  }
  if (op === 'createPolyline') {
    if (!picked.length) return null
    return { pts: [...picked, [x, y]], closed: false }
  }
  if (op === 'createLine' || op === 'createMleader' || op === 'mirror') {
    if (!last || picked.length >= 2) return null
    return { pts: [last, [x, y]], closed: false }
  }
  if (op === 'dimLinear' || op === 'dimAligned') {
    if (!last) return null
    if (picked.length === 1) return { pts: [last, [x, y]], closed: false }
    if (picked.length === 2) return dimensionGhost(op, picked[0], picked[1], x, y, inputs)
    return null
  }
  // W4g-4b: the ellipse about the centre with the cursor as the axis
  // endpoint, at a fixed preview ratio (the typed ratio is read at run).
  if (op === 'createEllipse') {
    if (!last || picked.length >= 2) return null
    const ax = x - last[0]
    const ay = y - last[1]
    if (ax === 0 && ay === 0) return null
    const n = 48
    const pts = new Array(n)
    for (let i = 0; i < n; i += 1) {
      const t = (i / n) * Math.PI * 2
      const c = Math.cos(t)
      const sn = Math.sin(t) * ELLIPSE_GHOST_RATIO
      pts[i] = [last[0] + ax * c - ay * sn, last[1] + ay * c + ax * sn]
    }
    return { pts, closed: true }
  }
  // W4g-4: the rectangle from the first corner to the cursor.
  if (op === 'createRectangle') {
    if (!last || picked.length >= 2) return null
    return { pts: [last, [x, last[1]], [x, y], [last[0], y]], closed: true }
  }
  if (base) return { pts: [base, [x, y]], closed: false }
  return null
}

/** True when the sequence still wants a click (an append always does). */
export function wantsPick(state) {
  return currentStep(state) !== null
}
