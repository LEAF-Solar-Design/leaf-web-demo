// W4g-3b (one head): the browser edit as a PLAN. The engine holds the head
// document (W4g-1b opened it); a save posts the DIFF of the engine's entity
// list against the list it loaded, in the frozen mutation contract v2 the
// catalog tools use (server/mutation_plan.py), so the server can commit the
// edit through the same closed data plan (the mock writer, or APS on the
// real DWG) instead of replacing the head with a DXF. A diff, not a journal:
// it is exact for every verb (COPY, EXPLODE, MOVE, undo, redo all fall out
// of "what is there now vs what was there"), needs no fold rules, and costs
// one pass over each list (O(n), a Map per side).
//
// Pure and fail-closed: a malformed entity is not guessed at, a handle that
// changed kind or a plan past MAX_PLAN_OPERATIONS is refused with a sentence,
// and the caller (the store's save) sends NO plan in that case, so the
// server takes the DXF sidecar leg with its own note; nothing is dropped
// silently. W4g-6d closes two gaps in that promise: an EDITABLE entity of a
// kind the contract does not carry (a TEXT the browser made, moved or
// erased) and a polyline whose curved segments (bulges) the contract's
// point lists cannot express are refusals too, never omissions. Handles
// cross as DXF hex (the worker names them in decimal).
import { hexHandle } from './engineIntake.js'

export const MAX_PLAN_OPERATIONS = 5000
// A coordinate difference below this is the same number written twice (the
// engine re-parses the document it wrote after every edit); above it, the
// entity moved. Relative to the magnitude so a 10 km drawing keeps the rule.
export const COORDINATE_EPSILON = 1e-9

const ROUND_KINDS = new Set(['CIRCLE', 'ARC'])
const LINEAR_KINDS = new Set(['LINE', 'LWPOLYLINE', 'POLYLINE'])
// A bulge below this is a straight segment (the kernel's and the crate's threshold).
const BULGE_EPS = 1e-10

const finite = (v) => typeof v === 'number' && Number.isFinite(v)

function point3(v) {
  if (!Array.isArray(v) || !finite(v[0]) || !finite(v[1])) return null
  return [v[0], v[1], finite(v[2]) ? v[2] : 0]
}

function sameNumber(a, b) {
  return Math.abs(a - b) <= COORDINATE_EPSILON * Math.max(1, Math.abs(a), Math.abs(b))
}

function samePoint(a, b) {
  return sameNumber(a[0], b[0]) && sameNumber(a[1], b[1]) && sameNumber(a[2], b[2])
}

function samePoints(a, b) {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i += 1) if (!samePoint(a[i], b[i])) return false
  return true
}

// W4g-7b-03c: colour, linetype and lineweight, normalized off the projection
// so an older engine reply (no such fields) reads as plain ByLayer — the
// same "missing means default" reading every other optional projection field
// already gets in this module.
function propsOf(entity) {
  const aci = Number.isFinite(entity.aci) ? entity.aci : 256
  const trueColor = Array.isArray(entity.trueColor) && entity.trueColor.length === 3
    && entity.trueColor.every(finite) ? entity.trueColor.slice(0, 3) : null
  const linetype = typeof entity.linetype === 'string' && entity.linetype ? entity.linetype : 'ByLayer'
  const lineweight = Number.isFinite(entity.lineweight) ? entity.lineweight : -1
  return { aci, trueColor, linetype, lineweight }
}

/**
 * One projection entity ({id|handle, type, layer, closed, vertices, radius,
 * startDeg, endDeg}) -> its geometry in the contract's terms, or null when
 * it is not a kind the plan carries (the engine leaves those untouched, so
 * they never differ). LINE keeps its own kind for an ADD (the interpreter
 * makes a real LINE) and reads as a two-point polyline for a replacement
 * (the intake's idiom, which the server's set_points already covers).
 */
