import { curveOf, locate, crossings } from './intersect.js'

const DEG = Math.PI / 180
const validPoint = (p) => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1])
const distance = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1])
const circleOf = (p) => p.curve.c ? p.curve : p.curve.segs[0].arc
function tolerance(prim, point) {
  let scale = Math.max(1, ...prim.box.map(Math.abs))
  if (point) scale = Math.max(scale, Math.abs(point[0]), Math.abs(point[1]))
  if (prim.circular) {
    const c = circleOf(prim)
    scale = Math.max(1, Math.abs(c.c[0]), Math.abs(c.c[1]), c.r,
      ...(prim.curve.pts ?? []).flat().map(Math.abs), ...(point ?? []).map(Math.abs))
  }
  return 1e-9 * scale
}
function onPrimitive(prim, point, tol) {
  const hit = locate(prim.curve, point)
  return hit.d <= tol && (prim.curve.kind !== 'ARC' || hit.on === true)
}
export function snapPrimitives(entity, source = 0) {
  if (!['LINE', 'LWPOLYLINE', 'CIRCLE', 'ARC'].includes(entity?.type)) return Object.freeze([])
  if (entity.type === 'ARC') {
    if (!Number.isFinite(entity.startDeg) || !Number.isFinite(entity.endDeg)) return Object.freeze([])
    const norm = (v) => ((v % 360) + 360) % 360
    entity = { ...entity, startDeg: norm(entity.startDeg), endDeg: norm(entity.endDeg) }
  }
  const curve = curveOf(entity)
  if (curve.refusal) return Object.freeze([])
  const make = (c, part) => {
    const round = c.c ? c : c.segs[0].arc
    const box = round
      ? [round.c[0] - round.r, round.c[1] - round.r, round.c[0] + round.r, round.c[1] + round.r]
      : [Math.min(c.pts[0][0], c.pts[1][0]), Math.min(c.pts[0][1], c.pts[1][1]),
        Math.max(c.pts[0][0], c.pts[1][0]), Math.max(c.pts[0][1], c.pts[1][1])]
    return Object.freeze({ source, part, box, circular: !!round, curve: c })
  }
  if (curve.kind !== 'POLY') return Object.freeze([make(curve, 0)])
  return Object.freeze(curve.segs.map((seg) => make({
    kind: seg.arc ? 'POLY' : 'LINE', pts: [seg.a, seg.b], closed: false,
    segs: [{ ...seg, i: 0 }],
  }, seg.i)))
}
export function nearestOnPrimitive(prim, cursor) {
  if (!validPoint(cursor)) return null
  const curve = prim.curve
  const { s } = locate(curve, cursor)
  if (curve.c) {
    const angle = curve.kind === 'ARC' ? curve.start + s : s
    return [curve.c[0] + curve.r * Math.cos(angle * DEG), curve.c[1] + curve.r * Math.sin(angle * DEG)]
  }
  const { a, b, arc } = curve.segs[0]
  if (s === 0) return [a[0], a[1]]
  if (s === 1) return [b[0], b[1]]
  if (arc) {
    const angle = (arc.start + arc.sweep * s) * DEG
    return [arc.c[0] + arc.r * Math.cos(angle), arc.c[1] + arc.r * Math.sin(angle)]
  }
  return [a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s]
}
export function perpendicularCandidates(prim, anchor) {
  if (!validPoint(anchor)) return []
  const tol = tolerance(prim, anchor)
  let points
  if (!prim.circular) {
    const { a, b } = prim.curve.segs[0]
    const dx = b[0] - a[0], dy = b[1] - a[1]
    const t = ((anchor[0] - a[0]) * dx + (anchor[1] - a[1]) * dy) / (dx * dx + dy * dy)
    if (!(t >= 0 && t <= 1)) return []
    points = [[a[0] + t * dx, a[1] + t * dy]]
  } else {
    const { c, r } = circleOf(prim)
    const d = distance(anchor, c)
    if (d <= tol) return []
    const dx = r * (anchor[0] - c[0]) / d, dy = r * (anchor[1] - c[1]) / d
    points = [[c[0] + dx, c[1] + dy], [c[0] - dx, c[1] - dy]]
  }
  return points.filter((p) => distance(p, anchor) > tol && onPrimitive(prim, p, tol))
}
export function tangentCandidates(prim, anchor) {
  if (!prim.circular || !validPoint(anchor)) return []
  const { c, r } = circleOf(prim)
  const d = distance(anchor, c), tol = tolerance(prim, anchor)
  if (!(d > r + tol)) return []
  const ux = (anchor[0] - c[0]) / d, uy = (anchor[1] - c[1]) / d
  const q = r / d, b = r * q, h = r * Math.sqrt((1 - q) * (1 + q))
  return [[c[0] + b * ux - h * uy, c[1] + b * uy + h * ux],
    [c[0] + b * ux + h * uy, c[1] + b * uy - h * ux]]
    .filter((p) => onPrimitive(prim, p, tol))
}
export function intersectionCandidates(a, b) {
  const points = []
  for (const { p } of crossings(a.curve, b.curve, 'none')) {
    const tol = Math.max(tolerance(a, p), tolerance(b, p))
    if (onPrimitive(a, p, tol) && onPrimitive(b, p, tol) && !points.some((q) => distance(p, q) <= tol)) points.push(p)
  }
  return points
}
