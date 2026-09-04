// W4g-5 OFFSET: the parallel copy, computed here and drawn by the engine's
// own create ops. The reference's OFFSET takes a distance, an entity and a
// SIDE (a click), and produces a parallel entity that distance away on that
// side. Nothing about it needs a new engine operation: a line's offset is a
// line, a circle's is a circle with r +/- d, an arc's keeps its angles, and a
// polyline's is the miter offset of its segments. So this module is pure
// geometry, the store dispatches its answer through the existing create path,
// and one OFFSET costs exactly one engine round trip.
//
// Fail-closed and bounded, by contract: every input is checked before any
// arithmetic that could produce NaN; a corner too sharp to miter, a circle
// offset inward past its own centre, a click that lands exactly ON the entity
// (no side to read) and an unsupported kind are REFUSALS with the sentence the
// drafter sees, never a silently wrong copy. No allocation per candidate
// beyond the answer itself; one pass over the vertices.

export const MAX_OFFSET_POINTS = 1000
// A miter longer than this many times the offset distance means the corner is
// effectively a reversal: AutoCAD trims such corners, we refuse rather than
// invent geometry the drafter did not draw.
export const MITER_LIMIT = 10
// Below this, two segment directions are parallel (or the point is on the
// entity) in double precision terms.
const EPSILON = 1e-9
/** The kinds a parallel copy is defined for; everything else is refused. */
const OFFSETTABLE = new Set(['LINE', 'LWPOLYLINE', 'POLYLINE', 'CIRCLE', 'ARC'])

const finite = (v) => typeof v === 'number' && Number.isFinite(v)

function point2(v) {
  return Array.isArray(v) && finite(v[0]) && finite(v[1]) ? [v[0], v[1]] : null
}

function vertices2(entity) {
  const raw = Array.isArray(entity?.vertices) ? entity.vertices : []
  const out = []
  for (const v of raw) {
    const p = point2(v)
    if (!p) return null
    out.push(p)
  }
  return out
}

const sub = (a, b) => [a[0] - b[0], a[1] - b[1]]
const add = (a, b) => [a[0] + b[0], a[1] + b[1]]
const scale = (a, k) => [a[0] * k, a[1] * k]
const cross = (a, b) => a[0] * b[1] - a[1] * b[0]
const len = (a) => Math.hypot(a[0], a[1])
/** The LEFT normal of a direction: +1 side is left of travel. */
const leftNormal = (d) => [-d[1], d[0]]

function unit(a) {
  const l = len(a)
  return l > EPSILON ? [a[0] / l, a[1] / l] : null
}

/** Squared distance from p to segment ab, and which side p falls on. */
function segmentSide(a, b, p) {
  const ab = sub(b, a)
  const l2 = ab[0] * ab[0] + ab[1] * ab[1]
  if (l2 <= EPSILON) return null
  let t = ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / l2
  t = t < 0 ? 0 : t > 1 ? 1 : t
  const near = [a[0] + ab[0] * t, a[1] + ab[1] * t]
  const dx = p[0] - near[0]
  const dy = p[1] - near[1]
  return { d2: dx * dx + dy * dy, side: cross(ab, sub(p, a)) }
}

/**
 * Which side of `entity` the point (px, py) falls on: +1 left of travel (or
 * outside, for a circle/arc), -1 right (inside), or 0 when the point lies on
 * the entity and names no side. Exported so a consumer can preview the side
 * before the run.
 */
export function offsetSide(entity, px, py) {
  if (!finite(px) || !finite(py)) return 0
  const kind = String(entity?.type || '')
  const verts = vertices2(entity)
  if (!verts || !verts.length) return 0
  const p = [px, py]
  if (kind === 'CIRCLE' || kind === 'ARC') {
    const r = entity.radius
    if (!finite(r) || r <= 0) return 0
    const d = len(sub(p, verts[0]))
    if (Math.abs(d - r) <= EPSILON) return 0
    return d > r ? 1 : -1
  }
  let best = null
  const closed = kind !== 'LINE' && entity.closed === true
  const last = closed ? verts.length : verts.length - 1
  for (let i = 0; i < last; i += 1) {
    const a = verts[i]
    const b = verts[(i + 1) % verts.length]
    const hit = segmentSide(a, b, p)
    if (hit && (best === null || hit.d2 < best.d2)) best = hit
  }
  if (!best || Math.abs(best.side) <= EPSILON) return 0
  return best.side > 0 ? 1 : -1
}

/**
 * The source's own geometry fault, or null when it can be offset at all:
 * a radius that is not positive, a zero-length line, a polyline with fewer
 * than two vertices or more than the bound. Read BEFORE the side, so a
 * degenerate entity names its fault instead of asking for a click.
 */
function sourceFault(kind, entity, verts) {
  if (kind === 'CIRCLE' || kind === 'ARC') {
    if (!finite(entity.radius) || entity.radius <= 0) return 'Offset refused: this entity has no radius to offset.'
    if (kind === 'ARC' && (!finite(entity.startDeg) || !finite(entity.endDeg))) {
      return 'Offset refused: this arc has no sweep to offset.'
    }
    return null
  }
  if (kind === 'LINE') {
    if (verts.length !== 2) return 'Offset refused: this line has no two endpoints.'
    if (!unit(sub(verts[1], verts[0]))) return 'Offset refused: this line has zero length.'
    return null
  }
  if (verts.length < 2) return 'Offset refused: this polyline has fewer than two vertices.'
  if (verts.length > MAX_OFFSET_POINTS) {
    return `Offset refused: this polyline has ${verts.length} vertices, over the ${MAX_OFFSET_POINTS} an offset can carry.`
  }
  return null
}

