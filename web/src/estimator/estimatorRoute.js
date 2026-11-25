import { authHeaders, config, noteUnauthorized } from '../api.js'
import { fetchWithBudget } from '../fetchBudget.js'
import { canonicalProject, estimatorOriginAllowed } from './estimatorOrigins.js'

export async function fetchEstimatorStatus({ fetchImpl = globalThis.fetch } = {}) {
  try {
    const path = '/api/estimator/status'
    const headers = { accept: 'application/json', 'X-Tenant-Id': config.tenant, ...authHeaders() }
    const response = noteUnauthorized(await fetchWithBudget(fetchImpl, `${config.apiBase}${path}`,
      { headers, cache: 'no-store' }), path, headers.Authorization)
    if (response.status === 404) return { state: 'off', bindings: [] }
    if (response.status === 401 || response.status === 403) return { state: 'signed-out', bindings: [] }
    if (!response.ok) throw new Error()
    const data = await response.json()
    if (data?.state !== 'on' || !Array.isArray(data.bindings) || !data.bindings.length || data.bindings.length > 32
      || !data.bindings.every(row => row && Object.keys(row).length === 2
        && canonicalProject(row.project_id) && estimatorOriginAllowed(row.estimator_origin))
      || new Set(data.bindings.map(row => row.project_id)).size !== data.bindings.length
      || new Set(data.bindings.map(row => row.estimator_origin)).size !== data.bindings.length) throw new Error()
    return { state: 'on', bindings: data.bindings.map(row => ({ ...row })) }
  } catch {
    return { state: 'unavailable', bindings: [] }
  }
}

export async function mintEstimatorHandoff(projectId, chan, { signal, fetchImpl = globalThis.fetch } = {}) {
  if (!canonicalProject(projectId) || typeof chan !== 'string' || chan.length !== 43 || !/^[A-Za-z0-9_-]+$/.test(chan)) {
    throw new Error('Estimator handoff unavailable')
  }
  const path = `/api/projects/${projectId}/estimator/handoff`
  const headers = { accept: 'application/json', 'Content-Type': 'application/json',
    'X-Tenant-Id': config.tenant, ...authHeaders() }
  const response = noteUnauthorized(await fetchWithBudget(fetchImpl, `${config.apiBase}${path}`, {
    method: 'POST', headers, body: JSON.stringify({ chan }), signal, cache: 'no-store',
  }, 10000), path, headers.Authorization)
  if (!response.ok) {
    const error = new Error('Estimator handoff unavailable')
    error.status = response.status
    throw error
  }
  return response.json()
}
