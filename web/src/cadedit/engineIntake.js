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
export const BLOCK_CHILD_CAP = 60
export const MAX_ARRAY_CELLS = 1000
// W4g-7b-04c: a LINEAR/ALIGNED dimension's schematic. DIM_EXT_PAST is the
// fixed-units choice from the spec's two options (a fixed span reads the
// same at any drawing scale, unlike a magnitude-order guess); the tick and
// text sizes are the same order as TEXT_ADVANCE's own convention.
export const DIM_EXT_PAST = 2
export const DIM_TICK_HALF = 0.15
export const DIM_TEXT_HEIGHT = 0.5
export const DIM_TEXT_GAP = 0.3

const finite = (v) => typeof v === 'number' && Number.isFinite(v)
const point = (v) => (Array.isArray(v) && finite(v[0]) && finite(v[1]) ? [v[0], v[1], finite(v[2]) ? v[2] : 0] : null)
// The same nanometre-grid clean rule as intersect.js's `clean`: the
// dimension schematic's trig (unit vectors, projections) leaves 1e-16
// noise on numbers a hand-derived row expects exact (1.4999999999999996
// for 1.5), and the engine writes what it is given.
const cleanCoord = (v) => {
  const r = Math.round(v * 1e9) / 1e9
  return Object.is(r, -0) ? 0 : r
}
const cleanPoint = (p) => [cleanCoord(p[0]), cleanCoord(p[1]), cleanCoord(p[2])]

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
 * The arc a DXF bulge describes between a and b: `{ cx, cy, r, a0, sweep }`
 * (radians, the sweep signed by the bulge, counter-clockwise positive), or
 * null for a straight or degenerate segment or a bulge that is not a
 * number. ONE rule for the mapper and the snap index (pointPicking), the
 * crate's own (explode's arc_from_bulge): radius d (1 + b^2) / 4|b|, the
 * centre d (1 - b^2) / 4b along the chord's left perpendicular from the
 * midpoint, the sweep 4 atan(|b|).
 */
export function bulgeArc(a, b, bulge) {
  if (!Number.isFinite(bulge) || Math.abs(bulge) <= 1e-10) return null
  if (!a || !b || !Number.isFinite(a[0]) || !Number.isFinite(a[1]) || !Number.isFinite(b[0]) || !Number.isFinite(b[1])) return null
  const dx = b[0] - a[0]
  const dy = b[1] - a[1]
  const d = Math.hypot(dx, dy)
  if (d <= 1e-12) return null
  const b2 = bulge * bulge
  const r = (d * (1 + b2)) / (4 * Math.abs(bulge))
  const off = (d * (1 - b2)) / (4 * bulge)
  const cx = (a[0] + b[0]) / 2 + (-dy / d) * off
  const cy = (a[1] + b[1]) / 2 + (dx / d) * off
  const a0 = Math.atan2(a[1] - cy, a[0] - cx)
  const sweep = 4 * Math.atan(Math.abs(bulge)) * (bulge > 0 ? 1 : -1)
  return { cx, cy, r, a0, sweep }
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
  const arc = bulgeArc(a, b, bulge)
  if (!arc) return []
  const { cx, cy, r, a0, sweep } = arc
  const n = Math.max(MIN_ARC_POINTS, Math.ceil(Math.abs(sweep) / (ARC_STEP_DEG * Math.PI / 180)) + 1)
  const out = []
  for (let i = 1; i < n - 1; i += 1) {
    const t = a0 + (sweep * i) / (n - 1)
    out.push([cx + r * Math.cos(t), cy + r * Math.sin(t), z])
  }
  return out
}

/**
 * Sample server intake bulges for the viewer's fills, strokes and picks.
 * Returns the same array when no valid record has a nonzero bulge, and keeps
 * every untouched record by identity. Malformed bulge lists remain chords.
 */
export function expandBulgedPolylines(polylines) {
  const isBulged = (pl) => Array.isArray(pl?.pts) && pl.pts.length >= 2
    && Array.isArray(pl.bulges) && pl.bulges.length === pl.pts.length
    && pl.bulges.every(finite) && pl.bulges.some((b) => b !== 0)
  if (!polylines.some(isBulged)) return polylines
  return polylines.map((pl) => {
    if (!isBulged(pl)) return pl
    const pts = []
    const append = (p) => {
      const last = pts[pts.length - 1]
      if (!last || last[0] !== p[0] || last[1] !== p[1] || (last[2] ?? 0) !== (p[2] ?? 0)) pts.push(p)
    }
    const count = pl.pts.length
    for (let i = 0; i < count; i += 1) {
      const a = pl.pts[i]
      append(a)
      if (i + 1 < count || pl.closed === true) {
        const b = pl.pts[(i + 1) % count]
        if (pl.bulges[i] !== 0) {
          for (const p of bulgePoints(a, b, pl.bulges[i], a[2] ?? 0)) append(p)
        }
      }
    }
    return { ...pl, pts }
  })
}

