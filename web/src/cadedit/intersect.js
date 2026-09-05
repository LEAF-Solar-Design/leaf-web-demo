// W4g-6: the intersection kernel behind TRIM, EXTEND, FILLET and CHAMFER.
// The reference does these on its own document model; ours splits the work
// the way OFFSET did: the geometry is computed here, in the browser, from the
// engine's projection of the two entities, and the engine only replaces an
// entity's OWN geometry (setVertices / setArc) or creates and deletes. One
// verb is one BATCH of those steps, applied by the worker in one turn, so it
// costs one round trip and one undo step however many entities it touches.
//
// Fail-closed and bounded, by contract: every input is checked before any
// arithmetic that could produce NaN; an entity over MAX_INTERSECT_POINTS is
// refused, never scanned; a crossing pass is at most (segments of the target)
// x (segments of the edge), both bounded by that constant; every impossible
// ask (no crossing, parallel lines, a pick ON a crossing, a closed polyline
// with one crossing, an arc extended past a full turn) is a REFUSAL with the
// sentence the drafter sees, never a silently wrong shape. No entity kind
// beyond LINE / LWPOLYLINE / CIRCLE / ARC is touched.
//
// Angles are degrees, counter-clockwise from +x, and an arc sweeps
// counter-clockwise from start to end with an end below the start wrapping
// through 360: the DXF rule pointPicking and the intake mapper already use.

import { bulgeArc } from './engineIntake.js'

export const MAX_INTERSECT_POINTS = 1000
/** The most steps one verb lowers to: FILLET and CHAMFER cut two entities and create one. */
export const MAX_BATCH_STEPS = 4
const EPSILON = 1e-9
const DEG = Math.PI / 180
// The angular tolerance that decides whether a crossing sits AT an arc's own
// endpoint (degrees): tight on purpose, the aperture is a picking notion.
const TINY_DEG = 1e-7
const KINDS = new Set(['LINE', 'LWPOLYLINE', 'CIRCLE', 'ARC'])
// A bulge below this is a straight segment (the crate's own explode threshold).
const BULGE_EPS = 1e-10

const finite = Number.isFinite
const sub = (a, b) => [a[0] - b[0], a[1] - b[1]]
const add = (a, b) => [a[0] + b[0], a[1] + b[1]]
const scale = (a, k) => [a[0] * k, a[1] * k]
const dot = (a, b) => a[0] * b[0] + a[1] * b[1]
const cross = (a, b) => a[0] * b[1] - a[1] * b[0]
const len = (a) => Math.hypot(a[0], a[1])
const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1])
const same = (a, b, eps) => dist(a, b) <= eps
function normDeg(a) {
  let d = a % 360
  if (d < 0) d += 360
  return d >= 360 ? 0 : d
}
/** Counter-clockwise sweep from start to end, in (0, 360]. */
function sweepDeg(startDeg, endDeg) {
  let sweep = endDeg - startDeg
  while (sweep <= 0) sweep += 360
  while (sweep > 360) sweep -= 360
  return sweep
}
const angleOf = (c, p) => normDeg(Math.atan2(p[1] - c[1], p[0] - c[0]) / DEG)
const onCircle = (c, r, deg) => [c[0] + r * Math.cos(deg * DEG), c[1] + r * Math.sin(deg * DEG)]
// Emitted numbers are rounded to a nanometre of drawing unit: the maths above
// leaves 1e-16 noise on exact corners (a 90-degree fillet's tangent point read
// 2.0000000000000004), and the engine writes what it is given.
const clean = (v) => {
  const r = Math.round(v * 1e9) / 1e9
  return Object.is(r, -0) ? 0 : r
}
const point2 = (v) => (Array.isArray(v) && finite(v[0]) && finite(v[1]) ? [v[0], v[1]] : null)

/**
 * The entity as a curve this kernel can reason about, or `{ refusal }`.
 * LINE -> { kind: 'LINE', pts }, LWPOLYLINE -> { kind: 'POLY', pts, closed },
 * CIRCLE -> { kind: 'CIRCLE', c, r }, ARC -> { kind: 'ARC', c, r, start, end, sweep }.
 */
export function curveOf(entity, role = 'entity') {
  const kind = String(entity?.type || '').toUpperCase()
  if (!KINDS.has(kind)) return { refusal: `a ${kind || 'entity'} of this kind cannot be a ${role} yet (lines, polylines, circles and arcs can)` }
  const raw = Array.isArray(entity?.vertices) ? entity.vertices : []
  if (raw.length > MAX_INTERSECT_POINTS) return { refusal: `the ${role} has more than ${MAX_INTERSECT_POINTS} points` }
  const pts = []
  for (const v of raw) {
    const p = point2(v)
    if (!p) return { refusal: `the ${role} has a point that is not a number` }
    pts.push(p)
  }
  if (kind === 'LINE') {
    if (pts.length !== 2) return { refusal: `the ${role} line has no two endpoints` }
    if (same(pts[0], pts[1], EPSILON)) return { refusal: `the ${role} line has zero length` }
    return { kind: 'LINE', pts, closed: false, segs: [{ i: 0, a: pts[0], b: pts[1], arc: null }] }
  }
  if (kind === 'LWPOLYLINE') {
    if (pts.length < 2) return { refusal: `the ${role} polyline has fewer than two points` }
    // W4g-6d: the projection carries one bulge per vertex (the segment that
    // STARTS at it); absent or null means every segment straight. A list of
    // the wrong length or a value that is not a number is refused, never
    // read as straight.
    const rawB = entity.bulges == null ? [] : entity.bulges
    if (!Array.isArray(rawB) || (rawB.length !== 0 && rawB.length !== pts.length)) return { refusal: `the ${role} polyline's bulge list does not match its points` }
    const bulges = new Array(pts.length).fill(0)
    for (let i = 0; i < rawB.length; i += 1) {
      if (!finite(rawB[i])) return { refusal: `the ${role} polyline has a bulge that is not a number` }
      bulges[i] = rawB[i]
    }
    const curved = bulges.some((b) => Math.abs(b) > BULGE_EPS)
    const closed = entity.closed === true
    const segs = []
    const count = closed ? pts.length : pts.length - 1
    for (let i = 0; i < count; i += 1) {
      const a = pts[i]
      const b = pts[(i + 1) % pts.length]
      let arc = null
      if (Math.abs(bulges[i]) > BULGE_EPS) {
        const curvedSeg = bulgeArc(a, b, bulges[i])
        if (!curvedSeg) return { refusal: `the ${role} polyline has a curved segment of zero length` }
        const { cx, cy, r, a0, sweep } = curvedSeg
        if (![cx, cy, r, a0, sweep].every(finite)) return { refusal: `the ${role} polyline has a bulge that overflows` }
        arc = { c: [cx, cy], r, start: normDeg(a0 / DEG), sweep: sweep / DEG }
      }
      segs.push({ i, a, b, arc })
    }
    return { kind: 'POLY', pts, closed, bulges, curved, segs }
  }
  const c = pts[0]
  const r = entity?.radius
  if (!c || !finite(r) || r <= 0) return { refusal: `the ${role} ${kind.toLowerCase()} has no centre or radius` }
  if (kind === 'CIRCLE') return { kind: 'CIRCLE', c, r }
  const start = entity?.startDeg
  const end = entity?.endDeg
  if (!finite(start) || !finite(end)) return { refusal: `the ${role} arc has no angles` }
  return { kind: 'ARC', c, r, start: normDeg(start), end: normDeg(end), sweep: sweepDeg(start, end) }
}

