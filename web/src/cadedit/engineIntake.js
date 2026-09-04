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

/** One entity -> one intake polyline, or null when it has nothing drawable. */
export function entityToPolyline(entity) {
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
