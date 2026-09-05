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

export const MAX_INTERSECT_POINTS = 1000
/** The most steps one verb lowers to: FILLET and CHAMFER cut two entities and create one. */
export const MAX_BATCH_STEPS = 4
const EPSILON = 1e-9
const DEG = Math.PI / 180
// The angular tolerance that decides whether a crossing sits AT an arc's own
// endpoint (degrees): tight on purpose, the aperture is a picking notion.
const TINY_DEG = 1e-7
const KINDS = new Set(['LINE', 'LWPOLYLINE', 'CIRCLE', 'ARC'])

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
    return { kind: 'LINE', pts, closed: false }
  }
  if (kind === 'LWPOLYLINE') {
    if (pts.length < 2) return { refusal: `the ${role} polyline has fewer than two points` }
    return { kind: 'POLY', pts, closed: entity.closed === true }
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

/** The straight segments of a LINE / POLY curve (a closed polyline includes its closing one). */
function segmentsOf(curve) {
  const { pts } = curve
  const out = []
  const n = pts.length
  const count = curve.closed ? n : n - 1
  for (let i = 0; i < count; i += 1) out.push([pts[i], pts[(i + 1) % n], i])
  return out
}
/** Point at param s of a LINE / POLY curve (s = segment index + fraction). */
function pointAt(curve, s) {
  const n = curve.pts.length
  const count = curve.closed ? n : n - 1
  let i = Math.floor(s)
  let t = s - i
  if (i >= count) { i = count - 1; t = 1 }
  if (i < 0) { i = 0; t = s }
  const a = curve.pts[i]
  const b = curve.pts[(i + 1) % n]
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
}
/** Nearest point of a curve to p: { d, s } with s the curve param (LINE/POLY) or angle (CIRCLE) or offset from start (ARC, `on` says it lies within the sweep). */
export function locate(curve, p) {
  if (curve.kind === 'LINE' || curve.kind === 'POLY') {
    let best = null
    for (const [a, b, i] of segmentsOf(curve)) {
      const ab = sub(b, a)
      const l2 = dot(ab, ab)
      let t = l2 <= EPSILON ? 0 : dot(sub(p, a), ab) / l2
      t = t < 0 ? 0 : t > 1 ? 1 : t
      const d = dist(p, add(a, scale(ab, t)))
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
const inUnit = (u) => u >= -EPSILON && u <= 1 + EPSILON

/**
 * Where `edge` crosses `target`: [{ s, p }] on the target's own param (LINE/
 * POLY: segment index + fraction, which may run past the ends when `extend`
 * names 'start' / 'end' / 'both' and the target is straight; CIRCLE: the
 * angle; ARC: the offset from its start, on the FULL circle so callers can
 * extend). The edge is never extended (the reference's default edge mode).
 * Crossings closer than `tol` merge.
 */
export function crossings(target, edge, extend = 'none', tol = EPSILON) {
  const out = []
  const eps = Math.max(tol, EPSILON)
  const push = (s, p) => { if (!out.some((o) => same(o.p, p, eps))) out.push({ s, p }) }
  const straight = target.kind === 'LINE' || target.kind === 'POLY'
  const edgeSegs = edge.kind === 'LINE' || edge.kind === 'POLY' ? segmentsOf(edge) : null
  if (straight) {
    const segs = segmentsOf(target)
    const last = segs.length - 1
    for (const [a, b, i] of segs) {
      const lowOk = (extend === 'start' || extend === 'both') && i === 0 && !target.closed
      const highOk = (extend === 'end' || extend === 'both') && i === last && !target.closed
      const accept = (t) => (t >= -EPSILON || lowOk) && (t <= 1 + EPSILON || highOk)
      if (edgeSegs) {
        for (const [c, d] of edgeSegs) {
          const hit = segSeg(a, b, c, d)
          if (hit && inUnit(hit.u) && accept(hit.t)) push(i + hit.t, hit.p)
        }
      } else {
        for (const hit of segCircle(a, b, edge.c, edge.r)) {
          if (accept(hit.t) && withinArc(edge, hit.p)) push(i + hit.t, hit.p)
        }
      }
    }
    return out.sort((x, y) => x.s - y.s)
  }
  // A round target: every crossing on the full circle, as an angle or an offset.
  const param = (p) => (target.kind === 'CIRCLE' ? angleOf(target.c, p) : normDeg(angleOf(target.c, p) - target.start))
  if (edgeSegs) {
    for (const [c, d] of edgeSegs) {
      for (const hit of segCircle(c, d, target.c, target.r)) if (inUnit(hit.t)) push(param(hit.p), hit.p)
    }
  } else {
    for (const p of circleCircle(target.c, target.r, edge.c, edge.r)) if (withinArc(edge, p)) push(param(p), p)
  }
  return out.sort((x, y) => x.s - y.s)
}

// ---- pieces ----------------------------------------------------------------------

/** The vertices of a LINE / POLY curve from param a to param b (a < b), consecutive duplicates dropped. */
function piece(curve, a, b, eps) {
  const pts = [pointAt(curve, a)]
  const n = curve.pts.length
  for (let k = Math.floor(a) + 1; k < b; k += 1) {
    if (k >= 0 && k < n) pts.push(curve.pts[k])
  }
  pts.push(pointAt(curve, b))
  return dedupe(pts, eps)
}
function dedupe(pts, eps) {
  const out = []
  for (const p of pts) if (!out.length || !same(out[out.length - 1], p, eps)) out.push([clean(p[0]), clean(p[1])])
  return out
}
const setVertices = (entityId, pts, closed) => ({ op: 'setVertices', entityId, points: pts, closed })
const setArc = (entityId, c, r, a0, a1) => ({ op: 'setArc', entityId, x: clean(c[0]), y: clean(c[1]), r, a0: clean(normDeg(a0)), a1: clean(normDeg(a1)) })
const createArc = (c, r, a0, a1, layer) => ({ op: 'createArc', inputs: { x: clean(c[0]), y: clean(c[1]), r, a0: clean(normDeg(a0)), a1: clean(normDeg(a1)), layer } })
const createLine = (a, b, layer) => ({ op: 'createLine', inputs: { x: clean(a[0]), y: clean(a[1]), x2: clean(b[0]), y2: clean(b[1]), layer } })
const createPolyline = (pts, closed, layer) => ({ op: 'createPolyline', inputs: { pts: pts.map((p) => `${clean(p[0])},${clean(p[1])}`).join(' '), closed, layer } })
const refuse = (verb, why) => ({ refusal: `${verb} refused: ${why}.` })
const layerOf = (entity) => String(entity?.layer ?? '')

function readPair(verb, x, y, what) {
  if (!finite(x) || !finite(y)) return refuse(verb, `${what} x and y must both be numbers`)
  return null
}
function readCurves(verb, target, edge, edgeRole) {
  if (!target || !edge) return refuse(verb, 'select an entity and name a second one')
  if (target.id === edge.id) return refuse(verb, `select a different entity as the ${edgeRole}`)
  const a = curveOf(target, 'selection')
  if (a.refusal) return refuse(verb, a.refusal)
  const b = curveOf(edge, edgeRole)
  if (b.refusal) return refuse(verb, b.refusal)
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
  const read = readCurves(verb, target, edge, 'cutting edge')
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
    const keepFirst = first && first.length >= 2
    const keepSecond = second && second.length >= 2
    if (!keepFirst && !keepSecond) return refuse(verb, 'nothing of the selection would remain')
    const steps = []
    if (keepFirst) steps.push(setVertices(target.id, first, false))
    if (keepSecond) {
      if (keepFirst) steps.push(T.kind === 'LINE' ? createLine(second[0], second[1], layer) : createPolyline(second, false, layer))
      else steps.push(setVertices(target.id, second, false))
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
    const pts = [pointAt(T, hi.s)]
    if (hi.s < lo.s) {
      for (let k = Math.floor(hi.s) + 1; k < lo.s; k += 1) pts.push(T.pts[k])
    } else {
      for (let k = Math.floor(hi.s) + 1; k < n; k += 1) pts.push(T.pts[k])
      for (let k = 0; k < lo.s; k += 1) pts.push(T.pts[k])
    }
    pts.push(pointAt(T, lo.s))
    const kept = dedupe(pts, eps)
    if (kept.length < 2) return refuse(verb, 'nothing of the selection would remain')
    return { steps: [setVertices(target.id, kept, false)] }
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
  const read = readCurves(verb, target, edge, 'boundary edge')
  if (read.refusal) return read
  const { a: T, b: E } = read
  const eps = Math.max(tol, EPSILON)
  const pick = [px, py]
  if (T.kind === 'CIRCLE') return refuse(verb, 'a circle has no end to extend')
  if (T.kind === 'POLY' && T.closed) return refuse(verb, 'a closed polyline has no end to extend')
  if (T.kind === 'LINE' || T.kind === 'POLY') {
    const n = T.pts.length
    const atEnd = dist(pick, T.pts[n - 1]) <= dist(pick, T.pts[0])
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
    return { steps: [setVertices(target.id, pts, false)] }
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

// ---- FILLET / CHAMFER ------------------------------------------------------------------

/** The two lines' crossing and each line's kept direction from it, or { refusal }. */
function corner(verb, target, edge, px, py, ex, ey) {
  const read = readCurves(verb, target, edge, 'second object')
  if (read.refusal) return read
  const { a: A, b: B } = read
  if (A.kind !== 'LINE') return refuse(verb, `the selection is a ${target.type}; this round takes two lines`)
  if (B.kind !== 'LINE') return refuse(verb, `the second object is a ${edge.type}; this round takes two lines`)
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
export function filletLines(target, edge, r, px, py, ex, ey) {
  const verb = 'Fillet'
  if (!finite(r) || r < 0) return refuse(verb, 'the radius must be a number that is 0 or more')
  let bad = readPair(verb, px, py, 'the point on the first line:')
  if (bad) return bad
  bad = readPair(verb, ex, ey, 'the point on the second line:')
  if (bad) return bad
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