/** Angular travel from the start in a segment's own sweep direction. */
const segOffset = (arc, p) => normDeg((angleOf(arc.c, p) - arc.start) * Math.sign(arc.sweep))
/** Point at param s of a LINE / POLY curve (s = segment index + fraction). */
function pointAt(curve, s) {
  const count = curve.segs.length
  let i = Math.floor(s)
  let t = s - i
  if (curve.closed) i = ((i % count) + count) % count
  else {
    if (i >= count) { i = count - 1; t = 1 }
    if (i < 0) { i = 0; t = s }
  }
  const { a, b, arc } = curve.segs[i]
  if (arc) return onCircle(arc.c, arc.r, arc.start + arc.sweep * t)
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
}
/** Nearest point of a curve to p: { d, s } with s the curve param (LINE/POLY) or angle (CIRCLE) or offset from start (ARC, `on` says it lies within the sweep). */
export function locate(curve, p) {
  if (curve.kind === 'LINE' || curve.kind === 'POLY') {
    let best = null
    for (const { a, b, i, arc } of curve.segs) {
      let t
      let d
      if (arc) {
        const o = segOffset(arc, p)
        const sweep = Math.abs(arc.sweep)
        if (o <= sweep + EPSILON) {
          t = Math.min(1, o / sweep)
          d = Math.abs(dist(p, arc.c) - arc.r)
        } else {
          const ds = dist(p, a)
          const de = dist(p, b)
          t = ds <= de ? 0 : 1
          d = Math.min(ds, de)
        }
      } else {
        const ab = sub(b, a)
        const l2 = dot(ab, ab)
        t = l2 <= EPSILON ? 0 : dot(sub(p, a), ab) / l2
        t = t < 0 ? 0 : t > 1 ? 1 : t
        d = dist(p, add(a, scale(ab, t)))
      }
      if (!best || d < best.d) best = { d, s: i + t }
    }
    return best
  }
  const a = angleOf(curve.c, p)
  const d = Math.abs(dist(p, curve.c) - curve.r)
  if (curve.kind === 'CIRCLE') return { d, s: a, on: true }
  const o = normDeg(a - curve.start)
  if (o <= curve.sweep) return { d, s: o, on: true }
  // Off the sweep: the nearer endpoint, and say so.
  const ds = dist(p, onCircle(curve.c, curve.r, curve.start))
  const de = dist(p, onCircle(curve.c, curve.r, curve.end))
  return ds <= de ? { d: ds, s: 0, on: false } : { d: de, s: curve.sweep, on: false }
}

/**
 * The nearest LINE / LWPOLYLINE / CIRCLE / ARC to (x, y) within `tol` world
 * units, skipping `exceptId` (the selection): `{ id, d }` or null. One linear
 * pass; an entity this kernel cannot read is skipped, never a throw.
 */
export function nearestEntity(entities, x, y, tol, exceptId = null) {
  if (!Array.isArray(entities) || !finite(x) || !finite(y) || !finite(tol) || tol < 0) return null
  const p = [x, y]
  let best = null
  for (const entity of entities) {
    if (!entity || entity.id === exceptId || entity.editable === false) continue
    const curve = curveOf(entity)
    if (curve.refusal) continue
    const hit = locate(curve, p)
    if (hit && hit.d <= tol && (!best || hit.d < best.d)) best = { id: entity.id, d: hit.d }
  }
  return best
}

// ---- crossings ---------------------------------------------------------------

/** Segment a-b against segment c-d: { t, u, p } or null (parallel and collinear both cross nowhere). */
function segSeg(a, b, c, d) {
  const r = sub(b, a)
  const s = sub(d, c)
  const denom = cross(r, s)
  if (Math.abs(denom) <= EPSILON * (1 + len(r) * len(s))) return null
  const ac = sub(c, a)
  const t = cross(ac, s) / denom
  const u = cross(ac, r) / denom
  return { t, u, p: add(a, scale(r, t)) }
}
/** Segment a-b against the circle (c, r): up to two { t, p }. */
function segCircle(a, b, c, r) {
  const d = sub(b, a)
  const f = sub(a, c)
  const A = dot(d, d)
  if (A <= EPSILON) return []
  const B = 2 * dot(f, d)
  const C = dot(f, f) - r * r
  const disc = B * B - 4 * A * C
  if (disc < 0) return []
  const root = Math.sqrt(Math.max(0, disc))
  const t1 = (-B - root) / (2 * A)
  const t2 = (-B + root) / (2 * A)
  const out = [{ t: t1, p: add(a, scale(d, t1)) }]
  if (root > EPSILON) out.push({ t: t2, p: add(a, scale(d, t2)) })
  return out
}
/** Circle (c1, r1) against circle (c2, r2): up to two points. */
function circleCircle(c1, r1, c2, r2) {
  const dd = dist(c1, c2)
  if (dd <= EPSILON || dd > r1 + r2 + EPSILON || dd < Math.abs(r1 - r2) - EPSILON) return []
  const a = (r1 * r1 - r2 * r2 + dd * dd) / (2 * dd)
  const h2 = r1 * r1 - a * a
  const h = Math.sqrt(Math.max(0, h2))
  const u = scale(sub(c2, c1), 1 / dd)
  const m = add(c1, scale(u, a))
  const n = [-u[1], u[0]]
  if (h <= EPSILON) return [m]
  return [add(m, scale(n, h)), sub(m, scale(n, h))]
}
const withinArc = (curve, p) => (curve.kind !== 'ARC' ? true : normDeg(angleOf(curve.c, p) - curve.start) <= curve.sweep + EPSILON)
const withinSeg = (seg, p) => {
  if (!seg.arc) return true
  const o = segOffset(seg.arc, p)
  return o <= Math.abs(seg.arc.sweep) + EPSILON || 360 - o <= EPSILON
}
const segParam = (arc, p) => {
  const o = segOffset(arc, p)
  return (360 - o <= TINY_DEG ? o - 360 : o) / Math.abs(arc.sweep)
}
const inUnit = (u) => u >= -EPSILON && u <= 1 + EPSILON