/**
 * Where the two offset lines through the corner meet: the miter point, or
 * null when there is none to compute. Two cases share a near-zero cross
 * product and must NOT share an answer: segments running the SAME way are
 * collinear, so their offsets are one line and the corner simply rides along
 * it; segments running OPPOSITE ways are a fold back on the path, whose
 * miter is at infinity. Returning the near point for the fold would emit a
 * spike that looks like geometry the drafter drew, so it returns null and the
 * caller refuses.
 */
function miter(prevDir, nextDir, corner, offsetVector, nextOffsetVector) {
  const p1 = add(corner, offsetVector)
  const p2 = add(corner, nextOffsetVector)
  const denominator = cross(prevDir, nextDir)
  if (Math.abs(denominator) <= EPSILON) {
    const sameWay = prevDir[0] * nextDir[0] + prevDir[1] * nextDir[1] > 0
    return sameWay ? p1 : null
  }
  const t = cross(sub(p2, p1), nextDir) / denominator
  return add(p1, scale(prevDir, t))
}

/**
 * The offset of `entity` by `distance` toward the point (px, py). Resolves
 * `{ op, inputs }` naming the create the engine should run, or `{ refusal }`
 * with the operator-facing sentence. The source's layer rides along, so an
 * offset copy lands beside its source rather than on whatever layer is
 * current.
 */
export function offsetEntity(entity, distance, px, py) {
  // Preserve the distinction between a missing/invalid operand and zero.
  // `Number(null)` is zero, which would turn a malformed distance into the
  // wrong refusal sentence at the store boundary.
  const d = typeof distance === 'number'
    ? distance
    : (typeof distance === 'string' && distance.trim() ? Number(distance) : Number.NaN)
  if (!finite(d)) return { refusal: 'Offset refused: the distance must be a number.' }
  if (d <= 0) return { refusal: 'Offset refused: the distance must be greater than 0.' }
  const kind = String(entity?.type || '')
  const layer = typeof entity?.layer === 'string' && entity.layer ? entity.layer : ''
  const verts = vertices2(entity)
  if (!verts || verts.length < 1) return { refusal: 'Offset refused: this entity has no geometry to offset.' }
  // The kind is refused BEFORE the side is read: "a TEXT cannot be offset" is
  // the useful sentence, and asking for a side on a kind that has none would
  // send the drafter clicking for nothing.
  if (!OFFSETTABLE.has(kind)) {
    return { refusal: `Offset refused: a ${kind || 'entity'} of this kind cannot be offset yet.` }
  }
  // Then the source's own geometry, so a degenerate entity says WHY rather
  // than asking for a side it could never read.
  const sourceRefusal = sourceFault(kind, entity, verts)
  if (sourceRefusal) return { refusal: sourceRefusal }
  const side = offsetSide(entity, px, py)
  if (side === 0) {
    return { refusal: 'Offset refused: click to one side of the entity to say which way to offset.' }
  }

  if (kind === 'CIRCLE' || kind === 'ARC') {
    const r = entity.radius
    const radius = r + side * d
    if (radius <= 0) {
      return { refusal: `Offset refused: offsetting inward by ${d} would leave no radius (the source is ${r}).` }
    }
    const [cx, cy] = verts[0]
    if (kind === 'CIRCLE') return { op: 'createCircle', inputs: { x: cx, y: cy, r: radius, layer } }
    return { op: 'createArc', inputs: { x: cx, y: cy, r: radius, a0: entity.startDeg, a1: entity.endDeg, layer } }
  }

  if (kind === 'LINE') {
    const dir = unit(sub(verts[1], verts[0]))
    const n = scale(leftNormal(dir), side * d)
    const a = add(verts[0], n)
    const b = add(verts[1], n)
    return { op: 'createLine', inputs: { x: a[0], y: a[1], x2: b[0], y2: b[1], layer } }
  }

  const closed = entity.closed === true
  const count = verts.length
  // One direction and one offset vector per segment, computed once.
  const segments = closed ? count : count - 1
  const dirs = new Array(segments)
  const offsets = new Array(segments)
  for (let i = 0; i < segments; i += 1) {
    const dir = unit(sub(verts[(i + 1) % count], verts[i]))
    if (!dir) return { refusal: `Offset refused: segment ${i} of this polyline has zero length.` }
    dirs[i] = dir
    offsets[i] = scale(leftNormal(dir), side * d)
  }
  const out = new Array(count)
  for (let i = 0; i < count; i += 1) {
    // A closed polyline wraps, so every vertex has both an incoming and an
    // outgoing segment; an open one's two ends have only one each.
    const incoming = closed ? (i - 1 + count) % count : i - 1
    const outgoing = i
    const hasIn = incoming >= 0 && incoming < segments
    const hasOut = outgoing < segments
    if (hasIn && hasOut) {
      const corner = miter(dirs[incoming], dirs[outgoing], verts[i], offsets[incoming], offsets[outgoing])
      if (!corner || !finite(corner[0]) || !finite(corner[1])) {
        return { refusal: `Offset refused: corner ${i} of this polyline folds back on itself and cannot be offset.` }
      }
      if (len(sub(corner, verts[i])) > MITER_LIMIT * d) {
        return { refusal: `Offset refused: corner ${i} of this polyline is too sharp to offset by ${d}.` }
      }
      out[i] = corner
    } else if (hasOut) {
      out[i] = add(verts[i], offsets[outgoing])
    } else {
      out[i] = add(verts[i], offsets[incoming])
    }
  }
  // The store's create takes points as a flat "x,y x,y" list, the same
  // grammar the polyline prompt accepts.
  const pts = out.map((p) => `${p[0]},${p[1]}`).join(' ')
  return { op: 'createPolyline', inputs: { pts, closed, layer } }
}
