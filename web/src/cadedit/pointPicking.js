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

import { nearestEntity } from './intersect.js'
/** The pick sequence per op, or null for ops with nothing to pick. */
export const PICK_SEQUENCES = Object.freeze({
  createLine: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'point', keys: ['x2', 'y2'] }],
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
  trim: [{ kind: 'edge', keys: ['edge', 'ex', 'ey'] }, { kind: 'point', keys: ['x', 'y'] }],
  extend: [{ kind: 'edge', keys: ['edge', 'ex', 'ey'] }, { kind: 'point', keys: ['x', 'y'] }],
  fillet: [{ kind: 'edge', keys: ['edge', 'ex', 'ey'] }, { kind: 'point', keys: ['x', 'y'] }],
  chamfer: [{ kind: 'edge', keys: ['edge', 'ex', 'ey'] }, { kind: 'point', keys: ['x', 'y'] }],
  // W4g-4b: a POINT is one pick; an ELLIPSE picks its centre then the axis
  // endpoint (the ratio is typed); MATCHPROP picks the destination entity.
  createPoint: [{ kind: 'point', keys: ['x', 'y'] }],
  createEllipse: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'point', keys: ['x2', 'y2'] }],
  matchprop: [{ kind: 'edge', keys: ['edge', 'ex', 'ey'] }],
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
 */
export function applyPick(state, x, y, inputs = {}, context = null) {
  const step = currentStep(state)
  if (!step || !finite(x) || !finite(y)) return { state, writes: [] }
  const picked = [...state.picked, [x, y]]
  const next = { ...state, picked, step: state.step + 1 }
  if (step.kind === 'edge') {
    // W4g-6: the click names an ENTITY (the cutting edge, the boundary,
    // the second object): the nearest one within the aperture other than
    // the selection, from `context` { entities, tol, exceptId }. A click
    // that lands on nothing writes nothing and the step waits.
    const hit = context ? nearestEntity(context.entities, x, y, context.tol, context.exceptId) : null
    if (!hit) return { state, writes: [] }
    return { state: next, writes: [[step.keys[0], String(hit.id)], [step.keys[1], round3(x)], [step.keys[2], round3(y)]] }
  }
  if (step.kind === 'point') return { state: next, writes: [[step.keys[0], round3(x)], [step.keys[1], round3(y)]] }
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
export const SNAP_KIND = Object.freeze({ END: 0, MID: 1, CENTRE: 2, QUADRANT: 3 })
const SNAP_KIND_NAME = Object.freeze(['endpoint', 'midpoint', 'centre', 'quadrant'])
const DEG = Math.PI / 180

// A DXF arc sweeps counter-clockwise from start to end; an end below the
// start wraps through 360 (the same rule the viewer intake draws it with).
function arcSweepDeg(startDeg, endDeg) {
  let sweep = endDeg - startDeg
  while (sweep <= 0) sweep += 360
  while (sweep > 360) sweep -= 360
  return sweep
}

export function buildSnapIndex(entities) {
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
        const sweep = arcSweepDeg(e.startDeg, e.endDeg)
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
    const segments = e.type === 'LWPOLYLINE' && e.closed && v.length > 2 ? v.length : v.length - 1
    for (let i = 0; i < segments; i += 1) {
      const a = v[i]
      const b = v[(i + 1) % v.length]
      if (a && b) push((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, SNAP_KIND.MID)
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
export function snapPoint(index, x, y, tol) {
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
  return { x: xs[best], y: ys[best], kind: SNAP_KIND_NAME[kinds[best]] }
}

// W4g-4b: the FIXED ratio the ellipse ghost is drawn at. The ratio input has
// no default (a step still waiting until typed), so the preview and the
// result agree only when the drafter types this value; the ghost shows the
// axis, the typed ratio decides the shape.
export const ELLIPSE_GHOST_RATIO = 0.5

/** The rubber band for the cursor at world (x, y): [[x,y],...] plus closed, or null. */
export function ghostFor(state, x, y) {
  if (!state?.sequence || !finite(x) || !finite(y)) return null
  const { op, picked, base } = state
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
  if (op === 'createLine' || op === 'mirror') {
    if (!last || picked.length >= 2) return null
    return { pts: [last, [x, y]], closed: false }
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
