import { authHeaders, config, noteUnauthorized } from '../api.js'
import { fetchWithBudget } from '../fetchBudget.js'

const VIEW_IDS = ['graph', 'widgets', 'history', 'runs', 'deploys']

export function isLabEmbedded(route, win = globalThis.window) {
  return route?.params?.embed === '1' && Boolean(win) && win.self !== win.top
}

// Route params are inert strings. They never supply credentials or host authority.
export function parseLabRoute(path, search = '') {
  if (typeof path !== 'string' || typeof search !== 'string') return null
  if (/[?#]/.test(path) || (search && !search.startsWith('?'))) return null
  const query = new URLSearchParams(search)
  const keys = [...query.keys()]
  if (new Set(keys).size !== keys.length) return null
  const surface = query.get('surface')
  const direct = /^\/app\/lab(?:\/(graph|widgets|history|runs|deploys))?\/?$/.exec(path)
  const surfaceRoute = /^\/(app|ty)\/?$/.test(path) && surface === 'lab'
  if (!direct && !surfaceRoute) return null
  if (surface !== null && surface !== 'lab') return null
  const view = direct ? direct[1] || 'graph' : query.get('view') || 'graph'
  if (!VIEW_IDS.includes(view)) return null
  if (direct && query.has('view') && query.get('view') !== view) return null
  query.delete('surface')
  query.delete('view')
  return { view, params: Object.fromEntries(query) }
}

// All accepted aliases format to one canonical path. Query values round-trip.
export function formatLabRoute(route) {
  if (!route || !VIEW_IDS.includes(route.view)) return null
  const params = route.params ?? {}
  if (typeof params !== 'object' || Array.isArray(params)) return null
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (key === 'surface' || key === 'view' || typeof value !== 'string') return null
    query.append(key, value)
  }
  const path = route.view === 'graph' ? '/app/lab' : `/app/lab/${route.view}`
  const search = query.toString()
  return search ? `${path}?${search}` : path
}

export async function fetchLabStatus({ fetchImpl = globalThis.fetch } = {}) {
  const unavailable = { state: 'unavailable', posture: null }
  try {
    const path = '/api/lab/status'
    const headers = { accept: 'application/json', 'X-Tenant-Id': config.tenant, ...authHeaders() }
    const response = noteUnauthorized(
      await fetchWithBudget(fetchImpl, `${config.apiBase}${path}`, { headers }),
      path,
      headers.Authorization,
    )
    if (response.status === 404) return { state: 'off', posture: null }
    if (response.status === 503) return { state: 'auth-off', posture: null }
    if (response.status === 401 || response.status === 403) return { state: 'signed-out', posture: null }
    if (!response.ok) return unavailable
    const data = await response.json()
    if (data?.ok !== true || data.schema !== 'leaf.lab-status.v1' || data.rail !== 'on'
      || !['local', 'staging', 'production'].includes(data.posture)
      || typeof data.tenant !== 'string' || !data.tenant.trim()) return unavailable
    return { state: 'on', posture: data.posture }
  } catch {
    return unavailable
  }
}
