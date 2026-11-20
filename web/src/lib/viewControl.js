export const VIEW_CONTROL_EVENT = 'cockpit:view'

const operations = new Set(['fit', 'zoom_in', 'zoom_out', 'zoom_to_layer'])
// The server stamps env.ts; a browser clock up to 5 s behind it still sees a fresh event.
const MAX_CLOCK_SKEW_MS = 5000
const announced = new Set()

export function viewRequestFromEvent(env, nowMs) {
  if (env?.type !== 'tool_result' || env.data?.tool !== 'view_control' || env.data.ok === false) return null
  const result = env.data.result
  if (result?.contract !== 'leaf.view-request.v1' || !operations.has(result.op)) return null
  const age = nowMs - Date.parse(typeof env.ts === 'string' ? env.ts : '')
  if (!Number.isFinite(age) || age < -MAX_CLOCK_SKEW_MS || age > 60000) return null

  const { op, layer } = result
  if (op !== 'zoom_to_layer') return 'layer' in result ? null : { op }
  if (typeof layer !== 'string' || !layer.length || layer.length > 255 || /[\u0000-\u001f\u007f]/.test(layer)) return null
  return { op, layer }
}

export function findLayerName(intakeLayers, requested) {
  if (!Array.isArray(intakeLayers) || typeof requested !== 'string' || !requested) return null
  const names = intakeLayers.slice(0, 10000).filter(name => typeof name === 'string')
  if (names.includes(requested)) return requested
  const query = requested.toLowerCase()
  const matches = names.filter(name => name.toLowerCase() === query)
  if (matches.length) return matches.length === 1 ? matches[0] : null
  const partial = names.filter(name => name.toLowerCase().includes(query))
  return partial.length === 1 ? partial[0] : null
}

export function announceViewRequest(env, sessionId, nowMs = Date.now(), target = globalThis) {
  const request = viewRequestFromEvent(env, nowMs)
  if (!request) return
  const key = `${sessionId}:${env.seq}`
  if (announced.has(key)) return
  announced.add(key)
  if (announced.size > 64) announced.delete(announced.values().next().value)
  target.dispatchEvent(new CustomEvent(VIEW_CONTROL_EVENT, { detail: request }))
}
