// Project one exact canonical context intake without transport, fallback, or mounting.
import { solarGraphOverlay } from './solarGraphOverlay.js'

export const SOLAR_PROJECT_DRAWING_CODES = Object.freeze([
  'READY', 'GRAPHLESS', 'SCENE_PENDING', 'BINDING_INVALID', 'SCOPE_STALE',
  'CONTEXT_INVALID', 'CONTEXT_MISMATCH', 'GRAPH_PAIR_INVALID',
  'GRAPH_UNREADABLE', 'INVALID_GRAPH', 'INVALID_SCALE', 'OVERLAY_LIMIT',
])

const FIELDS = ['kind', 'organization_id', 'project_id', 'drawing_id', 'input_version_id']
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const HASH = /^[0-9a-f]{64}$/
const MISSING = Symbol('missing')
const plain = value => value !== null && typeof value === 'object' &&
  Object.getPrototypeOf(value) === Object.prototype
const uuid = value => typeof value === 'string' && UUID.test(value)
const hash = value => typeof value === 'string' && HASH.test(value)

// Read only own data properties, once; never invoke an accessor.
function own(value, key) {
  if (!plain(value)) throw new TypeError()
  const descriptor = Object.getOwnPropertyDescriptor(value, key)
  if (!descriptor) return MISSING
  if (!Object.hasOwn(descriptor, 'value')) throw new TypeError()
  return descriptor.value
}

// Mirrors createSolarDrawingBinding without adding a second module dependency.
function bindingOf(value) {
  if (!value || Object.getPrototypeOf(value) !== Object.prototype ||
      Reflect.ownKeys(value).length !== FIELDS.length) throw new TypeError()
  const binding = {}
  for (const field of FIELDS) {
    const descriptor = Object.getOwnPropertyDescriptor(value, field)
    if (!descriptor || !Object.hasOwn(descriptor, 'value') ||
        (field === 'kind' ? descriptor.value !== 'project' : !uuid(descriptor.value))) {
      throw new TypeError()
    }
    binding[field] = descriptor.value
  }
  return Object.freeze(binding)
}

const refused = reason => ({
  mode: 'refused', intake: null, drawingKey: null, overlay: null, reason,
})

export function projectSolarDrawingView(options) {
  let failure = 'BINDING_INVALID'
  try {
    const binding = bindingOf(own(options, 'solarBinding'))
    failure = 'SCOPE_STALE'
    if (own(options, 'current') !== true) return refused(failure)
    failure = 'CONTEXT_INVALID'
    const view = own(options, 'contextView')
    const context = own(view, 'context')
    const identity = {}
    for (const field of FIELDS.slice(1)) {
      const value = own(context, field)
      if (!uuid(value)) return refused(failure)
      identity[field] = value
    }
    if (FIELDS.slice(1).some(field => identity[field] !== binding[field])) {
      return refused('CONTEXT_MISMATCH')
    }
    const head = own(context, 'head_version_id')
    const isHead = own(context, 'is_head')
    if (!uuid(head) || typeof isHead !== 'boolean' ||
        isHead !== (head === identity.input_version_id) ||
        !hash(own(context, 'intake_sha256'))) return refused(failure)
    const intake = own(view, 'intake')
    if (!plain(intake)) return refused(failure)
    const drawingKey = 'project:' + FIELDS.slice(1).map(field => binding[field]).join(':')
    const ready = (reason, overlay = null) => ({
      mode: 'ready', intake, drawingKey, overlay, reason,
    })

    let projection
    try {
      const graph = own(intake, 'solar_design_graph')
      const digest = own(intake, 'solar_design_graph_sha256')
      if (graph === MISSING && digest === MISSING) return ready('GRAPHLESS')
      if (graph === MISSING || digest === MISSING || !hash(digest)) {
        return ready('GRAPH_PAIR_INVALID')
      }
      if (!plain(graph)) return ready('INVALID_GRAPH')
      const project = own(graph, 'project')
      const units = plain(project) ? own(project, 'units') : MISSING
      const scale = plain(units) ? own(units, 'meters_per_unit') : undefined
      projection = solarGraphOverlay(graph, {
        metersPerUnit: scale === MISSING ? undefined : scale,
      })
    } catch {
      return ready('GRAPH_UNREADABLE')
    }
    if (projection.refusal !== null) {
      const reason = projection.refusal === 'invalid_scale' ? 'INVALID_SCALE' :
        projection.refusal === 'overlay_limit' ? 'OVERLAY_LIMIT' : 'INVALID_GRAPH'
      return ready(reason)
    }
    try {
      const scene = own(options, 'scene')
      if (own(scene, 'intake') !== intake || own(scene, 'drawingKey') !== drawingKey ||
          own(scene, 'current') !== true) return ready('SCENE_PENDING')
    } catch {
      return ready('SCENE_PENDING')
    }
    return ready('READY', { intake, drawingKey, polylines: projection.polylines })
  } catch {
    return refused(failure)
  }
}