/**
 * Where `edge` crosses `target`: [{ s, p }] on the target's own param (LINE/
 * POLY: segment index + fraction, which may run past the ends when `extend`
 * names 'start' / 'end' / 'both' and the target is straight; CIRCLE: the
 * angle; ARC: the offset from its start, on the FULL circle so callers can
 * extend). The edge is never extended (the reference's default edge mode).
 * Segment endpoints snap within `tol`; only equal params merge, so distinct
 * visits to a self-crossing point remain distinct crossings.
 */
export function crossings(target, edge, extend = 'none', tol = EPSILON) {
  const out = []
  const eps = Math.max(tol, EPSILON)
  const push = (s, p) => {
    if (target.closed && target.segs && s === target.segs.length) s = 0
    if (!out.some((o) => Math.abs(o.s - s) <= 1e-9)) out.push({ s, p })
  }
  const straight = target.kind === 'LINE' || target.kind === 'POLY'
  const edgeSegs = edge.kind === 'LINE' || edge.kind === 'POLY' ? edge.segs : null
  if (straight) {
    const segs = target.segs
    const last = segs.length - 1
    for (const { a, b, i, arc } of segs) {
      const lowOk = !arc && (extend === 'start' || extend === 'both') && i === 0 && !target.closed
      const highOk = !arc && (extend === 'end' || extend === 'both') && i === last && !target.closed
      const epsT = arc ? TINY_DEG / Math.abs(arc.sweep) : EPSILON
      const accept = (t) => (t >= -epsT || lowOk) && (t <= 1 + epsT || highOk)
      const hitOnTarget = (t, p) => {
        if (!accept(t)) return
        const o = arc ? segOffset(arc, p) : null
        if (same(p, a, eps) || (arc && Math.min(o, 360 - o) <= TINY_DEG)) push(i, a)
        else if (same(p, b, eps) || (arc && Math.abs(o - Math.abs(arc.sweep)) <= TINY_DEG)) push(i + 1, b)
        else push(i + t, p)
      }
      if (edgeSegs) {
        for (const edgeSeg of edgeSegs) {
          const { a: c, b: d, arc: edgeArc } = edgeSeg
          if (arc && edgeArc) {
            for (const p of circleCircle(arc.c, arc.r, edgeArc.c, edgeArc.r)) {
              if (withinSeg(edgeSeg, p)) hitOnTarget(segParam(arc, p), p)
            }
          } else if (arc) {
            for (const hit of segCircle(c, d, arc.c, arc.r)) {
              if (inUnit(hit.t)) hitOnTarget(segParam(arc, hit.p), hit.p)
            }
          } else if (edgeArc) {
            for (const hit of segCircle(a, b, edgeArc.c, edgeArc.r)) {
              if (withinSeg(edgeSeg, hit.p)) hitOnTarget(hit.t, hit.p)
            }
          } else {
            const hit = segSeg(a, b, c, d)
            if (hit && inUnit(hit.u)) hitOnTarget(hit.t, hit.p)
          }
        }
      } else if (arc) {
        for (const p of circleCircle(arc.c, arc.r, edge.c, edge.r)) {
          if (withinArc(edge, p)) hitOnTarget(segParam(arc, p), p)
        }
      } else {
        for (const hit of segCircle(a, b, edge.c, edge.r)) {
          if (withinArc(edge, hit.p)) hitOnTarget(hit.t, hit.p)
        }
      }
    }
    return out.sort((x, y) => x.s - y.s)
  }
  // A round target: every crossing on the full circle, as an angle or an offset.
  const param = (p) => (target.kind === 'CIRCLE' ? angleOf(target.c, p) : normDeg(angleOf(target.c, p) - target.start))
  if (edgeSegs) {
    for (const seg of edgeSegs) {
      if (seg.arc) {
        for (const p of circleCircle(target.c, target.r, seg.arc.c, seg.arc.r)) {
          if (withinSeg(seg, p) && withinArc(target, p)) push(param(p), p)
        }
      } else {
        for (const hit of segCircle(seg.a, seg.b, target.c, target.r)) if (inUnit(hit.t)) push(param(hit.p), hit.p)
      }
    }
  } else {
    for (const p of circleCircle(target.c, target.r, edge.c, edge.r)) if (withinArc(edge, p)) push(param(p), p)
  }
  return out.sort((x, y) => x.s - y.s)
}

// ---- pieces ----------------------------------------------------------------------

/** Vertices and bulges from a to b (a < b), possibly unwrapped across a closed seam. */
function piece(curve, a, b, eps) {
  const pts = []
  const bulges = []
  const n = curve.pts.length
  const bulgePart = (k, u, v) => {
    const seg = curve.segs[curve.closed ? ((k % n) + n) % n : k]
    if (!seg?.arc) return 0
    if (u === 0 && v === 1) return curve.bulges[seg.i]
    return Math.tan((v - u) * seg.arc.sweep * DEG / 4)
  }
  const push = (p, bulge) => {
    const q = [clean(p[0]), clean(p[1])]
    if (pts.length && same(pts[pts.length - 1], p, eps)) {
      pts[pts.length - 1] = q
      bulges[bulges.length - 1] = bulge
    } else {
      pts.push(q)
      bulges.push(bulge)
    }
  }
  const first = Math.floor(a)
  push(pointAt(curve, a), bulgePart(first, a - first, Math.min(1, b - first)))
  for (let k = Math.floor(a) + 1; k < b; k += 1) {
    push(curve.pts[((k % n) + n) % n], bulgePart(k, 0, Math.min(1, b - k)))
  }
  push(pointAt(curve, b), 0)
  return { pts, bulges }
}
const bulgesOrNull = (list) => list?.some((b) => Math.abs(b) > BULGE_EPS) ? list : null
// A polyline step carries one bulge per point when any kept segment curves.
const setVertices = (entityId, pts, closed, bulges = null) => ({ op: 'setVertices', entityId, points: pts, closed, ...(bulges ? { bulges } : {}) })
const setArc = (entityId, c, r, a0, a1) => ({ op: 'setArc', entityId, x: clean(c[0]), y: clean(c[1]), r, a0: clean(normDeg(a0)), a1: clean(normDeg(a1)) })
const createArc = (c, r, a0, a1, layer) => ({ op: 'createArc', inputs: { x: clean(c[0]), y: clean(c[1]), r, a0: clean(normDeg(a0)), a1: clean(normDeg(a1)), layer } })
const createLine = (a, b, layer) => ({ op: 'createLine', inputs: { x: clean(a[0]), y: clean(a[1]), x2: clean(b[0]), y2: clean(b[1]), layer } })
const createPolyline = (pts, closed, layer, bulges = null) => ({ op: 'createPolyline', inputs: { pts: pts.map((p) => `${clean(p[0])},${clean(p[1])}`).join(' '), closed, layer, ...(bulges ? { bulges } : {}) } })
const refuse = (verb, why) => ({ refusal: `${verb} refused: ${why}.` })
const layerOf = (entity) => String(entity?.layer ?? '')

