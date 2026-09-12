// Browser board spatial state is per-device, not project collaboration state.
export const CARD_NAMES = ['drawing', 'versions', 'jobs', 'tools', 'catalog', 'shared']
const point = (value) => Number.isFinite(value?.x) && Number.isFinite(value?.y)

export function validWorldState(state) {
  return Boolean(point(state?.camera) && Number.isFinite(state.camera.zoom)
    && state.camera.zoom > 0 && CARD_NAMES.every((name) => point(state?.positions?.[name])))
}

export function load(scopeId) {
  try {
    const state = JSON.parse(globalThis.localStorage.getItem(`leaf.worldspace.v1.${scopeId}`))
    return validWorldState(state) ? state : null
  } catch { return null }
}

export function save(scopeId, state) {
  try {
    if (validWorldState(state)) globalThis.localStorage.setItem(`leaf.worldspace.v1.${scopeId}`, JSON.stringify(state))
  } catch { /* Storage can be unavailable or full; the live board still works. */ }
}

export default { load, save }
