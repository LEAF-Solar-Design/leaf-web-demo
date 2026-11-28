import { decodeGroundFrameSlots } from './solarGroundSlots.js'

const COLLECTIONS = ['frames', 'panels', 'strings', 'inverters', 'routes']
const EQUIPMENT = ['string_inverter', 'combiner_box', 'central_inverter']
const ROUTES = ['start homerun', 'end homerun', 'feeder', 'trench']
const plain = value => value !== null && typeof value === 'object' &&
  [Object.prototype, null].includes(Object.getPrototypeOf(value))
const finite = value => typeof value === 'number' && Number.isFinite(value)
const own = (record, key) => plain(record) && Object.hasOwn(record, key) ? record[key] : undefined
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
// Cable route metadata lets the inspector identify the lines drawn on the canvas.
// Equipment and single-panel string squares are symbolic 0.5-metre markers.
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
  const frameByPanel = options?.completedPreview ? new Map() : null
  const frameIds = frameByPanel ? new Set() : null
  const drawingCentres = new Map()
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
    // One stored point is valid but has no line segment. A string can still
    // supply fallback geometry through its panel references.
    return { pts: length === 1 ? [] : pts }
  }

  // The decoder receives a dense array of the same frame objects, read by index.
  const frames = []
  for (let index = 0; index < lists.frames.length; index += 1) {
    const frame = lists.frames.list[index]
    const id = entityId(frame)
    if (id === null) return refused('invalid_graph')
    frameIds?.add(id)
    frames.push(frame)
  }
  const decoded = decodeGroundFrameSlots(frames)
  if (decoded === null) return refused('invalid_graph')
  for (const frame of decoded.frames) {
    for (const slot of frame.slots) {
      if (ids.has(slot.panelId)) return refused('invalid_graph')
      ids.add(slot.panelId)
      panels.set(slot.panelId, [slot.centre.x, slot.centre.y])
      frameByPanel?.set(slot.panelId, frame.frameId)
    }
  }
  for (let index = 0; index < lists.panels.length; index += 1) {
    const panel = lists.panels.list[index]
    const id = entityId(panel)
    if (id === null) return refused('invalid_graph')
    const centre = pointOf(panel.centre)
    frameByPanel?.set(id, own(panel, 'frame_ref'))
    if (centre === null) return refused('invalid_graph')
    // The drawing importer stores its source outline centres in drawing units.
    // Canonical panels and decoded Ground slots already use compute metres.
    const provenance = own(panel, 'provenance')
    if (own(provenance, 'created_by') === 'solar-panels-from-drawing') {
      drawingCentres.set(id, centre)
      panels.set(id, [centre[0] * scale, centre[1] * scale])
    } else {
      panels.set(id, centre)
    }
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
    let copiedCentres = false
    if (route.pts.length && route.pts.length === refs.length && refs.some(ref => drawingCentres.has(ref))) {
      const provenance = own(own(string, 'extra'), 'length_provenance')
      // Several tools copy the centres directly into a derived route. Use the
      // normalized panel positions only when the stored route matches that copy.
      copiedCentres = own(provenance, 'source') === 'derived' &&
        own(provenance, 'rule') === 'panel-centre-path' &&
        route.pts.every(([x, y], index) => {
          const ref = refs[index]
          const centre = drawingCentres.get(ref) || panels.get(ref)
          return x === centre[0] && y === centre[1]
        })
    }
    if (route.pts.length && !copiedCentres) plans.push(planOf({ points: route.pts, color: '#22c55e' }))
    else if (refs.length > 1) plans.push(planOf({ refs, color: '#22c55e' }))
    else if (refs.length === 1) plans.push(planOf({ marker: panels.get(refs[0]), color: '#22c55e' }))
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
    const id = entityId(route)
    if (id === null) return refused('invalid_graph')
    const kind = route.route_kind
    if (!ROUTES.includes(kind)) return refused('invalid_graph')
    const result = path(route.points)
    if (result.error) return refused(result.error)
    if (kind !== 'trench' && result.pts.length) {
      plans.push(planOf({
        points: result.pts,
        color: kind === 'feeder' ? '#f472b6' : '#38bdf8',
        route: { id, kind },
      }))
    }
  }

  if (frameByPanel) {
    const { frame_id: frame, groups } = options.completedPreview
    if (!frameIds.has(frame) ||
        !Array.isArray(groups) || groups.length < 1 || groups.length > 256) return refused('invalid_graph')
    const seen = new Set()
    for (const group of groups) {
      if (!Array.isArray(group) || !group.length || seen.size + group.length > 4096) return refused('invalid_graph')
      const refs = []
      for (const ref of group) {
        if (frameByPanel.get(ref) !== frame || seen.has(ref)) return refused('invalid_graph')
        seen.add(ref)
        refs.push(ref)
      }
      const color = '#60a5fa'
      plans.push(refs.length === 1 ? planOf({ marker: panels.get(refs[0]), color }) : planOf({ refs, color }))
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
    const line = { pts, color: plan.color }
    if (plan.route) line.route = plan.route
    polylines.push(line)
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
