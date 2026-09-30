import { entityToPolyline, hexHandle } from '../cadedit/engineIntake.js'

const list = (value) => Array.isArray(value) ? value : []
const handleOf = (value) => String(value ?? '').replace(/^0x/i, '').toUpperCase()
export const validObjectBounds = (b) => !!b && [b.minX, b.minY, b.maxX, b.maxY].every(Number.isFinite)
  && b.minX <= b.maxX && b.minY <= b.maxY
const union = (a, b) => !b ? a : !a ? { ...b } : ({ minX: Math.min(a.minX, b.minX),
  minY: Math.min(a.minY, b.minY), maxX: Math.max(a.maxX, b.maxX), maxY: Math.max(a.maxY, b.maxY) })

/** Pure, drawing-scoped navigation data. Definitions never become placed objects. */
export function buildDrawingObjectIndex({ drawingKey, entities, intake, solarGraph } = {}) {
  const records = [], byId = new Map(), byHandle = new Map(), graphIds = new Map(), counts = new Map(), layers = new Map()
  const issue = (code) => counts.set(code, (counts.get(code) || 0) + 1)
  const add = (id, kind, name) => {
    const record = { id, kind, name: String(name), aliases: [], path: '', bounds: null, physicalParentId: null,
      parentKind: null, electrical: [], handles: [], engineIds: [] }
    records.push(record); byId.set(id, record)
    return record
  }
  const boundsOf = (object) => {
    if (object.bounds != null) {
      if (validObjectBounds(object.bounds)) return { ...object.bounds }
      issue('invalid-bounds'); return null
    }
    let bounds = null, invalid = false
    const grow = (p) => {
      if (p == null) return
      const x = Array.isArray(p) ? p[0] : p.x, y = Array.isArray(p) ? p[1] : p.y
      if (!Number.isFinite(x) || !Number.isFinite(y)) { invalid = true; return }
      bounds = union(bounds, { minX: x, minY: y, maxX: x, maxY: y })
    }
    for (const p of list(object.pts || object.points || object.route || object.vertices)) {
      if (p == null) invalid = true
      else grow(p)
    }
    for (const key of ['pt', 'p1', 'p2', 'p3', 'p4', 'centre', 'position', 'insertion_point']) grow(object[key])
    if (invalid) { issue('invalid-bounds'); return null }
    return bounds
  }
  const layerFor = (name = '0') => {
    name = String(name ?? '0')
    const id = `layer:${name}`
    return byId.get(id) || add(id, 'layer', name)
  }
  const entityRecord = (handle, kind, layer) => {
    if (!handle) return null
    let record = byHandle.get(handle)
    if (!record) {
      const group = layerFor(layer)
      record = add(`h:${handle}`, kind, `${kind} · handle ${handle}`)
      record.handles.push(handle); byHandle.set(handle, record)
      record.physicalParentId = group.id
      layers.set(record, record.physicalParentId)
      record.parentKind = 'layer-fallback'
    }
    return record
  }
  for (const entity of list(entities)) {
    if (!entity || entity.type === 'BLOCK' || entity.type === 'BLOCK_RECORD') continue
    const record = entityRecord(handleOf(hexHandle(entity.id)), entity.type || entity.kind || 'entity', entity.layer)
    if (!record) continue
    record.engineIds.push(String(entity.id))
    const rawBounds = boundsOf(entity)
    const invalid = list(entity.vertices).some((p) => !Array.isArray(p) || !Number.isFinite(p[0]) || !Number.isFinite(p[1]))
    const geometry = invalid ? null : entityToPolyline(entity)
    record.bounds = entity.bounds != null || invalid ? rawBounds : geometry ? boundsOf(geometry) : rawBounds
  }
  for (const [key, kind] of [['polylines', 'LWPOLYLINE'], ['inserts', 'INSERT'], ['faces3d', '3DFACE'], ['circles', 'CIRCLE'], ['arcs', 'ARC']]) {
    for (const object of list(intake?.[key])) {
      if (!object) continue
      const record = entityRecord(handleOf(object.sourceHandle ?? object.handle), kind, object.layer)
      if (!record) continue
      let bounds = boundsOf(object)
      if (object.c && Number.isFinite(object.r) && object.r > 0) {
        bounds = boundsOf({ pts: [[object.c[0] - object.r, object.c[1] - object.r], [object.c[0] + object.r, object.c[1] + object.r]] })
      }
      record.bounds = union(record.bounds, bounds)
    }
  }
  let graph = solarGraph?.graph
  if (solarGraph && solarGraph.drawingKey !== drawingKey) { issue('drawing-key-mismatch'); graph = null }
  const objects = graph ? [graph.project, ...['frames', 'panels', 'strings', 'electrical_zones', 'inverters', 'routes', 'schedules']
    .flatMap((key) => list(graph[key]))].filter((o) => o && typeof o.id === 'string') : []
  for (const object of objects) {
    const handle = handleOf(object.provenance?.source_handle)
    let record = byHandle.get(handle)
    if (!record) {
      const id = handle ? `h:${handle}` : `g:${object.id}`
      record = byId.get(id) || add(id, object.kind, object.name || object.circuit_tag || object.id)
      if (handle) { record.handles.push(handle); byHandle.set(handle, record) }
    }
    record.kind = object.kind || record.kind
    record.aliases.push(object.id)
    const name = String(object.name || object.circuit_tag || '')
    if (name && name !== record.name) { record.aliases.push(record.name, name); record.name = name }
    record.bounds = union(record.bounds, boundsOf(object))
    graphIds.set(object.id, record)
  }
  const reference = (id) => {
    if (id == null) return null
    const record = graphIds.get(id)
    if (!record) issue('unknown-reference')
    return record
  }
  const contain = (child, parent) => {
    if (child && parent) { child.physicalParentId = parent.id; child.parentKind = 'physical' }
  }
  const memberships = new Map()
  const member = (record, parent) => {
    if (!memberships.has(record)) memberships.set(record, new Set())
    const ids = memberships.get(record)
    if (ids.has(parent.id)) return
    ids.add(parent.id); record.electrical.push({ id: parent.id, kind: parent.kind })
  }
  for (const object of objects) {
    const record = graphIds.get(object.id)
    contain(record, reference(object.frame_ref ?? object.parent_ref))
    for (const [field, value] of Object.entries(object)) {
      if (field === 'frame_ref' || field === 'parent_ref') continue
      if (field.endsWith('_refs') || field.endsWith('_ref')) {
        for (const id of field.endsWith('_refs') ? list(value) : [value]) {
          const target = reference(id)
          if (!target) continue
          if (object.kind === 'frame' && field === 'panel_refs') contain(target, record)
          else if (field === 'ordered_panel_refs' || (object.kind === 'zone-el' && field === 'panel_refs')) {
            member(target, record)
          } else if (field === 'electrical_zone_ref' || field === 'inverter_ref') {
            member(record, target)
          }
        }
      }
    }
  }
  for (const object of objects) {
    const record = graphIds.get(object.id), target = reference(object.assignment?.string_ref)
    if (target) member(record, target)
    // Nested references do not imply physical containment.
    const nested = ['matrix', 'sequences', 'panel_assignments', 'input_assignments', 'l1_assignments'].flatMap((key) => list(object[key]))
    while (nested.length) {
      const value = nested.pop()
      if (Array.isArray(value)) { for (const item of value) nested.push(item); continue }
      if (!value || typeof value !== 'object') continue
      for (const [key, ref] of Object.entries(value)) {
        if (key.endsWith('_refs')) { for (const id of list(ref)) reference(id) }
        else if (key.endsWith('_ref') || key === 'inverter_id') reference(ref)
        else if (ref && typeof ref === 'object') nested.push(ref)
      }
    }
  }
  // Iterative three-colour traversal: each parent edge is visited once, even for deep graphs.
  const done = new Set(), ordered = []
  for (const record of records) {
    if (done.has(record)) continue
    const chain = [], visiting = new Map()
    let cursor = record
    while (cursor && !done.has(cursor) && !visiting.has(cursor)) {
      visiting.set(cursor, chain.length); chain.push(cursor)
      cursor = byId.get(cursor.physicalParentId)
    }
    if (cursor && visiting.has(cursor)) {
      for (let i = visiting.get(cursor); i < chain.length; i++) {
        issue('cycle'); chain[i].physicalParentId = layers.get(chain[i]) || null
        chain[i].parentKind = chain[i].physicalParentId ? 'layer-fallback' : null
      }
    }
    for (let i = chain.length - 1; i >= 0; i--) { done.add(chain[i]); ordered.push(chain[i]) }
  }
  // Parents before children for paths; children before parents for aggregated bounds.
  for (const record of ordered) {
    const parent = byId.get(record.physicalParentId)
    record.path = `${parent?.path || (record.kind === 'layer' ? 'drawing / Layers' : 'drawing')} / ${record.name}`
  }
  for (let i = ordered.length - 1; i >= 0; i--) {
    const record = ordered[i], parent = byId.get(record.physicalParentId)
    if (parent) parent.bounds = union(parent.bounds, record.bounds)
  }
  for (const [record, layer] of layers) {
    const group = byId.get(layer)
    group.bounds = union(group.bounds, record.bounds)
  }
  for (const record of records) {
    if (record.bounds) {
      for (const axis of ['X', 'Y']) if (record.bounds[`min${axis}`] === record.bounds[`max${axis}`]) {
        record.bounds[`min${axis}`] -= 0.5; record.bounds[`max${axis}`] += 0.5
      }
      Object.freeze(record.bounds)
    }
    for (const key of ['aliases', 'electrical', 'handles', 'engineIds']) Object.freeze(record[key])
    Object.freeze(record)
  }
  const resolve = (input) => {
    const query = String(input ?? '').trim().replace(/^(zoom to |go to )/i, '').trim(), lower = query.toLowerCase()
    let matches = []
    if (query) {
      const exact = byId.get(query) || byHandle.get(handleOf(query))
      matches = exact ? [exact] : records.filter((r) => r.name.toLowerCase() === lower || r.aliases.some((a) => a.toLowerCase() === lower))
      if (!matches.length) matches = records.filter((r) => r.name.toLowerCase().includes(lower) || r.path.toLowerCase().includes(lower))
    }
    matches.sort((a, b) => a.path.localeCompare(b.path) || a.id.localeCompare(b.id))
    return { status: matches.length > 1 ? 'ambiguous' : matches.length ? 'unique' : 'missing', query,
      matches: matches.slice(0, 50), ...(matches.length > 50 ? { truncated: true } : {}) }
  }
  return Object.freeze({ drawingKey, intake, records: Object.freeze(records), byId, byHandle, resolve,
    issues: Object.freeze(Array.from(counts, ([code, count]) => Object.freeze({ code, count }))) })
}
