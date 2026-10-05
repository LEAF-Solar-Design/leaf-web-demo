export const RECENT_PROJECTS_KEY = 'leaf.studio.recentProjects.v1'
export const RECENT_PROJECTS_LIMIT = 5

const validId = (id) => typeof id === 'string' && id.trim().length > 0
const idsOnly = (ids) => [...new Set(Array.isArray(ids) ? ids.filter(validId) : [])]
const normalize = (value) => ({
  recent: idsOnly(value?.recent).slice(0, RECENT_PROJECTS_LIMIT),
  pinned: idsOnly(value?.pinned),
})

export function recentProjectsKey(principal) {
  return validId(principal) ? `${RECENT_PROJECTS_KEY}.${encodeURIComponent(principal)}` : null
}

// Claims scope browser preferences only; the server still owns authentication.
// Never use an opaque bearer as a key or persist the bearer itself.
export function readProjectPrincipal(storage) {
  try {
    const target = storage === undefined ? globalThis.localStorage : storage
    const token = target?.getItem('leaf.jwt')
    const payload = token?.split('.')[1]?.replace(/-/g, '+').replace(/_/g, '/')
    if (!payload) return null
    const claims = JSON.parse(globalThis.atob(payload.padEnd(Math.ceil(payload.length / 4) * 4, '=')))
    if (!validId(claims?.sub)) return null
    return JSON.stringify([claims.iss || '', claims.sub, claims.org_id || ''])
  } catch { return null }
}

export function readRecentProjects(principal, storage) {
  try {
    const key = recentProjectsKey(principal)
    if (!key) return normalize(null)
    const target = storage === undefined ? globalThis.localStorage : storage
    return normalize(JSON.parse(target?.getItem(key) || 'null'))
  } catch { return normalize(null) }
}

export function writeRecentProjects(principal, value, storage) {
  try {
    const key = recentProjectsKey(principal)
    if (!key) return false
    const target = storage === undefined ? globalThis.localStorage : storage
    if (!target) return false
    target.setItem(key, JSON.stringify(normalize(value)))
    return true
  } catch { return false }
}

export function addRecentProject(value, id) {
  const state = normalize(value)
  return validId(id) ? { ...state, recent: [id, ...state.recent.filter((item) => item !== id)].slice(0, RECENT_PROJECTS_LIMIT) } : state
}

export function togglePinnedProject(value, id) {
  const state = normalize(value)
  if (!validId(id)) return state
  return { ...state, pinned: state.pinned.includes(id) ? state.pinned.filter((item) => item !== id) : [...state.pinned, id] }
}