export function planGeometry(entity) {
  if (!entity || typeof entity !== 'object') return null
  const type = String(entity.type || '')
  const layer = typeof entity.layer === 'string' && entity.layer ? entity.layer : '0'
  const verts = Array.isArray(entity.vertices) ? entity.vertices : []
  const props = propsOf(entity)
  if (ROUND_KINDS.has(type)) {
    const c = point3(verts[0])
    const r = entity.radius
    if (!c || !finite(r) || r <= 0) return null
    if (type === 'CIRCLE') return { kind: 'CIRCLE', layer, c, r, props }
    if (!finite(entity.startDeg) || !finite(entity.endDeg)) return null
    return { kind: 'ARC', layer, c, r, start_deg: entity.startDeg, end_deg: entity.endDeg, props }
  }
  if (!LINEAR_KINDS.has(type)) return null
  const pts = []
  for (const v of verts) {
    const p = point3(v)
    if (!p) return null
    pts.push(p)
  }
  if (type === 'LINE') return pts.length === 2 ? { kind: 'LINE', layer, pts, props } : null
  if (pts.length < 2) return null
  // W4g-6d: the projection's bulges ride along so a curved polyline is seen;
  // the contract's point list cannot carry them, so a geometry change on
  // such a polyline is a refusal in diffPlan, never a flattened set_points.
  const rawB = Array.isArray(entity.bulges) ? entity.bulges : []
  const bulges = rawB.length === pts.length && rawB.every(finite) ? rawB.slice() : new Array(pts.length).fill(0)
  const curved = rawB.length !== 0 && (rawB.length !== pts.length || !rawB.every(finite) || bulges.some((b) => Math.abs(b) > BULGE_EPS))
  return { kind: 'LWPOLYLINE', layer, closed: entity.closed === true, pts, bulges, curved, props }
}

/**
 * An editable entity the contract has no kind for (TEXT today): a fingerprint
 * of everything the projection reports, so a change, an add or a removal is
 * SEEN and refused instead of dropped. INSERT references remain opaque even
 * when read-only; a raw operation or a definition edit must never vanish.
 */
function opaqueOf(entity) {
  if (!entity || typeof entity !== 'object') return null
  const type = String(entity.type || '')
  if (!type || ROUND_KINDS.has(type) || LINEAR_KINDS.has(type)) return null
  const props = propsOf(entity)
  const print = JSON.stringify([type, entity.layer ?? null, entity.vertices ?? null, entity.radius ?? null, entity.startDeg ?? null,
    entity.endDeg ?? null, entity.text ?? null, entity.height ?? null, entity.rotationDeg ?? null, entity.bulges ?? null, entity.closed === true,
    // W4g-4b: an ELLIPSE's axis and ratio (the row that added them caught their absence here).
    entity.majorAxis ?? null, entity.ratio ?? null, entity.name ?? null, entity.ip ?? null, entity.scale ?? null,
    entity.columns ?? 1, entity.rows ?? 1, entity.columnSpacing ?? 0, entity.rowSpacing ?? 0,
    // W4g-7b-03c: colour, linetype and lineweight, so a property-only change
    // on an opaque kind (TEXT, POINT, ELLIPSE) is seen, not dropped.
    props.aci, props.trueColor, props.linetype, props.lineweight])
  return { kind: 'OPAQUE', type, print }
}

