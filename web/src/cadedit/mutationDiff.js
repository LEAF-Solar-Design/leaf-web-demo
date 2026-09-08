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
  return { kind: 'OPAQUE', type, print, props }
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
  // Placement changes refuse; the reference's own properties lower below.
  const print = JSON.stringify([name, point, rot, scale, layer,
    entity.columns ?? 1, entity.rows ?? 1, entity.columnSpacing ?? 0, entity.rowSpacing ?? 0])
  return { kind: 'INSERT', name, ip: point, rot, scale: scale.slice(), layer, print, props }
}

// W4g-7b-04c: a created or removed LINEAR/ALIGNED DIMENSION is a real
// mutation (the 04s add carries no measurement: the server computes it); a
// MOVED one (or any other in-place change — none is reachable through the
// store's own verbs, which refuse every one but delete) stays opaque, the
// same shape insertOf gives INSERT. Any other dimtype (OTHER, or a
// malformed record) returns null and falls through to opaqueOf, same as an
// unlisted read-only kind.
function dimensionOf(entity) {
  if (!entity || entity.type !== 'DIMENSION') return null
  const dimtype = entity.dimtype
  if (dimtype !== 'LINEAR' && dimtype !== 'ALIGNED') return null
  const def1 = point3(entity.def1)
  const def2 = point3(entity.def2)
  const dimline = point3(entity.dimline)
  if (!def1 || !def2 || !dimline) return null
  const rotation = dimtype === 'LINEAR' ? entity.rotationDeg : 0
  if (!finite(rotation)) return null
  const style = typeof entity.style === 'string' && entity.style ? entity.style : 'Standard'
  const layer = typeof entity.layer === 'string' && entity.layer ? entity.layer : '0'
  const print = JSON.stringify([dimtype, def1, def2, dimline, rotation, style, layer])
  return { kind: 'DIMENSION', dimtype, def1, def2, dimline, rotation, style, layer, print }
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
    const geometry = planGeometry(entity) || insertOf(entity) || dimensionOf(entity) || opaqueOf(entity)
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
  // W4g-7b-03c-g: the INSERT styled add. mutation_plan.py's styled-add rule
  // admits INSERT the same as the geometry kinds, so a created-then-coloured
  // reference must carry its properties or the route's properties note is
  // suppressed and the save lands uncoloured.
  if (g.kind === 'INSERT') return { handle, kind: 'INSERT', name: g.name, pt: g.ip, rot: normalizedDeg(g.rot), scale: g.scale, layer: g.layer, ...styleOf(g) }
  // W4g-7b-04c: no measurement (the server computes it); rotation rides
  // only for LINEAR (ALIGNED carries none of its own).
  if (g.kind === 'DIMENSION') {
    const record = { handle, kind: 'DIMENSION', dimtype: g.dimtype, def1: g.def1, def2: g.def2, dimline: g.dimline, style: g.style, layer: g.layer }
    if (g.dimtype === 'LINEAR') record.rotation = normalizedDeg(g.rotation)
    return record
  }
  return { handle, layer: g.layer, closed: g.closed, pts: g.pts, ...styleOf(g) }
}

const byHandle = (a, b) => (a.handle < b.handle ? -1 : a.handle > b.handle ? 1 : 0)

/** Compare child geometry and properties without its reassigned identity. */
export function sameBlockMember(a, b) {
  const left = planGeometry(a)
  const right = planGeometry(b)
  if (!left || !right || left.curved || right.curved) return false
  const equal = (x, y) => typeof x === 'number' && typeof y === 'number' ? sameNumber(x, y)
    : Array.isArray(x) && Array.isArray(y) ? x.length === y.length && x.every((v, i) => equal(v, y[i]))
      : x && y && typeof x === 'object' && typeof y === 'object'
        ? Object.keys(x).length === Object.keys(y).length && Object.keys(x).every((k) => equal(x[k], y[k])) : x === y
  const widths = (entity) => [entity.constantWidth ?? 0,
    entity.startWidths ?? (entity.vertices || []).map(() => 0),
    entity.endWidths ?? (entity.vertices || []).map(() => 0)]
  // Width refusal precedes quantization: even 1e-12 must never match zero.
  const equalWidths = (x, y) => Array.isArray(x) && Array.isArray(y)
    ? x.length === y.length && x.every((v, i) => equalWidths(v, y[i])) : x === y
  return equal(left, right) && equal(a.normal ?? [0, 0, 1], b.normal ?? [0, 0, 1]) && equalWidths(widths(a), widths(b))
}