function readPair(verb, x, y, what) {
  if (!finite(x) || !finite(y)) return refuse(verb, `${what} x and y must both be numbers`)
  return null
}
function readCurves(verb, target, edge, edgeRole, curvedOk = { target: false, edge: false }) {
  if (!target || !edge) return refuse(verb, 'select an entity and name a second one')
  if (target.id === edge.id) return refuse(verb, `select a different entity as the ${edgeRole}`)
  const a = curveOf(target, 'selection')
  if (a.refusal) return refuse(verb, a.refusal)
  const b = curveOf(edge, edgeRole)
  if (b.refusal) return refuse(verb, b.refusal)
  // Only verbs whose write-back preserves curved segments opt in.
  if (a.curved && !curvedOk.target) return refuse(verb, 'the selection is a polyline with curved segments; not in this round')
  if (b.curved && !curvedOk.edge) return refuse(verb, `the ${edgeRole} is a polyline with curved segments; not in this round`)
  return { a, b }
}

// ---- TRIM --------------------------------------------------------------------------

/**
 * TRIM: cut the selection (`target`) at its crossings with the cutting edge
 * and remove the part the point (px, py) lies on. Returns { steps } in the
 * store's own terms, or { refusal }. `tol` is the aperture in world units.
 */
export function trimEntity(target, edge, px, py, tol = EPSILON) {
  const verb = 'Trim'
  const bad = readPair(verb, px, py, 'the point on the part to remove:')
  if (bad) return bad
  const read = readCurves(verb, target, edge, 'cutting edge', { target: true, edge: true })
  if (read.refusal) return read
  const { a: T, b: E } = read
  const eps = Math.max(tol, EPSILON)
  const pick = [px, py]
  const layer = layerOf(target)
  if (T.kind === 'LINE' || (T.kind === 'POLY' && !T.closed)) {
    const end = T.kind === 'LINE' ? 1 : T.pts.length - 1
    const inside = crossings(T, E, 'none', eps).filter((c) => c.s > EPSILON && c.s < end - EPSILON && !same(c.p, T.pts[0], eps) && !same(c.p, T.pts[T.pts.length - 1], eps))
    if (!inside.length) return refuse(verb, 'the cutting edge does not cross the selection')
    const sp = locate(T, pick).s
    if (inside.some((c) => same(c.p, pointAt(T, sp), eps))) return refuse(verb, 'click on the part to remove, away from the crossing')
    const lo = inside.filter((c) => c.s < sp).pop() || null
    const hi = inside.find((c) => c.s > sp) || null
    const first = lo ? piece(T, 0, lo.s, eps) : null
    const second = hi ? piece(T, hi.s, end, eps) : null
    const keepFirst = first && first.pts.length >= 2
    const keepSecond = second && second.pts.length >= 2
    if (!keepFirst && !keepSecond) return refuse(verb, 'nothing of the selection would remain')
    const steps = []
    if (keepFirst) steps.push(setVertices(target.id, first.pts, false, bulgesOrNull(first.bulges)))
    if (keepSecond) {
      if (keepFirst) steps.push(T.kind === 'LINE' ? createLine(second.pts[0], second.pts[1], layer) : createPolyline(second.pts, false, layer, bulgesOrNull(second.bulges)))
      else steps.push(setVertices(target.id, second.pts, false, bulgesOrNull(second.bulges)))
    }
    return { steps }
  }
  if (T.kind === 'POLY') {
    // Closed: the removed piece runs between the two crossings around the
    // pick; what remains is one OPEN polyline from the far crossing round to
    // the near one.
    const all = crossings(T, E, 'none', eps)
    if (all.length < 2) return refuse(verb, 'a closed polyline needs two crossings with the cutting edge to lose a piece')
    const sp = locate(T, pick).s
    if (all.some((c) => same(c.p, pointAt(T, sp), eps))) return refuse(verb, 'click on the part to remove, away from the crossing')
    const lo = all.filter((c) => c.s < sp).pop() || all[all.length - 1]
    const hi = all.find((c) => c.s > sp) || all[0]
    if (lo === hi) return refuse(verb, 'a closed polyline needs two crossings with the cutting edge to lose a piece')
    const n = T.pts.length
    const kept = piece(T, hi.s, lo.s + (hi.s < lo.s ? 0 : n), eps)
    if (kept.pts.length < 2) return refuse(verb, 'nothing of the selection would remain')
    return { steps: [setVertices(target.id, kept.pts, false, bulgesOrNull(kept.bulges))] }
  }
  const epsA = (eps / T.r) / DEG + EPSILON
  if (T.kind === 'CIRCLE') {
    const all = crossings(T, E, 'none', eps)
    if (all.length < 2) return refuse(verb, 'a circle needs two crossings with the cutting edge to lose a piece')
    const ap = locate(T, pick).s
    if (all.some((c) => Math.abs(normDeg(c.s - ap)) < epsA || Math.abs(normDeg(ap - c.s)) < epsA)) return refuse(verb, 'click on the part to remove, away from the crossing')
    const lo = all.filter((c) => c.s < ap).pop() || all[all.length - 1]
    const hi = all.find((c) => c.s > ap) || all[0]
    if (lo === hi) return refuse(verb, 'a circle needs two crossings with the cutting edge to lose a piece')
    return { steps: [{ op: 'delete', entityId: target.id }, createArc(T.c, T.r, hi.s, lo.s, layer)] }
  }
  // ARC: offsets from the start, inside the sweep only.
  const inside = crossings(T, E, 'none', eps).filter((c) => c.s > epsA && c.s < T.sweep - epsA)
  if (!inside.length) return refuse(verb, 'the cutting edge does not cross the selection')
  const at = locate(T, pick)
  if (!at.on) return refuse(verb, 'click on the arc itself, on the part to remove')
  if (inside.some((c) => Math.abs(c.s - at.s) < epsA)) return refuse(verb, 'click on the part to remove, away from the crossing')
  const lo = inside.filter((c) => c.s < at.s).pop() || null
  const hi = inside.find((c) => c.s > at.s) || null
  const steps = []
  if (lo) steps.push(setArc(target.id, T.c, T.r, T.start, T.start + lo.s))
  if (hi) {
    if (lo) steps.push(createArc(T.c, T.r, T.start + hi.s, T.end, layer))
    else steps.push(setArc(target.id, T.c, T.r, T.start + hi.s, T.end))
  }
  return { steps }
}

