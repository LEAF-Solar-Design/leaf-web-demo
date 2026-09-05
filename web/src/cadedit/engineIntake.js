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
/** A TEXT's outline box is this many heights wide per character. */
export const TEXT_ADVANCE = 0.6
// W4g-4b: a POINT draws as a small bow-tie marker (honest about being a
// marker, like PDMODE 3's cross). Its half-size is a fraction of the
// drawing's larger extent (PDSIZE's negative form: a share of what is on
// screen at fit), floored at POINT_MARK drawing units for a drawing that is
// nothing but points; a marker in fixed drawing units read as invisible on
// a millimetre drawing and as geometry on a feet drawing (kimi, #1059). An
// ELLIPSE samples ELLIPSE_SEGMENTS points around its full turn.
export const POINT_MARK = 0.5
export const POINT_MARK_FRACTION = 0.005
export const ELLIPSE_SEGMENTS = 64

const finite = (v) => typeof v === 'number' && Number.isFinite(v)
const point = (v) => (Array.isArray(v) && finite(v[0]) && finite(v[1]) ? [v[0], v[1], finite(v[2]) ? v[2] : 0] : null)

const DECIMAL_ID = /^\d{1,20}$/

/**
 * W4g-1b: the worker names an entity by its handle VALUE in decimal
 * ("37986"), while every intake, the console's selection readout and the
 * write contract name the same entity by the DXF handle in hex ("9462").
 * Now that the console's own drawing is the engine document, the viewer
 * intake carries the hex form so a pick on the canvas reads as the drawing's
 * own handle; the engine's decimal id stays the engine's (the prompts and
 * the picker never see this). BigInt: a handle can exceed 2^53.
 */
export function hexHandle(id) {
  const raw = String(id ?? '')
  if (!DECIMAL_ID.test(raw)) return raw
  return BigInt(raw).toString(16).toUpperCase()
}

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

/**
 * W4g-6d: the points BETWEEN a and b along the arc a DXF bulge describes
 * (tan of a quarter of the included angle, positive counter-clockwise): the
 * crate's own rule (explode's arc_from_bulge), radius d (1 + b^2) / 4|b| and
 * the centre d (1 - b^2) / 4b along the chord's left perpendicular from its
 * midpoint. Endpoints excluded (they are the polyline's own vertices); a
 * straight or degenerate segment yields nothing. Bounded by the arc sampler's
 * own step, so a full semicircle is 24 points.
 */
export function bulgePoints(a, b, bulge, z) {
  if (!Number.isFinite(bulge) || Math.abs(bulge) <= 1e-10) return []
  const dx = b[0] - a[0]
  const dy = b[1] - a[1]
  const d = Math.hypot(dx, dy)
  if (d <= 1e-12) return []
  const b2 = bulge * bulge
  const r = (d * (1 + b2)) / (4 * Math.abs(bulge))
  const off = (d * (1 - b2)) / (4 * bulge)
  const cx = (a[0] + b[0]) / 2 + (-dy / d) * off
  const cy = (a[1] + b[1]) / 2 + (dx / d) * off
  const a0 = Math.atan2(a[1] - cy, a[0] - cx)
  // The arc turns through 4 atan(|bulge|), the bulge's sign giving the sense.
  const sweep = 4 * Math.atan(Math.abs(bulge)) * (bulge > 0 ? 1 : -1)
  const n = Math.max(MIN_ARC_POINTS, Math.ceil(Math.abs(sweep) / (ARC_STEP_DEG * Math.PI / 180)) + 1)
  const out = []
  for (let i = 1; i < n - 1; i += 1) {
    const t = a0 + (sweep * i) / (n - 1)
    out.push([cx + r * Math.cos(t), cy + r * Math.sin(t), z])
  }
  return out
}

/**
 * The half-size of a POINT marker for a drawing whose entities span the
 * box `extent` ({ w, h } in drawing units): a fraction of the larger side,
 * never below POINT_MARK. Null or a degenerate box means the floor.
 */
export function pointMarkSize(extent) {
  const w = extent && finite(extent.w) ? extent.w : 0
  const h = extent && finite(extent.h) ? extent.h : 0
  return Math.max(POINT_MARK, POINT_MARK_FRACTION * Math.max(w, h))
}

