import { decodeGroundFrameSlots } from './solarGroundSlots.js'

const COLLECTIONS = ['frames', 'panels', 'strings', 'inverters', 'routes']
const EQUIPMENT = ['string_inverter', 'combiner_box', 'central_inverter']
const ROUTES = ['start homerun', 'end homerun', 'feeder', 'trench']
const plain = value => value !== null && typeof value === 'object' &&
  [Object.prototype, null].includes(Object.getPrototypeOf(value))
const finite = value => typeof value === 'number' && Number.isFinite(value)
const refused = refusal => ({ polylines: [], refusal })
// Plans are null-prototype records, so a field the projector never wrote (points,
// refs or marker inherited from a polluted Object.prototype) reads undefined.
const planOf = fields => Object.assign(Object.create(null), fields)

// Every value the projector uses is read exactly once and copied, so validation
// and projection can never see different geometry. An array's length is read
// once and must be a non-negative safe integer (a Proxy can report any length);
// elements are read by index from 0 to that length, never through an iterator.
const lengthOf = value => {
  if (!Array.isArray(value)) return null
  const length = value.length
  return Number.isSafeInteger(length) && length >= 0 ? length : null
}
// A point is an array of length 2 or 3 of finite numbers. Returns a fresh
// [x, y] (Z is checked and dropped), or null.
const pointOf = value => {
  const length = lengthOf(value)
  if (length !== 2 && length !== 3) return null
  const x = value[0]
  const y = value[1]
  if (!finite(x) || !finite(y) || (length === 3 && !finite(value[2]))) return null
  return [x, y]
}