// ---- EXTEND ------------------------------------------------------------------------

/**
 * EXTEND: lengthen the selection's end nearer to (px, py) along its own
 * direction until it meets the boundary edge. A circle or a closed polyline
 * has no end; a boundary that does not lie ahead of the end is a refusal.
 */
export function extendEntity(target, edge, px, py, tol = EPSILON) {
  const verb = 'Extend'
  const bad = readPair(verb, px, py, 'the point near the end to extend:')
  if (bad) return bad
  const read = readCurves(verb, target, edge, 'boundary edge', { target: true, edge: true })
  if (read.refusal) return read
  const { a: T, b: E } = read
  const eps = Math.max(tol, EPSILON)
  const pick = [px, py]
  if (T.kind === 'CIRCLE') return refuse(verb, 'a circle has no end to extend')
  if (T.kind === 'POLY' && T.closed) return refuse(verb, 'a closed polyline has no end to extend')
  if (T.kind === 'LINE' || T.kind === 'POLY') {
    const n = T.pts.length
    const atEnd = dist(pick, T.pts[n - 1]) <= dist(pick, T.pts[0])
    if (T.segs[atEnd ? T.segs.length - 1 : 0].arc) return refuse(verb, 'the end segment to extend is curved; not in this round')
    const hits = crossings(T, E, atEnd ? 'end' : 'start', eps)
    const last = n - 1
    let chosen = null
    if (atEnd) {
      for (const c of hits) if (c.s > last + EPSILON && (!chosen || c.s < chosen.s)) chosen = c
    } else {
      for (const c of hits) if (c.s < -EPSILON && (!chosen || c.s > chosen.s)) chosen = c
    }
    if (!chosen) return refuse(verb, 'the boundary edge does not lie ahead of that end')
    const pts = T.pts.map((p) => [p[0], p[1]])
    pts[atEnd ? n - 1 : 0] = [clean(chosen.p[0]), clean(chosen.p[1])]
    return { steps: [setVertices(target.id, pts, false, bulgesOrNull(T.bulges))] }
  }
  // ARC: along its own circle, the sweep grows toward the nearest crossing
  // beyond the chosen end, and never to a full turn.
  const startPt = onCircle(T.c, T.r, T.start)
  const endPt = onCircle(T.c, T.r, T.end)
  const atEnd = dist(pick, endPt) <= dist(pick, startPt)
  const epsA = (eps / T.r) / DEG + EPSILON
  const all = crossings(T, E, 'none', eps)
  let best = null
  for (const c of all) {
    // c.s is the offset from the start, counter-clockwise, on the full
    // circle. Only a crossing in the GAP (past the end, before the start) lies
    // ahead of either end; the arc's own far endpoint counts as the gap's far
    // limit, and reaching it is the full turn refused below. The band is a
    // tight angular tolerance, never the aperture: a crossing ON the arc a
    // degree before its end is behind that end, not in the gap.
    const inGap = c.s >= T.sweep - TINY_DEG || c.s <= TINY_DEG
    if (!inGap) continue
    const ahead = atEnd ? normDeg(c.s - T.sweep) : normDeg(-c.s)
    if (ahead > epsA && (best === null || ahead < best)) best = ahead
  }
  if (best === null) return refuse(verb, 'the boundary edge does not lie ahead of that end')
  if (T.sweep + best >= 360 - epsA) return refuse(verb, 'extending that far would close the arc into a full turn')
  return atEnd
    ? { steps: [setArc(target.id, T.c, T.r, T.start, T.end + best)] }
    : { steps: [setArc(target.id, T.c, T.r, T.start - best, T.end)] }
}

// ---- FILLET / CHAMFER at a polyline's own corner (W4g-6d) ------------------------------------

/**
 * The corner of ONE polyline the two picks name: the segment nearest each
 * pick (by the curve param), which must be two different segments sharing a
 * vertex V (the closing segment counts on a closed polyline), both straight.
 * Returns { P, vIdx, u1, u2, L1, L2, theta } with u1 the unit direction from
 * V back along the first segment, u2 from V along the second, L1 / L2 their
 * lengths, theta the angle between them; or { refusal }.
 */
function polyCorner(verb, target, p1, p2) {
  const P = curveOf(target, 'selection')
  if (P.refusal) return refuse(verb, P.refusal)
  if (P.kind !== 'POLY') return refuse(verb, `select a different entity as the second ${verb === 'Chamfer' ? 'line' : 'object'}`)
  const n = P.pts.length
  const segCount = P.closed ? n : n - 1
  if (segCount < 2) return refuse(verb, 'the polyline has one segment; no corner to make')
  const segOf = (p) => Math.min(segCount - 1, Math.max(0, Math.floor(locate(P, p).s)))
  const i1 = segOf(p1)
  const i2 = segOf(p2)
  if (i1 === i2) return refuse(verb, 'click two different segments of the polyline that meet at the corner')
  // Which segment comes first along the polyline: the shared vertex is the
  // second one's start. On a closed polyline the last segment meets the first.
  let first
  let second
  if ((i1 + 1) % segCount === i2 && (i1 + 1 < segCount || P.closed)) [first, second] = [i1, i2]
  else if ((i2 + 1) % segCount === i1 && (i2 + 1 < segCount || P.closed)) [first, second] = [i2, i1]
  else return refuse(verb, 'the two segments do not meet at a corner; click two segments that share a vertex')
  if (Math.abs(P.bulges[first]) > BULGE_EPS || Math.abs(P.bulges[second]) > BULGE_EPS) return refuse(verb, 'a segment at that corner is curved; not in this round')
  const vIdx = second
  const V = P.pts[vIdx]
  const A = P.pts[first]
  const B = P.pts[(second + 1) % n]
  const d1 = sub(A, V)
  const d2 = sub(B, V)
  const L1 = len(d1)
  const L2 = len(d2)
  if (L1 <= EPSILON || L2 <= EPSILON) return refuse(verb, 'a segment at that corner has zero length')
  const u1 = scale(d1, 1 / L1)
  const u2 = scale(d2, 1 / L2)
  const theta = Math.acos(Math.max(-1, Math.min(1, dot(u1, u2))))
  if (theta <= EPSILON || Math.PI - theta <= EPSILON) return refuse(verb, 'the two segments point the same way; no corner to make')
  return { P, vIdx, V, u1, u2, L1, L2, theta }
}
/** The polyline with V replaced by two points (and the first point's bulge), as ONE setVertices step. */
function cornerStep(target, c, T1, T2, bulge) {
  const { P, vIdx } = c
  const pts = P.pts.map((p) => [p[0], p[1]])
  const bulges = P.bulges.slice()
  pts.splice(vIdx, 1, [clean(T1[0]), clean(T1[1])], [clean(T2[0]), clean(T2[1])])
  // The new first point carries the corner's bulge; the second keeps the
  // bulge V had (straight, checked), so the segment after it is unchanged.
  bulges.splice(vIdx, 1, clean(bulge), bulges[vIdx])
  return setVertices(target.id, pts, P.closed, bulges)
}
/**
 * FILLET at a polyline's own corner: V becomes T1 and T2, each r / tan(theta / 2)
 * from V along its segment, and T1 carries the arc as a bulge: tan of a quarter
 * of the included angle (pi - theta), positive when the polyline turns left
 * at V (the crate's convention, positive counter-clockwise).
 */