// W4g-7b-02c: a created or removed INSERT is a real mutation (contract v3's
// `added`/`removed`), never an opaque refusal; a MOVED or rescaled one stays
// opaque (the reference's own edit verbs never touch it in this round, so a
// change can only be a raw operation nobody should be able to hide). Returns
// null for anything that is not a well-formed INSERT (falls through to
// opaqueOf, which keeps today's refusal for a malformed reference).
function insertOf(entity) {
  if (!entity || entity.type !== 'INSERT') return null
  const ip = entity.ip
  if (!Array.isArray(ip) || !finite(ip[0]) || !finite(ip[1])) return null
  const rot = entity.rotationDeg
  if (!finite(rot)) return null
  const scale = entity.scale
  if (!Array.isArray(scale) || scale.length !== 3 || !scale.every(finite)) return null
  const name = String(entity.name ?? '')
  if (!name) return null
  const layer = typeof entity.layer === 'string' && entity.layer ? entity.layer : '0'
  const point = [ip[0], ip[1], 0]
  const props = propsOf(entity)
  // W4g-7b-03c: an INSERT reference's colour/linetype/lineweight now live on
  // its own EntityCommon (the crate accepts property ops on a reference); a
  // change to them is still a raw-operation refusal here, same as any other
  // in-place INSERT change, so it is never silently dropped from the print.
  const print = JSON.stringify([name, point, rot, scale, layer,
    entity.columns ?? 1, entity.rows ?? 1, entity.columnSpacing ?? 0, entity.rowSpacing ?? 0,
    props.aci, props.trueColor, props.linetype, props.lineweight])
  return { kind: 'INSERT', name, ip: point, rot, scale: scale.slice(), layer, print }
}

// Rotation degrees the way the contract's add carries them: [0, 360), 6 dp.
// W4g-7b-02c-e: round BEFORE wrapping, never after — wrapping a value that
// rounds up to exactly 360 (359.9999996) first, then rounding, lands back on
// 360 itself, outside the promised range.
// W4g-7b-02c-f: the wrap subtraction (d %= 360, d += 360) can reintroduce
// sub-ulp error above 6 dp (361.000001 must land back on exactly 1.000001,
// not its nearest double), so round AGAIN after wrapping; that second round
// can itself land exactly on 360 (a value the wrap wrote as 359.999999...998),
// which is mapped back to 0. The final `+ 0` turns a surviving -0 (deg
// exactly 0 or a negative value that rounds to -0) into +0, since a diff and
// its JSON never carry a sign no reader asked for.
function normalizedDeg(deg) {
  let d = Math.round(deg * 1e6) / 1e6
  d %= 360
  if (d < 0) d += 360
  d = Math.round(d * 1e6) / 1e6
  if (d === 360) d = 0
  return d + 0
}

function indexByHandle(entities) {
  const out = new Map()
  for (const entity of Array.isArray(entities) ? entities : []) {
    const geometry = planGeometry(entity) || insertOf(entity) || opaqueOf(entity)
    if (!geometry) continue
    const handle = hexHandle(entity.id ?? entity.handle ?? '')
    if (!handle) continue
    // A handle listed twice is nobody's: the contract refuses ambiguity,
    // and so does this side, by leaving both out of the plan.
    if (out.has(handle)) { out.set(handle, null); continue }
    out.set(handle, geometry)
  }
  for (const [handle, geometry] of out) if (geometry === null) out.delete(handle)
  return out
}

const isLinear = (g) => g.kind === 'LINE' || g.kind === 'LWPOLYLINE'

function sameRound(a, b) {
  if (!samePoint(a.c, b.c) || !sameNumber(a.r, b.r)) return false
  if (a.kind === 'ARC') return sameNumber(a.start_deg, b.start_deg) && sameNumber(a.end_deg, b.end_deg)
  return true
}

// W4g-7b-03c: a created entity whose properties are not plain ByLayer rides
// as a "styled add" — the three fields present only when they differ from
// the ByLayer default, so an ordinary add's wire shape is unchanged.
function styleOf(g) {
  const props = g.props
  if (!props) return {}
  const styled = {}
  if (props.aci !== 256) styled.color = props.aci
  if (props.linetype.toLowerCase() !== 'bylayer') styled.linetype = props.linetype
  if (props.lineweight !== -1) styled.lineweight = props.lineweight
  return styled
}

