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
// silently. Handles cross as DXF hex (the worker names them in decimal).
import { hexHandle } from './engineIntake.js'

export const MAX_PLAN_OPERATIONS = 5000
// A coordinate difference below this is the same number written twice (the
// engine re-parses the document it wrote after every edit); above it, the
// entity moved. Relative to the magnitude so a 10 km drawing keeps the rule.
export const COORDINATE_EPSILON = 1e-9

const ROUND_KINDS = new Set(['CIRCLE', 'ARC'])
const LINEAR_KINDS = new Set(['LINE', 'LWPOLYLINE', 'POLYLINE'])

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
  if (ROUND_KINDS.has(type)) {
    const c = point3(verts[0])
    const r = entity.radius
    if (!c || !finite(r) || r <= 0) return null
    if (type === 'CIRCLE') return { kind: 'CIRCLE', layer, c, r }
    if (!finite(entity.startDeg) || !finite(entity.endDeg)) return null
    return { kind: 'ARC', layer, c, r, start_deg: entity.startDeg, end_deg: entity.endDeg }
  }
  if (!LINEAR_KINDS.has(type)) return null
  const pts = []
  for (const v of verts) {
    const p = point3(v)
    if (!p) return null
    pts.push(p)
  }
  if (type === 'LINE') return pts.length === 2 ? { kind: 'LINE', layer, pts } : null
  if (pts.length < 2) return null
  return { kind: 'LWPOLYLINE', layer, closed: entity.closed === true, pts }
}

function indexByHandle(entities) {
  const out = new Map()
  for (const entity of Array.isArray(entities) ? entities : []) {
    const geometry = planGeometry(entity)
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

function addedRecord(handle, g) {
  if (g.kind === 'CIRCLE') return { handle, kind: 'CIRCLE', layer: g.layer, c: g.c, r: g.r }
  if (g.kind === 'ARC') return { handle, kind: 'ARC', layer: g.layer, c: g.c, r: g.r, start_deg: g.start_deg, end_deg: g.end_deg }
  if (g.kind === 'LINE') return { handle, kind: 'LINE', layer: g.layer, pts: g.pts }
  return { handle, layer: g.layer, closed: g.closed, pts: g.pts }
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
  const before = indexByHandle(committed)
  const after = indexByHandle(current)
  const added = []
  const removed = []
  const setLayer = []
  const setPoints = []
  const setCircle = []
  const setArc = []
  for (const [handle, was] of before) {
    const now = after.get(handle)
    if (!now) { removed.push(handle); continue }
    if (was.kind !== now.kind && !(isLinear(was) && isLinear(now))) {
      return { mutations: null, count: 0, reason: `entity ${handle} changed kind from ${was.kind} to ${now.kind}, which the plan cannot express` }
    }
    if (was.layer !== now.layer) setLayer.push({ handle, layer: now.layer })
    if (now.kind === 'CIRCLE') {
      if (!sameRound(was, now)) setCircle.push({ handle, c: now.c, r: now.r })
    } else if (now.kind === 'ARC') {
      if (!sameRound(was, now)) setArc.push({ handle, c: now.c, r: now.r, start_deg: now.start_deg, end_deg: now.end_deg })
    } else {
      const wasClosed = was.kind === 'LWPOLYLINE' && was.closed
      const nowClosed = now.kind === 'LWPOLYLINE' && now.closed
      if (wasClosed !== nowClosed || !samePoints(was.pts, now.pts)) {
        setPoints.push({ handle, closed: nowClosed, pts: now.pts })
      }
    }
  }
  for (const [handle, now] of after) if (!before.has(handle)) added.push(addedRecord(handle, now))
  const count = added.length + removed.length + setLayer.length + setPoints.length + setCircle.length + setArc.length
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
  return { mutations, count, reason: null }
}