/** One entity -> one intake polyline, or null when it has nothing drawable. `markSize` is a POINT marker's half-size. */
export function entityToPolyline(entity, markSize = POINT_MARK) {
  if (!entity || typeof entity !== 'object') return null
  const handle = hexHandle(entity.id ?? entity.handle ?? '')
  const layer = typeof entity.layer === 'string' && entity.layer ? entity.layer : '0'
  const verts = Array.isArray(entity.vertices) ? entity.vertices : []
  const type = String(entity.type || '')
  // W4g-5d: a TEXT draws as its outline box until the viewer draws glyphs:
  // the insertion point at the box's bottom-left, the box `height` tall and
  // TEXT_ADVANCE * height wide per character (a conventional average glyph
  // advance), rotated about the insertion point. Honest about being a box,
  // never a fabricated glyph; the pick and the selection land on it.
  if (type === 'TEXT') {
    const c = point(verts[0])
    const h = entity.height
    const chars = typeof entity.text === 'string' ? [...entity.text].length : 0
    if (!c || !finite(h) || h <= 0 || chars < 1) return null
    const rad = (finite(entity.rotationDeg) ? entity.rotationDeg : 0) * (Math.PI / 180)
    const w = TEXT_ADVANCE * h * chars
    const cos = Math.cos(rad)
    const sin = Math.sin(rad)
    const at = (dx, dy) => [c[0] + dx * cos - dy * sin, c[1] + dx * sin + dy * cos, c[2]]
    return { handle, layer, pts: [at(0, 0), at(w, 0), at(w, h), at(0, h)], closed: true }
  }
  // W4g-4b: a POINT is a marker; an ELLIPSE is sampled from its centre, its
  // major axis (relative to the centre) and its minor-to-major ratio.
  if (type === 'POINT') {
    const c = point(verts[0])
    if (!c) return null
    const s = finite(markSize) && markSize > 0 ? markSize : POINT_MARK
    return { handle, layer, pts: [[c[0] - s, c[1] - s, c[2]], [c[0] + s, c[1] + s, c[2]], [c[0], c[1], c[2]], [c[0] - s, c[1] + s, c[2]], [c[0] + s, c[1] - s, c[2]]], closed: false }
  }
  if (type === 'ELLIPSE') {
    const c = point(verts[0])
    const axis = Array.isArray(entity.majorAxis) ? entity.majorAxis : null
    const ratio = entity.ratio
    if (!c || !axis || !finite(axis[0]) || !finite(axis[1]) || (axis[0] === 0 && axis[1] === 0) || !finite(ratio) || ratio <= 0 || ratio > 1) return null
    const pts = new Array(ELLIPSE_SEGMENTS)
    for (let i = 0; i < ELLIPSE_SEGMENTS; i += 1) {
      const t = (i / ELLIPSE_SEGMENTS) * Math.PI * 2
      const cs = Math.cos(t)
      const sn = Math.sin(t) * ratio
      pts[i] = [c[0] + axis[0] * cs - axis[1] * sn, c[1] + axis[1] * cs + axis[0] * sn, c[2]]
    }
    return { handle, layer, pts, closed: true }
  }
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
  const closed = entity.closed === true
  // W4g-6d: a curved segment (bulge on its start vertex) draws as its arc;
  // a list that does not match the points is ignored as straight, since a
  // drawing is better than none and the kernel refuses such a list itself.
  const bulges = Array.isArray(entity.bulges) && entity.bulges.length === verts.length && pts.length === verts.length ? entity.bulges : null
  if (bulges && bulges.some((b) => Number.isFinite(b) && Math.abs(b) > 1e-10)) {
    const out = []
    const last = closed ? pts.length : pts.length - 1
    for (let i = 0; i < pts.length; i += 1) {
      out.push(pts[i])
      if (i < last) out.push(...bulgePoints(pts[i], pts[(i + 1) % pts.length], bulges[i], pts[i][2]))
    }
    return { handle, layer, pts: out, closed }
  }
  return { handle, layer, pts, closed }
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
  const list = Array.isArray(entities) ? entities : []
  // One pass for the drawing's extent (every vertex, plus a circle's or
  // arc's radius), so a POINT marker is sized to what the drawing spans.
  let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity
  for (const entity of list) {
    const verts = Array.isArray(entity?.vertices) ? entity.vertices : []
    const r = (entity?.type === 'CIRCLE' || entity?.type === 'ARC') && finite(entity.radius) && entity.radius > 0 ? entity.radius : 0
    for (const v of verts) {
      const p = point(v)
      if (!p) continue
      if (p[0] - r < minX) minX = p[0] - r
      if (p[0] + r > maxX) maxX = p[0] + r
      if (p[1] - r < minY) minY = p[1] - r
      if (p[1] + r > maxY) maxY = p[1] + r
    }
  }
  const markSize = pointMarkSize(minX <= maxX ? { w: maxX - minX, h: maxY - minY } : null)
  for (const entity of list) {
    const pl = entityToPolyline(entity, markSize)
    if (!pl) continue
    if (points + pl.pts.length > MAX_POINTS) { truncated += 1; continue }
    points += pl.pts.length
    polylines.push(pl)
  }
  return { source: 'engine', documentId: String(documentId || ''), polylines, inserts: [], faces3d: [], points, truncated }
}

/**
 * W4g-3b: a SERVER intake may carry the two additive lists the contract v2
 * writes, `circles` [{handle, layer, c, r}] and `arcs` [{handle, layer, c,
 * r, start_deg, end_deg}]. The viewer draws polylines, so they are sampled
 * here with the same rule the engine document uses (48-gon, 7.5 deg arc
 * steps); a malformed record is skipped, never thrown on. Returns [] for an
 * intake without them, so every existing intake draws exactly as before.
 */
export function intakeRoundPolylines(intake) {
  const out = []
  if (!intake || typeof intake !== 'object') return out
  for (const c of Array.isArray(intake.circles) ? intake.circles : []) {
    const centre = point(c?.c)
    if (!centre || !finite(c.r) || c.r <= 0) continue
    out.push({ handle: String(c.handle ?? ''), layer: typeof c.layer === 'string' && c.layer ? c.layer : '0', pts: circlePoints(centre[0], centre[1], centre[2], c.r), closed: true })
  }
  for (const a of Array.isArray(intake.arcs) ? intake.arcs : []) {
    const centre = point(a?.c)
    if (!centre || !finite(a.r) || a.r <= 0 || !finite(a.start_deg) || !finite(a.end_deg)) continue
    out.push({ handle: String(a.handle ?? ''), layer: typeof a.layer === 'string' && a.layer ? a.layer : '0', pts: arcPoints(centre[0], centre[1], centre[2], a.r, a.start_deg, a.end_deg), closed: false })
  }
  return out
}