function addedRecord(handle, g) {
  if (g.kind === 'CIRCLE') return { handle, kind: 'CIRCLE', layer: g.layer, c: g.c, r: g.r, ...styleOf(g) }
  if (g.kind === 'ARC') return { handle, kind: 'ARC', layer: g.layer, c: g.c, r: g.r, start_deg: g.start_deg, end_deg: g.end_deg, ...styleOf(g) }
  if (g.kind === 'LINE') return { handle, kind: 'LINE', layer: g.layer, pts: g.pts, ...styleOf(g) }
  if (g.kind === 'INSERT') return { handle, kind: 'INSERT', name: g.name, pt: g.ip, rot: normalizedDeg(g.rot), scale: g.scale, layer: g.layer }
  return { handle, layer: g.layer, closed: g.closed, pts: g.pts, ...styleOf(g) }
}

const byHandle = (a, b) => (a.handle < b.handle ? -1 : a.handle > b.handle ? 1 : 0)

/**
 * The plan from `committed` (the entity list the head document loaded with)
 * to `current` (the list now). Resolves `{ mutations, count, reason }`:
 * `mutations` is the contract object (only the non-empty lists present) or
 * null with `reason` naming why no plan can carry this edit; `count` is the
 * operation count either way. A count of 0 with no reason means nothing the
 * contract sees changed.
 */
