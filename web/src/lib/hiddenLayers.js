// GAPS 51 (film take 4): a drawing's own hidden layers (frozen or off in its
// layer table, recorded by the intake as `hidden_layers`) start hidden in the
// viewer, the way AutoCAD shows them. A layer the user has toggled keeps the
// user's choice. One pass over the hidden list; returns the input record
// unchanged when there is nothing to default, and the SAME output object for
// the same (record, list) pair (a per-record WeakMap entry), so a caller can
// use it in a render body and in dependency arrays without a hook.
const cache = new WeakMap()
export function withHiddenLayerDefaults(visibleLayers, hiddenLayers) {
  if (!Array.isArray(hiddenLayers) || hiddenLayers.length === 0) return visibleLayers
  const key = visibleLayers && typeof visibleLayers === 'object' ? visibleLayers : null
  const hit = key && cache.get(key)
  if (hit && hit.hidden === hiddenLayers) return hit.out
  const out = computeDefaults(visibleLayers, hiddenLayers)
  if (key) cache.set(key, { hidden: hiddenLayers, out })
  return out
}
function computeDefaults(visibleLayers, hiddenLayers) {
  const base = visibleLayers || {}
  let out = null
  for (const layer of hiddenLayers) {
    if (typeof layer !== 'string' || Object.prototype.hasOwnProperty.call(base, layer)) continue
    if (!out) out = { ...base }
    out[layer] = false
  }
  return out || visibleLayers
}