// Mounted by App in Viewer's matching scene. This is a bounded 2D preview of stored
// design data, with projection-scoped validation; server/version rails remain
// authoritative for all other fields, topology, freshness and revisions.
// Viewer consumes only pts/color, so colours encode kinds. Equipment squares
// are symbolic 0.5-metre markers, not footprints. Trenches, labels, outlines,
// selection, layer visibility and automatic framing are excluded.
function project(graph, options) {
  if (graph === undefined || graph === null) return { polylines: [], refusal: null }
  if (!plain(graph)) return refused('invalid_graph')
  if (Reflect.ownKeys(graph).length === 0) return { polylines: [], refusal: null }
  if (graph.graph_schema_version !== 1) return refused('invalid_graph')
  const lists = {}
  for (const key of COLLECTIONS) {
    const list = graph[key]
    const length = lengthOf(list)
    if (length === null) return refused('invalid_graph')
    lists[key] = { list, length }
  }
  if (COLLECTIONS.some(key => lists[key].length > 100_000)) return refused('overlay_limit')
  const nonempty = COLLECTIONS.some(key => lists[key].length !== 0)
  const scale = nonempty ? options?.metersPerUnit : undefined
  if (nonempty && (!finite(scale) || scale <= 0)) return refused('invalid_scale')
  if (nonempty) {
    const projectEntity = graph.project
    const units = plain(projectEntity) ? projectEntity.units : undefined
    if (!plain(units) || units.compute_units !== 'm' ||
        units.meters_per_unit !== scale || scale <= 1e-12) return refused('invalid_graph')
  }

  const ids = new Set()
  const panels = new Map()
  // Plans retain copied geometry only. No viewer output is allocated until every
  // entity and the aggregate read-point bound have been checked.
  const plans = []
  // Returns the entity's id, read once, or null when the entity is malformed.
  const entityId = value => {
    if (!plain(value)) return null
    const id = value.id
    if (typeof id !== 'string' || id.length === 0 || ids.has(id)) return null
    ids.add(id)
    return id
  }
  let readPoints = 0
  const path = values => {
    const length = lengthOf(values)
    if (length === null) return { error: 'invalid_graph' }
    const pts = []
    for (let index = 0; index < length; index += 1) {
      readPoints += 1
      if (readPoints > 500_000) return { error: 'overlay_limit' }
      const pt = pointOf(values[index])
      if (pt === null) return { error: 'invalid_graph' }
      pts.push(pt)
    }
    return length === 1 ? { error: 'invalid_graph' } : { pts }
  }

  // The decoder receives a dense array of the same frame objects, read by index.
  const frames = []
  for (let index = 0; index < lists.frames.length; index += 1) {
    const frame = lists.frames.list[index]
    if (entityId(frame) === null) return refused('invalid_graph')
    frames.push(frame)
  }
  const decoded = decodeGroundFrameSlots(frames)
  if (decoded === null) return refused('invalid_graph')
  for (const frame of decoded.frames) {
    for (const slot of frame.slots) {
      if (ids.has(slot.panelId)) return refused('invalid_graph')
      ids.add(slot.panelId)
      panels.set(slot.panelId, [slot.centre.x, slot.centre.y])
    }
  }
  for (let index = 0; index < lists.panels.length; index += 1) {
    const panel = lists.panels.list[index]
    const id = entityId(panel)
    if (id === null) return refused('invalid_graph')
    const centre = pointOf(panel.centre)
    if (centre === null) return refused('invalid_graph')
    panels.set(id, centre)
  }
  for (let index = 0; index < lists.strings.length; index += 1) {
    const string = lists.strings.list[index]
    if (entityId(string) === null) return refused('invalid_graph')
    const refValues = string.ordered_panel_refs
    const refCount = lengthOf(refValues)
    if (refCount === null) return refused('invalid_graph')
    const refs = []
    const seen = new Set()
    for (let at = 0; at < refCount; at += 1) {
      const ref = refValues[at]
      if (typeof ref !== 'string' || !panels.has(ref) || seen.has(ref)) return refused('invalid_graph')
      seen.add(ref)
      refs.push(ref)
    }
    const route = path(string.route)
    if (route.error) return refused(route.error)
    if (route.pts.length) plans.push(planOf({ points: route.pts, color: '#22c55e' }))
    else if (refs.length > 1) plans.push(planOf({ refs, color: '#22c55e' }))
  }
  for (let index = 0; index < lists.inverters.length; index += 1) {
    const inverter = lists.inverters.list[index]
    if (entityId(inverter) === null) return refused('invalid_graph')
    const position = pointOf(inverter.position)
    const typed = Object.hasOwn(inverter, 'equipment_type')
    const type = typed ? inverter.equipment_type : undefined
    if (position === null || (typed && !EQUIPMENT.includes(type))) return refused('invalid_graph')
    plans.push(planOf({ marker: position, color: type === 'combiner_box' ? '#a855f7' : '#f59e0b' }))
  }
  for (let index = 0; index < lists.routes.length; index += 1) {
    const route = lists.routes.list[index]
    if (entityId(route) === null) return refused('invalid_graph')
    const kind = route.route_kind
    if (!ROUTES.includes(kind)) return refused('invalid_graph')
    const result = path(route.points)
    if (result.error) return refused(result.error)
    if (kind !== 'trench' && result.pts.length) {
      plans.push(planOf({ points: result.pts, color: kind === 'feeder' ? '#f472b6' : '#38bdf8' }))
    }
  }

  const polylines = []
  let emittedPoints = 0
  for (const plan of plans) {
    const count = plan.marker ? 5 : (plan.points || plan.refs).length
    if (polylines.length >= 4_096 || emittedPoints + count > 100_000) return refused('overlay_limit')
    let source = plan.points
    if (plan.marker) {
      const [x, y] = plan.marker
      source = [[x - .25, y - .25], [x + .25, y - .25], [x + .25, y + .25],
        [x - .25, y + .25], [x - .25, y - .25]]
    }
    const pts = []
    for (let index = 0; index < count; index += 1) {
      const value = source ? source[index] : panels.get(plan.refs[index])
      const x = value[0] / scale
      const y = value[1] / scale
      if (!finite(x) || !finite(y) || !finite(Math.fround(x)) || !finite(Math.fround(y))) return refused('invalid_graph')
      pts.push([x === 0 ? 0 : x, y === 0 ? 0 : y])
    }
    polylines.push({ pts, color: plan.color })
    emittedPoints += count
  }
  return { polylines, refusal: null }
}

export function solarGraphOverlay(graph, options) {
  try {
    return project(graph, options)
  } catch {
    // Proxies, accessors and other malformed caller values cannot escape.
    return refused('invalid_graph')
  }
}

// The intake on the Viewer's canvas, from the override the Viewer reported and the base intake it is
// given: the override when it was applied over that same base, else the base. The Viewer drops an
// override one commit after its base changes, so an override reported over another base is gone.
export function solarOverlayCanvasIntake(override, baseIntake) {
  const applied = override != null && override.base === baseIntake ? override.intake : null
  return applied || baseIntake || null
}