export function diffPlan(committed, current) {
  const before = indexByHandle(Array.isArray(committed) ? committed : committed?.entities)
  const after = indexByHandle(Array.isArray(current) ? current : current?.entities)
  const added = []
  const removed = []
  const setLayer = []
  const setPoints = []
  const setCircle = []
  const setArc = []
  const setColor = []
  const setLinetype = []
  const setLineweight = []
  const cannot = (reason) => ({ mutations: null, count: 0, reason })
  // The engine digest covers EVERY child, including unlisted/unsupported ones.
  // Keep the legacy full-record fallback for older projections without digests.
  const canonical = (value) => Array.isArray(value) ? value.map(canonical)
    : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])])) : value
  const definitions = (projection) => new Map((Array.isArray(projection?.blocks) ? projection.blocks : [])
    .map((block) => [block.name, typeof block.digest === 'string'
      ? JSON.stringify([block.name, block.digest]) : JSON.stringify(canonical(block))]))
  const oldBlocks = definitions(committed)
  const newBlocks = definitions(current)
  for (const name of new Set([...oldBlocks.keys(), ...newBlocks.keys()])) {
    if (oldBlocks.get(name) === newBlocks.get(name)) continue
    const change = !oldBlocks.has(name) ? 'added' : !newBlocks.has(name) ? 'removed' : 'changed'
    return cannot(`block ${name} is a definition the plan cannot carry, and it was ${change}`)
  }
  for (const [handle, was] of before) {
    const now = after.get(handle)
    if (!now) {
      // A kind the contract has no add for still has a remove (by handle);
      // an OPAQUE entity erased is one the plan cannot see go.
      if (was.kind === 'OPAQUE') return cannot(`entity ${handle} is a ${was.type} the plan cannot carry, and it was removed`)
      removed.push(handle)
      continue
    }
    if (was.kind === 'OPAQUE' || now.kind === 'OPAQUE') {
      if (was.kind === now.kind && was.print === now.print) continue
      const name = was.kind === 'OPAQUE' ? was.type : now.type
      return cannot(`entity ${handle} is a ${name} the plan cannot carry, and it changed`)
    }
    if (was.kind !== now.kind && !(isLinear(was) && isLinear(now))) {
      return cannot(`entity ${handle} changed kind from ${was.kind} to ${now.kind}, which the plan cannot express`)
    }
    // W4g-7b-02c: an INSERT is a real add/remove but stays opaque for any
    // in-place change (a move, a rescale, a re-layer): no edit verb touches
    // one in this round, so a change is a raw operation, never a silent drop.
    if (was.kind === 'INSERT') {
      if (was.print === now.print) continue
      return cannot(`entity ${handle} is a INSERT the plan cannot carry, and it changed`)
    }
    if (was.layer !== now.layer) setLayer.push({ handle, layer: now.layer })
    // W4g-7b-03c: colour, linetype and lineweight lower independently of
    // geometry, so an entity whose geometry ALSO changed carries both. A
    // true colour that appeared or changed cannot be carried (the contract
    // is ACI only); a true colour that stayed exactly what it was, or that
    // was cleared by an ACI set, is not this case (the aci compare below
    // still catches the clear-and-recolour as an ordinary set_color).
    const wasProps = was.props
    const nowProps = now.props
    const trueColorChanged = JSON.stringify(wasProps.trueColor) !== JSON.stringify(nowProps.trueColor)
    if (trueColorChanged && nowProps.trueColor) {
      return cannot(`entity ${handle} has a true colour the plan cannot carry`)
    }
    if (wasProps.aci !== nowProps.aci) setColor.push({ handle, aci: nowProps.aci })
    if (wasProps.linetype.toLowerCase() !== nowProps.linetype.toLowerCase()) setLinetype.push({ handle, name: nowProps.linetype })
    if (wasProps.lineweight !== nowProps.lineweight) setLineweight.push({ handle, weight: nowProps.lineweight })
    if (now.kind === 'CIRCLE') {
      if (!sameRound(was, now)) setCircle.push({ handle, c: now.c, r: now.r })
    } else if (now.kind === 'ARC') {
      if (!sameRound(was, now)) setArc.push({ handle, c: now.c, r: now.r, start_deg: now.start_deg, end_deg: now.end_deg })
    } else {
      const wasClosed = was.kind === 'LWPOLYLINE' && was.closed
      const nowClosed = now.kind === 'LWPOLYLINE' && now.closed
      const wasB = was.bulges || []
      const nowB = now.bulges || []
      const sameBulges = wasB.length === nowB.length && wasB.every((b, i) => sameNumber(b, nowB[i]))
      if (wasClosed !== nowClosed || !samePoints(was.pts, now.pts) || !sameBulges) {
        // A set_points carries points only: a curved polyline (before or
        // after) would be written back as its chords. Refuse, never flatten.
        if (was.curved || now.curved) return cannot(`polyline ${handle} has curved segments the plan cannot carry`)
        setPoints.push({ handle, closed: nowClosed, pts: now.pts })
      }
    }
  }
  for (const [handle, now] of after) {
    if (before.has(handle)) continue
    if (now.kind === 'OPAQUE') return cannot(`entity ${handle} is a ${now.type} the plan cannot carry, and it was added`)
    if (now.curved) return cannot(`polyline ${handle} has curved segments the plan cannot carry`)
    added.push(addedRecord(handle, now))
  }
  const count = added.length + removed.length + setLayer.length + setPoints.length + setCircle.length + setArc.length
    + setColor.length + setLinetype.length + setLineweight.length
  if (count > MAX_PLAN_OPERATIONS) {
    return { mutations: null, count, reason: `this edit changes ${count} entities, over the ${MAX_PLAN_OPERATIONS} a plan can carry` }
  }
  const mutations = {}
  if (added.length) mutations.added = added.sort(byHandle)
  if (removed.length) mutations.removed = removed.sort()
  if (setLayer.length) mutations.set_layer = setLayer.sort(byHandle)
  if (setPoints.length) mutations.set_points = setPoints.sort(byHandle)
  if (setCircle.length) mutations.set_circle = setCircle.sort(byHandle)
  if (setArc.length) mutations.set_arc = setArc.sort(byHandle)
  if (setColor.length) mutations.set_color = setColor.sort(byHandle)
  if (setLinetype.length) mutations.set_linetype = setLinetype.sort(byHandle)
  if (setLineweight.length) mutations.set_lineweight = setLineweight.sort(byHandle)
  return { mutations, count, reason: null }
}