// The server sorts geometry-only canonical JSON before attaching styles.
// The unique handle ends every comparison: fields after it cannot affect
// the ordinal. Refuse prefixes whose Python float spelling or normalized
// dimension geometry we cannot reproduce, instead of guessing an ordinal.
function additionSortPrefix(record) {
  const float = (value) => {
    if (!Number.isFinite(value) || (value !== 0 && (Math.abs(value) < 0.0001 || Math.abs(value) >= 1e16))) throw new Error('number spelling')
    return Number.isInteger(value) ? `${value === 0 ? 0 : value}.0` : String(value)
  }
  const handle = `"handle":${JSON.stringify(record.handle)}`
  if (record.kind === 'DIMENSION') throw new Error('normalized dimension geometry')
  if (record.c) return `{"c":[${record.c.map(float).join(',')}],${record.kind === 'ARC' ? `"end_deg":${float(record.end_deg)},` : ''}${handle}`
  if (!record.kind) return `{"closed":${record.closed},${handle}`
  return `{${handle}`
}

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
  // W4g-7b-05c-2: `kind` names the entity type that caused the refusal (null
  // when the refusal is not about one entity's own kind, e.g. a block
  // definition or the operation-count cap); `cause` is one of the closed set
  // the store's save reads to decide REJECT (moved-reference, true-colour, group-singleton)
  // vs. today's sidecar fallback (every other cause, including null).
  const cannot = (reason, kind = null, cause = null) => ({ mutations: null, count: 0, reason, kind, cause })
  let hard = null
  let soft = null
  const refuse = (reason, kind = null, cause = null) => {
    const refusal = cannot(reason, kind, cause)
    if (cause === 'moved-reference' || cause === 'true-colour' || cause === 'group-singleton' || cause === 'block-def-unmatched') hard ||= refusal
    else soft ||= refusal
  }
  // The engine digest covers EVERY child, including unlisted/unsupported ones.
  // Keep the legacy full-record fallback for older projections without digests.
  const canonical = (value) => Array.isArray(value) ? value.map(canonical)
    : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])])) : value
  const definitions = (projection) => new Map((Array.isArray(projection?.blocks) ? projection.blocks : [])
    .map((block) => [block.name, typeof block.digest === 'string'
      ? JSON.stringify([block.name, block.digest]) : JSON.stringify(canonical(block))]))
  const oldBlocks = definitions(committed)
  const newBlocks = definitions(current)
  const pendingBlocks = []
  for (const name of new Set([...oldBlocks.keys(), ...newBlocks.keys()])) {
    if (oldBlocks.get(name) === newBlocks.get(name)) continue
    const change = !oldBlocks.has(name) ? 'added' : !newBlocks.has(name) ? 'removed' : 'changed'
    if (change === 'added') { pendingBlocks.push((current.blocks || []).find((block) => block.name === name)); continue }
    refuse(`block ${name} is a definition the plan cannot carry, and it was ${change}`)
  }
  for (const [handle, was] of before) {
    const now = after.get(handle)
    if (!now) {
      // A kind the contract has no add for still has a remove (by handle);
      // an OPAQUE entity erased is one the plan cannot see go.
      if (was.kind === 'OPAQUE') {
        refuse(`entity ${handle} is a ${was.type} the plan cannot carry, and it was removed`, was.type, 'opaque-kind')
        continue
      }
      removed.push(handle)
      continue
    }
    if (was.kind === 'OPAQUE' || now.kind === 'OPAQUE') {
      const name = was.kind === 'OPAQUE' ? was.type : now.type
      if (now.props?.trueColor && JSON.stringify(was.props?.trueColor ?? null) !== JSON.stringify(now.props.trueColor)) {
        refuse(`entity ${handle} has a true colour the plan cannot carry`, name, 'true-colour')
        continue
      }
      if (was.kind === now.kind && was.print === now.print) continue
      refuse(`entity ${handle} is a ${name} the plan cannot carry, and it changed`, name, 'opaque-kind')
      continue
    }
    if (was.kind !== now.kind && !(isLinear(was) && isLinear(now))) {
      refuse(`entity ${handle} changed kind from ${was.kind} to ${now.kind}, which the plan cannot express`)
      continue
    }
    // An INSERT permits properties, but never a placement change.
    if (was.kind === 'INSERT' && was.print !== now.print) {
      refuse(`entity ${handle} is a INSERT the plan cannot carry, and it changed`, 'INSERT', 'moved-reference')
      continue
    }
    // W4g-7b-04c: a DIMENSION stays opaque for any in-place change, same as
    // INSERT — no verb the store exposes touches one but delete, so this is
    // a defensive refusal (a raw operation), never a silent drop.
    if (was.kind === 'DIMENSION') {
      if (was.print === now.print) continue
      refuse(`entity ${handle} is a DIMENSION the plan cannot carry, and it changed`, 'DIMENSION', 'moved-reference')
      continue
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
      refuse(`entity ${handle} has a true colour the plan cannot carry`, now.kind, 'true-colour')
      continue
    }
    // W4g-7b-03c-g F3: clearing a true colour back to a plain ACI can leave
    // the nearest-index projection (`aci`) unchanged, so the aci compare
    // alone misses it; the crate's no-op rule only exempts colour when the
    // head still carries rgb (mutation_plan.py), so a cleared true colour
    // always needs its own set_color even at an unchanged index.
    if (wasProps.aci !== nowProps.aci || (wasProps.trueColor && !nowProps.trueColor)) {
      setColor.push({ handle, aci: nowProps.aci })
    }
    if (wasProps.linetype.toLowerCase() !== nowProps.linetype.toLowerCase()) setLinetype.push({ handle, name: nowProps.linetype })
    if (wasProps.lineweight !== nowProps.lineweight) setLineweight.push({ handle, weight: nowProps.lineweight })
    if (now.kind === 'INSERT') continue
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
        if (was.curved || now.curved) {
          refuse(`polyline ${handle} has curved segments the plan cannot carry`, 'LWPOLYLINE', 'curved-geometry')
          continue
        }
        setPoints.push({ handle, closed: nowClosed, pts: now.pts })
      }
    }
  }
  for (const [handle, now] of after) {
    if (before.has(handle)) continue
    if (now.kind === 'OPAQUE') {
      refuse(`entity ${handle} is a ${now.type} the plan cannot carry, and it was added`, now.type, 'opaque-kind')
      continue
    }
    if (now.curved) {
      refuse(`polyline ${handle} has curved segments the plan cannot carry`, 'LWPOLYLINE', 'curved-geometry')
      continue
    }
    added.push(addedRecord(handle, now))
  }
  const groupMap = (projection) => new Map((projection?.groups || projection?.entities?.groups || []).map((group) => [group.name.toUpperCase(), group]))
  const oldGroups = groupMap(committed)
  const newGroups = groupMap(current)
  const addedGroups = []
  const removedGroups = []
  const memberHandles = (group) => [...new Set((group.memberIds || []).map(hexHandle))]
  const livingMembers = (group) => memberHandles(group).filter((id) => !before.has(id) || after.has(id))
  // Deleting entities repairs membership in the interpreter. Compare the
  // surviving old members so a natural singleton never becomes an invalid
  // one-member ADDGROUP replacement.
  const sameMembers = (a, b) => a.length === b.length && a.every((id) => b.includes(id))
  for (const [name, group] of oldGroups) {
    const now = newGroups.get(name)
    if (!now || !sameMembers(memberHandles(group).filter((id) => after.has(id)), livingMembers(now))) removedGroups.push(name)
  }
  for (const [name, group] of newGroups) {
    if (!oldGroups.has(name) || removedGroups.includes(name)) {
      const members = livingMembers(group)
      if (members.length < 2) refuse(`group ${name} needs at least two members to save; ungroup it or add a member`, null, 'group-singleton')
      else addedGroups.push({ name, members })
    }
  }
  added.sort(byHandle)
  if ((addedGroups.length || removedGroups.length || pendingBlocks.length) && added.length > 1) {
    try {
      const prefixes = new Map(added.map((record) => [record.handle, additionSortPrefix(record)]))
      added.sort((a, b) => prefixes.get(a.handle) < prefixes.get(b.handle) ? -1 : 1)
    } catch {
      refuse('same-plan ordinals cannot be established; save the new entities before grouping or creating a block', null, pendingBlocks.length ? 'block-def-unmatched' : null)
    }
  }
  const ordinal = new Map(added.map((record, index) => [record.handle, index]))
  const blockDefs = []
  const consumed = new Set()
  const committedEntities = Array.isArray(committed) ? committed : committed?.entities || []
  for (const block of pendingBlocks) {
    const unmatched = () => refuse(`Block ${block?.name || ''} cannot be saved: every child must match a distinct removed committed entity and its replacement INSERT.`, null, 'block-def-unmatched')
    const children = block?.children
    const base = Array.isArray(block?.base) && block.base.length === 3 && block.base.every(finite) && block.base[2] === 0 ? block.base.slice() : null
    if (!base || !Array.isArray(children) || !children.length || children.length > 60 || block.complete === false || block.baseUnknown) { unmatched(); continue }
    const members = []
    for (const child of children) {
      const match = committedEntities.find((entity) => {
        const handle = hexHandle(entity.id ?? entity.handle ?? '')
        return removed.includes(handle) && !consumed.has(handle) && sameBlockMember(entity, child)
      })
      if (!match) { unmatched(); break }
      const handle = hexHandle(match.id ?? match.handle)
      consumed.add(handle)
      members.push(handle)
    }
    const replacements = added.filter((record) => record.kind === 'INSERT' && record.name === block.name)
    const replacement = replacements[0]
    if (members.length !== children.length || replacements.length !== 1 || replacement.layer !== '0'
        || !samePoint(replacement.pt, base) || replacement.rot !== 0 || !samePoint(replacement.scale, [1, 1, 1])) { unmatched(); continue }
    blockDefs.push({ name: block.name, base, members: members.sort(), insert: ordinal.get(replacement.handle) })
  }
  for (const group of addedGroups) {
    group.members = group.members.map((handle) => {
      if (before.has(handle)) return handle
      if (ordinal.has(handle)) return { add: ordinal.get(handle) }
      refuse(`group ${group.name} has a member the plan cannot add`)
      return handle
    })
  }
  const count = blockDefs.length + addedGroups.length + removedGroups.length + added.length + removed.length + setLayer.length + setPoints.length + setCircle.length + setArc.length
    + setColor.length + setLinetype.length + setLineweight.length
  if (count > MAX_PLAN_OPERATIONS) {
    soft ||= { mutations: null, count, reason: `this edit changes ${count} entities, over the ${MAX_PLAN_OPERATIONS} a plan can carry` }
  }
  if (hard || soft) return hard || soft
  const mutations = {}
  if (blockDefs.length) mutations.block_defs = blockDefs.sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)
  if (added.length) mutations.added = added
  if (addedGroups.length) mutations.added_groups = addedGroups.sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)
  if (removedGroups.length) mutations.removed_groups = removedGroups.sort()
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