function filletPolyCorner(target, r, p1, p2) {
  const verb = 'Fillet'
  if (r === 0) return refuse(verb, 'the corner is already sharp; a fillet on a polyline corner needs a radius greater than 0')
  const c = polyCorner(verb, target, p1, p2)
  if (c.refusal) return c
  const { V, u1, u2, L1, L2, theta } = c
  const along = r / Math.tan(theta / 2)
  if (along >= Math.min(L1, L2) - EPSILON) {
    const most = Math.tan(theta / 2) * Math.min(L1, L2)
    return refuse(verb, `the radius is too large for these two segments (at most ${fmt3(most)} fits)`)
  }
  const T1 = add(V, scale(u1, along))
  const T2 = add(V, scale(u2, along))
  // Travel at T1 is -u1 (into V), then u2 away: a left turn is counter-clockwise.
  const turn = cross(u2, u1)
  const bulge = (turn > 0 ? 1 : -1) * Math.tan((Math.PI - theta) / 4)
  return { steps: [cornerStep(target, c, T1, T2, bulge)] }
}
/** CHAMFER at a polyline's own corner: V becomes P1 (d1 back along the first segment) and P2 (d2 along the second), no arc. */
function chamferPolyCorner(target, d1, d2, p1, p2) {
  const verb = 'Chamfer'
  if (d1 === 0 && d2 === 0) return refuse(verb, 'the corner is already sharp; a chamfer on a polyline corner needs a distance greater than 0')
  const c = polyCorner(verb, target, p1, p2)
  if (c.refusal) return c
  const { V, u1, u2, L1, L2 } = c
  if (d1 >= L1 - EPSILON) return refuse(verb, `the first distance is too large for the first segment (less than ${fmt3(L1)} fits)`)
  if (d2 >= L2 - EPSILON) return refuse(verb, `the second distance is too large for the second segment (less than ${fmt3(L2)} fits)`)
  return { steps: [cornerStep(target, c, add(V, scale(u1, d1)), add(V, scale(u2, d2)), 0)] }
}

// ---- FILLET / CHAMFER ------------------------------------------------------------------

/** The two lines' crossing and each line's kept direction from it, or { refusal }. */
function corner(verb, target, edge, px, py, ex, ey) {
  const read = readCurves(verb, target, edge, 'second object')
  if (read.refusal) return read
  const { a: A, b: B } = read
  if (A.kind !== 'LINE') return refuse(verb, `the selection is a ${target.type}; ${verb.toUpperCase()} between lines takes two lines`)
  if (B.kind !== 'LINE') return refuse(verb, `the second object is a ${edge.type}; ${verb.toUpperCase()} between lines takes two lines`)
  const hit = segSeg(A.pts[0], A.pts[1], B.pts[0], B.pts[1])
  if (!hit) return refuse(verb, 'the two lines are parallel and never meet')
  const X = hit.p
  const side = (L, p) => {
    const d = sub(L.pts[1], L.pts[0])
    const along = dot(sub(p, X), d)
    if (Math.abs(along) <= EPSILON * (1 + len(d) * len(d))) return null
    // The unit direction from X toward the side the point names, and the
    // endpoint on that side (kept, and moved to the tangent point).
    const forward = along > 0
    const u = scale(d, (forward ? 1 : -1) / len(d))
    return { u, keptIndex: forward ? 1 : 0 }
  }
  const sa = side(A, [px, py])
  if (!sa) return refuse(verb, 'click on the first line to one side of the crossing to say which part to keep')
  const sb = side(B, [ex, ey])
  if (!sb) return refuse(verb, 'click on the second line to one side of the crossing to say which part to keep')
  const cosT = Math.max(-1, Math.min(1, dot(sa.u, sb.u)))
  const theta = Math.acos(cosT)
  if (theta <= EPSILON || Math.PI - theta <= EPSILON) return refuse(verb, 'the two kept parts point the same way; no corner to make')
  // How much of each line lies on its kept side of the crossing: the SIGNED
  // projection of the kept endpoint onto the kept direction from X. Two lines
  // need not touch (a short line is extended to the corner), so the endpoint
  // in direction u can sit BEHIND the crossing; an unsigned distance would
  // read that as reach and the "cut" would write a point the line never
  // occupied (kimi, round two). The tangent or cut point must fall inside
  // this length; a pick past a line's own end names a part with no length.
  sa.reach = dot(sub(A.pts[sa.keptIndex], X), sa.u)
  sb.reach = dot(sub(B.pts[sb.keptIndex], X), sb.u)
  if (sa.reach <= EPSILON) return refuse(verb, 'the part of the first line to keep has no length on that side of the crossing')
  if (sb.reach <= EPSILON) return refuse(verb, 'the part of the second line to keep has no length on that side of the crossing')
  return { A, B, X, sa, sb, theta }
}
// The figure a refusal names: cleaned of float noise first (an exact 10 reads
// 9.999999999999998 off tan), then floored to three decimals so it always fits.
const fmt3 = (v) => String(Math.floor(clean(v) * 1000) / 1000)
/** A line's new endpoints: the tangent/cut point replaces the end on the far side of X. */
function cutLine(L, s, P) {
  const pts = L.pts.map((p) => [p[0], p[1]])
  const moved = s.keptIndex === 1 ? 0 : 1
  pts[moved] = [clean(P[0]), clean(P[1])]
  return pts
}

