import { useMemo } from 'react'
import { solarGraphOverlay } from './solarGraphOverlay.js'

export const SOLAR_OVERLAY_REASONS = Object.freeze({
  invalid_graph: 'Solar design lines are hidden because the saved design geometry could not be read.',
  invalid_scale: 'Solar design lines are hidden because the drawing scale is missing or invalid.',
  overlay_limit: 'Solar design lines are hidden because this design exceeds the preview size limit.',
  drawing_mismatch: 'Solar design lines are hidden until the canvas shows the matching saved drawing.',
  refresh_pending: 'Solar design lines are hidden while the saved drawing refresh is incomplete.',
})

const UNREADABLE = Symbol('unreadable Solar graph')
const UNREAD_SHOWN = Symbol('unreadable shown drawing')
const FAILED = Symbol('unreadable caller value')
const UNREADABLE_PROJECTION = Object.freeze({ unreadable: true })
const none = reason => ({ solarOverlay: null, reason })
const nonempty = value => typeof value === 'string' && value.length > 0

// Every caller value is read at most once per decision, and only when the decision reaches the step
// that needs it, in the frozen precedence order. Reads share one cache keyed by the object and the
// property, so an object passed in two places is still read once, and a read that throws is kept as
// FAILED and refuses at the step that needed it.
function callerReader() {
  const seen = new Map()
  return (owner, key) => {
    let cache = seen.get(owner)
    if (cache === undefined) {
      cache = new Map()
      seen.set(owner, cache)
    }
    if (cache.has(key)) return cache.get(key)
    let value
    try {
      value = owner[key]
    } catch {
      value = FAILED
    }
    cache.set(key, value)
    return value
  }
}

// The projection is read once, under one guard: its refusal first, then (only without one) its
// polylines and their length. A read that throws, or polylines that are not an array, refuse as an
// unreadable graph, and the overlay binds exactly the array whose length was checked.
function readProjection(projection) {
  try {
    const refusal = projection.refusal
    if (refusal != null) return { refusal, polylines: null, count: 0 }
    const polylines = projection.polylines
    if (!Array.isArray(polylines)) return UNREADABLE_PROJECTION
    return { refusal: null, polylines, count: polylines.length }
  } catch {
    return UNREADABLE_PROJECTION
  }
}

function decide(input, projection, read) {
  const enabled = read(input, 'enabled')
  if (enabled === FAILED) return none('drawing_mismatch')
  if (enabled !== true) return none(null)
  if (projection === UNREADABLE_PROJECTION) return none('invalid_graph')
  if (projection.refusal != null) {
    switch (projection.refusal) {
      case 'invalid_graph': return none('invalid_graph')
      case 'invalid_scale': return none('invalid_scale')
      case 'overlay_limit': return none('overlay_limit')
      default: return none('invalid_graph')
    }
  }
  if (projection.count === 0) return none(null)
  const refreshPending = read(input, 'refreshPending')
  if (refreshPending === FAILED) return none('drawing_mismatch')
  if (refreshPending === true) return none('refresh_pending')
  if (read(input, 'sceneCurrent') !== true) return none('drawing_mismatch')
  const shown = read(input, 'shown')
  if (shown === FAILED || shown == null) return none('drawing_mismatch')
  const drawingKey = read(input, 'drawingKey')
  if (!nonempty(drawingKey)) return none('drawing_mismatch')
  const requestedDrawingId = read(input, 'requestedDrawingId')
  if (!nonempty(requestedDrawingId)) return none('drawing_mismatch')
  const activeIntake = read(input, 'activeIntake')
  if (activeIntake === FAILED) return none('drawing_mismatch')
  const source = read(activeIntake ?? shown, 'source')
  if (source === FAILED) return none('drawing_mismatch')
  if (source !== 'engine') {
    if (activeIntake != null && activeIntake !== shown) return none('drawing_mismatch')
    return { solarOverlay: { polylines: projection.polylines, intake: shown, drawingKey }, reason: null }
  }
  if (activeIntake == null) return none('drawing_mismatch')
  const engineDocument = read(input, 'engineDocument')
  if (engineDocument === FAILED || engineDocument == null) return none('drawing_mismatch')
  if (read(engineDocument, 'documentOrigin') !== 'head') return none('drawing_mismatch')
  const documentId = read(engineDocument, 'documentId')
  if (!nonempty(documentId) || documentId !== read(activeIntake, 'documentId')) return none('drawing_mismatch')
  const prefix = `${requestedDrawingId}-v`
  if (!documentId.startsWith(prefix) || !/^\d+\.dxf$/.test(documentId.slice(prefix.length))) {
    return none('drawing_mismatch')
  }
  const activeVersion = read(input, 'activeVersion')
  if (!Number.isInteger(activeVersion) || activeVersion <= 0) return none('drawing_mismatch')
  const committedVersion = read(engineDocument, 'committedVersion')
  if (!Number.isInteger(committedVersion) || committedVersion <= 0 || committedVersion !== activeVersion) {
    return none('drawing_mismatch')
  }
  if (read(input, 'engineDirty') !== false) return none('drawing_mismatch')
  return { solarOverlay: { polylines: projection.polylines, intake: activeIntake,
    drawingKey: `engine:${documentId}` }, reason: null }
}

// Admission stays independent of projection and never retains an earlier scene.
export function decideSolarGraphOverlay(input, projection) {
  return decide(input, readProjection(projection), callerReader())
}

// Unmounted: A1b-2 supplies evaluated scene fences and attaches the result. The projection depends
// only on the caller's enabled flag and the shown drawing's graph and scale, so an admission value
// that cannot be read refuses that render without discarding the projection.
export function useSolarGraphOverlay(input) {
  const read = callerReader()
  const enabled = read(input, 'enabled') === true
  let graph
  let metersPerUnit
  if (enabled) {
    const shown = read(input, 'shown')
    if (shown === FAILED) {
      graph = UNREAD_SHOWN
    } else {
      const carried = shown == null ? undefined : read(shown, 'solar_design_graph')
      if (carried === FAILED) {
        graph = UNREADABLE
      } else {
        try {
          graph = carried
          metersPerUnit = graph?.project?.units?.meters_per_unit
        } catch {
          graph = UNREADABLE
          metersPerUnit = undefined
        }
      }
    }
  }
  const projection = useMemo(() => {
    if (!enabled || graph === UNREAD_SHOWN) return { polylines: [], refusal: null }
    if (graph === UNREADABLE) return { polylines: [], refusal: 'invalid_graph' }
    return solarGraphOverlay(graph, { metersPerUnit })
  }, [graph, metersPerUnit, enabled])
  if (graph === UNREAD_SHOWN) return none('drawing_mismatch')
  return decide(input, readProjection(projection), read)
}
