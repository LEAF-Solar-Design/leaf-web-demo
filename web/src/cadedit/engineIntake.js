// W4f slice A0: the engine document as viewer geometry. The browser engine
// edits an IMPORTED DXF and reports its entities as {id, handle, type, layer,
// closed, vertices, radius, startDeg, endDeg} (the worker's projection); the
// viewer draws an INTAKE ({polylines: [{handle, layer, pts, closed}], inserts,
// faces3d}). This is the pure mapper between them, so the canvas can show what
// the engine holds while a DXF is open and the prompts have something to point
// at. Bounded and fail-closed: a malformed entity is skipped, never thrown on;
// a huge document is truncated at MAX_POINTS with the truncation reported.
export const MAX_POINTS = 200_000
export const CIRCLE_SEGMENTS = 48
export const ARC_STEP_DEG = 7.5
export const MIN_ARC_POINTS = 8

const finite = (v) => typeof v === 'number' && Number.isFinite(v)
const point = (v) => (Array.isArray(v) && finite(v[0]) && finite(v[1]) ? [v[0], v[1], finite(v[2]) ? v[2] : 0] : null)

function circlePoints(cx, cy, z, r) {
  const pts = new Array(CIRCLE_SEGMENTS)
  for (let i = 0; i < CIRCLE_SEGMENTS; i += 1) {
    const a = (i / CIRCLE_SEGMENTS) * Math.PI * 2
    pts[i] = [cx + r * Math.cos(a), cy + r * Math.sin(a), z]
  }
  return pts
}

function arcPoints(cx, cy, z, r, startDeg, endDeg) {
  // DXF arcs sweep counter-clockwise from start to end; an end below the
  // start wraps through 360.
  let sweep = endDeg - startDeg
  while (sweep <= 0) sweep += 360
  while (sweep > 360) sweep -= 360
  const n = Math.max(MIN_ARC_POINTS, Math.ceil(sweep / ARC_STEP_DEG) + 1)
  const pts = new Array(n)
  for (let i = 0; i < n; i += 1) {
    const a = ((startDeg + (sweep * i) / (n - 1)) * Math.PI) / 180
    pts[i] = [cx + r * Math.cos(a), cy + r * Math.sin(a), z]
  }
  return pts
}

/** One entity -> one intake polyline, or null when it has nothing drawable. */
export function entityToPolyline(entity) {
  if (!entity || typeof entity !== 'object') return null
  const handle = String(entity.id ?? entity.handle ?? '')
  const layer = typeof entity.layer === 'string' && entity.layer ? entity.layer : '0'
  const verts = Array.isArray(entity.vertices) ? entity.vertices : []
  const type = String(entity.type || '')
  if (type === 'CIRCLE' || type === 'ARC') {
    const c = point(verts[0])
    const r = entity.radius
    if (!c || !finite(r) || r <= 0) return null
    if (type === 'CIRCLE') return { handle, layer, pts: circlePoints(c[0], c[1], c[2], r), closed: true }
    const { startDeg, endDeg } = entity
    if (!finite(startDeg) || !finite(endDeg)) return null
    return { handle, layer, pts: arcPoints(c[0], c[1], c[2], r, startDeg, endDeg), closed: false }
  }
  const pts = []
  for (const v of verts) {
    const p = point(v)
    if (p) pts.push(p)
  }
  if (pts.length < 2) return null
  return { handle, layer, pts, closed: entity.closed === true }
}

/**
 * The engine's entity list -> a viewer intake. `truncated` says how many
 * entities were dropped past MAX_POINTS (an honest number for the status
 * line, never a silent cut).
 */
export function engineIntake(entities, documentId = '') {
  const polylines = []
  let points = 0
  let truncated = 0
  for (const entity of Array.isArray(entities) ? entities : []) {
    const pl = entityToPolyline(entity)
    if (!pl) continue
    if (points + pl.pts.length > MAX_POINTS) { truncated += 1; continue }
    points += pl.pts.length
    polylines.push(pl)
  }
  return { source: 'engine', documentId: String(documentId || ''), polylines, inserts: [], faces3d: [], points, truncated }
}
