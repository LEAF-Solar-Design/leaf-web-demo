import { authHeaders, config, noteUnauthorized } from '../../api.js'
import { fetchWithBudget } from '../../fetchBudget.js'

function validTool(tool) {
  return tool && typeof tool.id === 'string' && Boolean(tool.id.trim())
    && typeof tool.version === 'string' && Boolean(tool.version.trim())
    && typeof tool.title === 'string' && typeof tool.available === 'boolean'
    && (tool.source === 'shipped' || tool.source === 'tenant')
    && typeof tool.effect === 'string' && tool.effect.length > 0 && tool.effect.length <= 32
    && (tool.reason === null || typeof tool.reason === 'string')
    && (tool.manifest === null || (typeof tool.manifest === 'object' && !Array.isArray(tool.manifest)
      && tool.manifest.id === tool.id && tool.manifest.version?.semver === tool.version))
}

export async function fetchLabTools({ fetchImpl = globalThis.fetch } = {}) {
  const unavailable = { state: 'unavailable', tools: [], truncated: false }
  try {
    const path = '/api/lab/tools'
    const headers = { accept: 'application/json', 'X-Tenant-Id': config.tenant, ...authHeaders() }
    const response = noteUnauthorized(
      await fetchWithBudget(fetchImpl, `${config.apiBase}${path}`, { headers }),
      path,
      headers.Authorization,
    )
    if (response.status === 404) return { state: 'off', tools: [], truncated: false }
    if (response.status === 401 || response.status === 403) return { state: 'signed-out', tools: [], truncated: false }
    if (!response.ok) return unavailable
    const data = await response.json()
    if (data?.ok !== true || data.schema !== 'leaf.lab-tools.v1'
      || !Array.isArray(data.tools) || typeof data.truncated !== 'boolean') return unavailable
    // One tool per id: the palette drags by id, so a later duplicate is dropped.
    const seen = new Set()
    const tools = data.tools.filter(tool => validTool(tool) && !seen.has(tool.id) && Boolean(seen.add(tool.id)))
    return { state: 'on', tools: tools.slice(0, 200), truncated: data.truncated || tools.length > 200 }
  } catch {
    return unavailable
  }
}

export function toolRegistry(tools) {
  return Object.fromEntries(tools.filter(tool => validTool(tool) && tool.manifest)
    .map(tool => [`${tool.id}@${tool.version}`, tool.manifest]))
}