/**
 * FILLET: round the corner between the selection and the second line with an
 * arc of radius `r` tangent to both; r = 0 makes the sharp corner. (px, py)
 * says which part of the first line to keep, (ex, ey) which of the second.
 */
// ---- FILLET on arcs (W4g-6b) ------------------------------------------------------

/**
 * A line's kept part relative to the point P on it (a crossing or a tangent
 * point): the endpoint on the pick's side of P, the unit direction from P
 * toward it, and the SIGNED reach (kimi, #1036 round two). Null when the pick
 * sits on P or the kept part has no length.
 */
function lineKeep(L, P, pick) {
  const d = sub(L.pts[1], L.pts[0])
  const along = dot(sub(pick, P), d)
  if (Math.abs(along) <= EPSILON * (1 + len(d) * len(d))) return null
  const forward = along > 0
  const u = scale(d, (forward ? 1 : -1) / len(d))
  const keptIndex = forward ? 1 : 0
  const reach = dot(sub(L.pts[keptIndex], P), u)
  if (reach <= EPSILON) return null
  return { u, keptIndex, reach }
}
/**
 * An arc's kept part relative to the angle `deg` on its circle (a crossing or
 * a tangent point) and the pick's angle: `{ start, end }` in degrees, the arc
 * kept from its start to `deg` or from `deg` to its end, extended through the
 * gap when `deg` lies beyond the nearer end. Null when the kept sweep would be
 * empty or a full turn.
 */
function arcKeep(A, deg, pick) {
  const off = normDeg(deg - A.start)
  // Offsets past the sweep are the gap: the point is ahead of the end when
  // it is nearer the end than the start, else behind the start.
  const past = off > A.sweep
  const ahead = past ? (off - A.sweep) < (360 - off) : false
  const x = past ? (ahead ? off : off - 360) : off
  const p = normDeg(angleOf(A.c, pick) - A.start)
  const keepStart = p < x
  const sweep = keepStart ? x : A.sweep - x
  if (sweep <= TINY_DEG || sweep >= 360 - TINY_DEG) return null
  return keepStart ? { start: A.start, end: A.start + x } : { start: A.start + x, end: A.end }
}
/** Every centre of a circle of radius r tangent to line L (offset r to either side) and to circle (c, R) (offset to R + r and R - r). */
function lineCircleCentres(L, c, R, r) {
  const d = sub(L.pts[1], L.pts[0])
  const n = scale([-d[1], d[0]], 1 / len(d))
  const out = []
  for (const s of [1, -1]) {
    const a = add(L.pts[0], scale(n, s * r))
    const b = add(L.pts[1], scale(n, s * r))
    for (const rr of [R + r, R - r]) {
      if (rr <= EPSILON) continue
      for (const hit of segCircle(a, b, c, rr)) out.push(hit.p)
    }
  }
  return out
}
/** Every centre of a circle of radius r tangent to circles (c1, R1) and (c2, R2). */
function circleCircleCentres(c1, R1, c2, R2, r) {
  const out = []
  for (const r1 of [R1 + r, R1 - r]) {
    if (r1 <= EPSILON) continue
    for (const r2 of [R2 + r, R2 - r]) {
      if (r2 <= EPSILON) continue
      for (const p of circleCircle(c1, r1, c2, r2)) out.push(p)
    }
  }
  return out
}
const footOnLine = (L, C) => {
  const d = sub(L.pts[1], L.pts[0])
  const t = dot(sub(C, L.pts[0]), d) / dot(d, d)
  return add(L.pts[0], scale(d, t))
}
const onCircleToward = (c, R, C) => add(c, scale(sub(C, c), R / dist(C, c)))
const arcStep = (id, A, keep) => setArc(id, A.c, A.r, keep.start, keep.end)
/** The keep of a whole circle: admissible, nothing cut (W4g-6c). */
const WHOLE = Object.freeze({ whole: true })

/**
 * FILLET where at least one object is an ARC: the fillet circle is tangent
 * to both kept parts, so its centre is an intersection of the two curves'
 * offsets; every such centre is a candidate, admissible when each tangent
 * point leaves a kept part with length on its pick's side, and the one
 * nearest the picks wins. r = 0 is the corner at the crossing nearest the
 * picks. A CIRCLE (W4g-6c) is never cut: the reference leaves it whole and
 * adds the fillet arc, so its side has no kept part to test and no step to
 * emit; two circles at r = 0 have no corner to make. Polylines need bulges
 * the engine does not carry yet: refused naming the reason.
 */