/** A measurement to 3 decimals with trailing zeros trimmed ("3", "4.125", never "3.000"). */
export function formatMeasurement(n) {
  if (!finite(n)) return ''
  return Number(n.toFixed(3)).toString()
}

/**
 * W4g-7b-04c: a LINEAR or ALIGNED DIMENSION's SCHEMATIC, drawn from the
 * projection alone (`def1`, `def2`, `dimline`, `rotationDeg`, `measurement`):
 * two extension lines from each definition point to the dimension line and
 * DIM_EXT_PAST past it, the dimension line itself between the two feet, a
 * 45-degree tick mark at each foot (honest until the viewer draws
 * arrowhead glyphs, the same idiom as the 5d TEXT outline box), and the
 * measurement as an axis-aligned TEXT outline box centred (in X) and
 * DIM_TEXT_GAP above the dimension line's midpoint. LINEAR's dimension
 * line runs along the rotation axis through the dimline point; ALIGNED's
 * runs parallel to def1-def2 through it. `dimline` is used only as A point
 * ON that line (never as an offset itself), so a raw click and the
 * server's canonical projection of it draw the identical line. Returns []
 * for a malformed record (never throws): the caller skips it like any
 * other undrawable entity.
 */
export function dimensionSchematic(entity) {
  const def1 = point(entity?.def1)
  const def2 = point(entity?.def2)
  const dimline = point(entity?.dimline)
  if (!def1 || !def2 || !dimline) return []
  let u
  if (entity.dimtype === 'ALIGNED') {
    const dx = def2[0] - def1[0]
    const dy = def2[1] - def1[1]
    const len = Math.hypot(dx, dy)
    if (len <= 1e-9) return []
    u = [dx / len, dy / len]
  } else {
    const rad = (finite(entity.rotationDeg) ? entity.rotationDeg : 0) * (Math.PI / 180)
    u = [Math.cos(rad), Math.sin(rad)]
    // W4g-7b-04c-3 F3a: a rotation perpendicular to def1-def2 projects both
    // definition points onto the same foot (measurement 0); draw nothing
    // rather than a zero-length dimension line under a "0" box. The store
    // and the crate both refuse creating this; a loaded document can still
    // carry one.
    const projection = (def2[0] - def1[0]) * u[0] + (def2[1] - def1[1]) * u[1]
    if (Math.abs(projection) < 1e-9) return []
  }
  const n = [-u[1], u[0]]
  // The foot where an extension line perpendicular to u meets the
  // dimension-line-through-`dimline`: the projection of `def` onto the
  // u-direction offset from `dimline`, added back to `dimline`.
  const foot = (def) => {
    const t = (def[0] - dimline[0]) * u[0] + (def[1] - dimline[1]) * u[1]
    return [dimline[0] + t * u[0], dimline[1] + t * u[1], dimline[2]]
  }
  const foot1 = foot(def1)
  const foot2 = foot(def2)
  const extDir = (def, ft) => {
    const dx = ft[0] - def[0]
    const dy = ft[1] - def[1]
    const len = Math.hypot(dx, dy)
    return len > 1e-9 ? [dx / len, dy / len] : n
  }
  const dir1 = extDir(def1, foot1)
  const dir2 = extDir(def2, foot2)
  const past = (ft, dir) => [ft[0] + dir[0] * DIM_EXT_PAST, ft[1] + dir[1] * DIM_EXT_PAST, ft[2]]
  const handle = hexHandle(entity.id ?? entity.handle ?? '')
  const layer = typeof entity.layer === 'string' && entity.layer ? entity.layer : '0'
  const tickDir = [(u[0] + n[0]) / Math.SQRT2, (u[1] + n[1]) / Math.SQRT2]
  const tick = (ft) => ({ handle, layer, closed: false, pts: [
    [ft[0] - tickDir[0] * DIM_TICK_HALF, ft[1] - tickDir[1] * DIM_TICK_HALF, ft[2]],
    [ft[0] + tickDir[0] * DIM_TICK_HALF, ft[1] + tickDir[1] * DIM_TICK_HALF, ft[2]],
  ] })
  const mid = [(foot1[0] + foot2[0]) / 2, (foot1[1] + foot2[1]) / 2, foot1[2]]
  const label = formatMeasurement(entity.measurement)
  const chars = Math.max(label.length, 1)
  const w = TEXT_ADVANCE * DIM_TEXT_HEIGHT * chars
  const bl = [mid[0] - w / 2, mid[1] + DIM_TEXT_GAP, mid[2]]
  const textBox = { handle, layer, closed: true, pts: [
    [bl[0], bl[1], bl[2]], [bl[0] + w, bl[1], bl[2]], [bl[0] + w, bl[1] + DIM_TEXT_HEIGHT, bl[2]], [bl[0], bl[1] + DIM_TEXT_HEIGHT, bl[2]],
  ] }
  const tick1 = tick(foot1)
  const tick2 = tick(foot2)
  return [
    { handle, layer, closed: false, pts: [def1, past(foot1, dir1)].map(cleanPoint) },
    { handle, layer, closed: false, pts: [def2, past(foot2, dir2)].map(cleanPoint) },
    { handle, layer, closed: false, pts: [foot1, foot2].map(cleanPoint) },
    { ...tick1, pts: tick1.pts.map(cleanPoint) },
    { ...tick2, pts: tick2.pts.map(cleanPoint) },
    { ...textBox, pts: textBox.pts.map(cleanPoint) },
  ]
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

/** MLEADER outline: leader, dogleg, arrow triangle and text box, all owned by its handle. */
export function mleaderSchematic(entity) {
  const vertices = Array.isArray(entity?.vertices) ? entity.vertices.map(point) : []
  const textLocation = point(entity?.textLocation)
  const height = entity?.height
  const arrow = entity?.arrow
  const dogleg = entity?.dogleg
  const chars = typeof entity?.text === 'string' ? [...entity.text].length : 0
  if (vertices.length < 2 || vertices.some((p) => !p) || !textLocation || !chars
    || !finite(height) || height <= 0 || !finite(arrow) || arrow < 0 || !finite(dogleg) || dogleg < 0) return []
  const tip = vertices[0]
  const next = vertices[1]
  const landing = vertices[vertices.length - 1]
  const length = Math.hypot(next[0] - tip[0], next[1] - tip[1])
  if (length <= 1e-9) return []
  const ux = (next[0] - tip[0]) / length
  const uy = (next[1] - tip[1]) / length
  // The worker's text placement carries the horizontal side when no explicit
  // direction is projected. Created leaders place text to the right.
  const dir = point(entity.dogleg_dir) || [textLocation[0] < landing[0] ? -1 : 1, 0, 0]
  const dirLength = Math.hypot(dir[0], dir[1])
  if (dirLength <= 1e-9) return []
  const end = [landing[0] + dogleg * dir[0] / dirLength, landing[1] + dogleg * dir[1] / dirLength, landing[2]]
  const base = [tip[0] + arrow * ux, tip[1] + arrow * uy, tip[2]]
  const half = arrow / 2
  const width = TEXT_ADVANCE * height * chars
  const [x, y, z] = textLocation
  const handle = hexHandle(entity.id ?? entity.handle ?? '')
  const layer = typeof entity.layer === 'string' && entity.layer ? entity.layer : '0'
  const piece = (pts, closed = false) => ({ handle, layer, pts: pts.map(cleanPoint), closed })
  const pieces = [
    piece(vertices),
    piece([landing, end]),
    piece([tip, [base[0] - uy * half, base[1] + ux * half, tip[2]], [base[0] + uy * half, base[1] - ux * half, tip[2]]], true),
    piece([[x, y, z], [x + width, y, z], [x + width, y + height, z], [x, y + height, z]], true),
  ]
  return pieces.every((pl) => pl.pts.every((p) => p.every(finite))) ? pieces : []
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
  if (!['LINE', 'LWPOLYLINE', 'POLYLINE'].includes(type)) return null
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
export function expandBlockReference(insert, definition) {
  if (definition?.baseUnknown === true) return { polylines: [], complete: false }
  const base = point(definition?.base)
  const ip = point(insert?.ip)
  const scale = insert?.scale
  const rotation = insert?.rotationDeg
  if (!base || !ip || !Array.isArray(scale) || scale.length !== 3 || !scale.every(finite) || !finite(rotation)) {
    return { polylines: [], complete: false }
  }
  const columns = insert.columns ?? 1
  const rows = insert.rows ?? 1
  const columnSpacing = insert.columnSpacing ?? 0
  const rowSpacing = insert.rowSpacing ?? 0
  if (!Number.isSafeInteger(columns) || columns < 1 || !Number.isSafeInteger(rows) || rows < 1
    || !finite(columnSpacing) || !finite(rowSpacing)) return { polylines: [], complete: false }
  const cells = Math.min(columns * rows, MAX_ARRAY_CELLS)
  const children = Array.isArray(definition?.children) ? definition.children : []
  let complete = definition?.complete === true && children.length <= BLOCK_CHILD_CAP && columns * rows <= MAX_ARRAY_CELLS
  const rad = rotation * Math.PI / 180
  const cos = Math.cos(rad)
  const sin = Math.sin(rad)
  const handle = hexHandle(insert.id ?? insert.handle ?? '')
  const polylines = []
  for (const child of children.slice(0, BLOCK_CHILD_CAP)) {
    if (!['LINE', 'LWPOLYLINE', 'POLYLINE', 'CIRCLE', 'ARC', 'TEXT'].includes(child?.type)) { complete = false; continue }
    // Sample in child coordinates first so non-uniform scale and mirrors
    // transform circular arcs and text outlines correctly too.
    const pl = entityToPolyline(child)
    if (!pl) { complete = false; continue }
    for (let cell = 0; cell < cells; cell += 1) {
      const column = cell % columns
      const row = Math.floor(cell / columns)
      const pts = pl.pts.map((p) => {
        const x = (p[0] - base[0]) * scale[0]
        const y = (p[1] - base[1]) * scale[1]
        // The pinned Insert::array_transforms adds spacing to the insertion
        // point, outside both the child's scale and rotation.
        return [ip[0] + column * columnSpacing + x * cos - y * sin, ip[1] + row * rowSpacing + x * sin + y * cos, ip[2] + (p[2] - base[2]) * scale[2]]
      })
      if (pts.some((p) => !p.every(finite))) { complete = false; continue }
      polylines.push({ ...pl, handle, sourceHandle: handle, layer: pl.layer === '0' ? (insert.layer || '0') : pl.layer, pts })
    }
  }
  return { polylines, complete }
}

export function engineIntake(entities, documentId = '', catalogue = entities?.blocks) {
  const polylines = []
  const inserts = []
  let points = 0
  let truncated = 0
  const list = Array.isArray(entities) ? entities : Array.isArray(entities?.entities) ? entities.entities : []
  const blocks = Array.isArray(catalogue) ? catalogue : []
  const definitions = new Map(blocks.map((b) => [String(b.name).toUpperCase(), b]))
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
    if (entity?.type === 'INSERT') {
      const definition = definitions.get(String(entity.name).toUpperCase())
      const expanded = definition ? expandBlockReference(entity, definition) : { polylines: [], complete: false }
      for (const pl of expanded.polylines) {
        if (points + pl.pts.length > MAX_POINTS) { truncated += 1; expanded.complete = false; continue }
        points += pl.pts.length
        polylines.push(pl)
      }
      const pt = point(entity.ip)
      if (pt) inserts.push({ handle: hexHandle(entity.id ?? entity.handle ?? ''), name: entity.name, layer: entity.layer || '0', pt,
        rot: entity.rotationDeg, scale: entity.scale, incomplete: !expanded.complete })
      continue
    }
    // W4g-7b-04c: only LINEAR/ALIGNED draw (a schematic); OTHER dimtypes
    // (RADIUS etc.) are visible-by-handle-only projections and draw nothing,
    // per the case table.
    if (entity?.type === 'MLEADER') {
      for (const pl of mleaderSchematic(entity)) {
        if (points + pl.pts.length > MAX_POINTS) { truncated += 1; continue }
        points += pl.pts.length
        polylines.push(pl)
      }
      continue
    }
    if (entity?.type === 'DIMENSION') {
      if (entity.dimtype === 'LINEAR' || entity.dimtype === 'ALIGNED') {
        for (const pl of dimensionSchematic(entity)) {
          if (points + pl.pts.length > MAX_POINTS) { truncated += 1; continue }
          points += pl.pts.length
          polylines.push(pl)
        }
      }
      continue
    }
    const pl = entityToPolyline(entity, markSize)
    if (!pl) continue
    if (points + pl.pts.length > MAX_POINTS) { truncated += 1; continue }
    points += pl.pts.length
    polylines.push(pl)
  }
  const groups = (entities?.groups || []).map((group) => ({
    handle: hexHandle(group.id), name: group.name, flags: group.unnamed ? 1 : 0,
    selectable: group.selectable, description: group.description,
    members: (group.memberIds || []).map(hexHandle),
  }))
  return { source: 'engine', documentId: String(documentId || ''), polylines, inserts, blocks, groups, faces3d: [], points, truncated }
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