function filletWithArc(verb, target, edge, A, B, r, p1, p2) {
  const kinds = [A.kind, B.kind]
  if (kinds.includes('POLY')) return refuse(verb, 'a polyline corner needs a bulge the engine does not carry yet; not in this round')
  const aLine = A.kind === 'LINE'
  const bLine = B.kind === 'LINE'
  const aWhole = A.kind === 'CIRCLE'
  const bWhole = B.kind === 'CIRCLE'
  if (r === 0 && aWhole && bWhole) return refuse(verb, 'two circles have no corner to make; a fillet between circles needs a radius greater than 0')
  // Candidate contact points: for r = 0 the crossings themselves (extension
  // allowed on both curves), else the tangent circle centres.
  const candidates = []
  if (r === 0) {
    const points = aLine
      ? segCircle(A.pts[0], A.pts[1], B.c, B.r).map((h) => h.p)
      : bLine ? segCircle(B.pts[0], B.pts[1], A.c, A.r).map((h) => h.p) : circleCircle(A.c, A.r, B.c, B.r)
    if (points.length === 1) return refuse(verb, 'the two objects touch without crossing; no corner to make')
    if (!points.length) return refuse(verb, 'the two objects never meet, even extended')
    for (const X of points) candidates.push({ C: null, T1: X, T2: X })
  } else {
    const centres = aLine ? lineCircleCentres(A, B.c, B.r, r) : bLine ? lineCircleCentres(B, A.c, A.r, r) : circleCircleCentres(A.c, A.r, B.c, B.r, r)
    let touching = false
    for (const C of centres) {
      const T1 = aLine ? footOnLine(A, C) : onCircleToward(A.c, A.r, C)
      const T2 = bLine ? footOnLine(B, C) : onCircleToward(B.c, B.r, C)
      // Where the two curves touch without crossing, an offset pair meets at
      // the touch point itself: both tangent points coincide and the fillet
      // would be a zero-sweep arc (kimi, #1051). That candidate is no fillet.
      if (same(T1, T2, 1e-7)) { touching = true; continue }
      candidates.push({ C, T1, T2 })
    }
    if (!candidates.length) {
      return refuse(verb, touching ? 'the two objects touch without crossing; no corner to make' : 'no circle of that radius is tangent to both objects')
    }
  }
  let best = null
  for (const cand of candidates) {
    // A whole circle is always admissible on its side: nothing of it is kept
    // or cut, the picks alone choose among its candidates.
    const keepA = aLine ? lineKeep(A, cand.T1, p1) : aWhole ? WHOLE : arcKeep(A, angleOf(A.c, cand.T1), p1)
    const keepB = bLine ? lineKeep(B, cand.T2, p2) : bWhole ? WHOLE : arcKeep(B, angleOf(B.c, cand.T2), p2)
    if (!keepA || !keepB) continue
    const cost = dist(p1, cand.T1) + dist(p2, cand.T2)
    if (!best || cost < best.cost) best = { ...cand, keepA, keepB, cost }
  }
  if (!best) {
    return refuse(verb, r === 0
      ? 'the picks name parts that cannot meet at a crossing'
      : 'the radius is too large for the room the picks leave, or the picks name parts that cannot meet')
  }
  const steps = []
  if (!aWhole) steps.push(aLine ? setVertices(target.id, cutLine(A, best.keepA, best.T1), false) : arcStep(target.id, A, best.keepA))
  if (!bWhole) steps.push(bLine ? setVertices(edge.id, cutLine(B, best.keepB, best.T2), false) : arcStep(edge.id, B, best.keepB))
  if (r > 0) {
    const a1 = angleOf(best.C, best.T1)
    const a2 = angleOf(best.C, best.T2)
    const [a0, aEnd] = sweepDeg(a1, a2) <= 180 ? [a1, a2] : [a2, a1]
    steps.push(createArc(best.C, r, a0, aEnd, layerOf(target)))
  }
  return { steps }
}

export function filletLines(target, edge, r, px, py, ex, ey) {
  const verb = 'Fillet'
  if (!finite(r) || r < 0) return refuse(verb, 'the radius must be a number that is 0 or more')
  let bad = readPair(verb, px, py, 'the point on the first line:')
  if (bad) return bad
  bad = readPair(verb, ex, ey, 'the point on the second line:')
  if (bad) return bad
  // W4g-6d: both picks on the selection itself name a corner of ONE
  // polyline (the reference's FILLET on a polyline); any other same-entity
  // ask is refused by readCurves below.
  if (target && edge && target.id === edge.id && String(target.type || '').toUpperCase() === 'LWPOLYLINE') return filletPolyCorner(target, r, [px, py], [ex, ey])
  // W4g-6b: an ARC on either side takes the tangent-circle path; two lines
  // keep the corner path below.
  const kinds = readCurves(verb, target, edge, 'second object')
  if (kinds.refusal) return kinds
  if (kinds.a.kind !== 'LINE' || kinds.b.kind !== 'LINE') return filletWithArc(verb, target, edge, kinds.a, kinds.b, r, [px, py], [ex, ey])
  const c = corner(verb, target, edge, px, py, ex, ey)
  if (c.refusal) return c
  const { A, B, X, sa, sb, theta } = c
  if (r === 0) {
    return { steps: [setVertices(target.id, cutLine(A, sa, X), false), setVertices(edge.id, cutLine(B, sb, X), false)] }
  }
  const along = r / Math.tan(theta / 2)
  // The tangent points sit `along` from the crossing on each kept side; past
  // a kept end there is no line to be tangent to. Name the largest radius
  // these two lines can take, so the next try is not a guess.
  if (along >= Math.min(sa.reach, sb.reach) - EPSILON) {
    const most = Math.tan(theta / 2) * Math.min(sa.reach, sb.reach)
    return refuse(verb, `the radius is too large for these two lines (at most ${fmt3(most)} fits)`)
  }
  const T1 = add(X, scale(sa.u, along))
  const T2 = add(X, scale(sb.u, along))
  const bis = add(sa.u, sb.u)
  const bl = len(bis)
  if (bl <= EPSILON) return refuse(verb, 'the two kept parts point the same way; no corner to make')
  const C = add(X, scale(bis, (r / Math.sin(theta / 2)) / bl))
  const a1 = angleOf(C, T1)
  const a2 = angleOf(C, T2)
  // The fillet is the MINOR arc between the tangent points.
  const [a0, aEnd] = sweepDeg(a1, a2) <= 180 ? [a1, a2] : [a2, a1]
  return {
    steps: [
      setVertices(target.id, cutLine(A, sa, T1), false),
      setVertices(edge.id, cutLine(B, sb, T2), false),
      createArc(C, r, a0, aEnd, layerOf(target)),
    ],
  }
}

/**
 * CHAMFER: bevel the corner between the selection and the second line, `d1`
 * back along the first from the crossing and `d2` along the second, joined
 * by a new line. Both 0 makes the sharp corner.
 */
export function chamferLines(target, edge, d1, d2, px, py, ex, ey) {
  const verb = 'Chamfer'
  if (!finite(d1) || d1 < 0 || !finite(d2) || d2 < 0) return refuse(verb, 'both distances must be numbers that are 0 or more')
  let bad = readPair(verb, px, py, 'the point on the first line:')
  if (bad) return bad
  bad = readPair(verb, ex, ey, 'the point on the second line:')
  if (bad) return bad
  if (target && edge && target.id === edge.id && String(target.type || '').toUpperCase() === 'LWPOLYLINE') return chamferPolyCorner(target, d1, d2, [px, py], [ex, ey])
  const c = corner(verb, target, edge, px, py, ex, ey)
  if (c.refusal) return c
  const { A, B, X, sa, sb } = c
  // Each cut point sits its distance from the crossing on the kept side; a
  // distance that reaches or passes the kept end has no line left to cut.
  if (d1 >= sa.reach - EPSILON) return refuse(verb, `the first distance is too large for the first line (less than ${fmt3(sa.reach)} fits)`)
  if (d2 >= sb.reach - EPSILON) return refuse(verb, `the second distance is too large for the second line (less than ${fmt3(sb.reach)} fits)`)
  const P1 = add(X, scale(sa.u, d1))
  const P2 = add(X, scale(sb.u, d2))
  const steps = [setVertices(target.id, cutLine(A, sa, P1), false), setVertices(edge.id, cutLine(B, sb, P2), false)]
  if (dist(P1, P2) > EPSILON) steps.push(createLine(P1, P2, layerOf(target)))
  return { steps }
}
