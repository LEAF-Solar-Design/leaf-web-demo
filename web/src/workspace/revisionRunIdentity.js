import { authHeaders, config } from '../api.js'
import { readActiveScope } from '../auth/activeScope.js'
import { fetchWithBudget } from '../fetchBudget.js'

export async function getToolExecutableIdentity(name, catalogDigest, signal) {
  const scope = readActiveScope()
  if (!scope) return null
  const query = new URLSearchParams({ catalog_digest: catalogDigest })
  const path = `/api/tools/${encodeURIComponent(name)}/executable-identity?${query}`
  const response = await fetchWithBudget(fetch, `${config.apiBase}${path}`, {
    cache: 'no-store', redirect: 'error', headers: authHeaders(), signal,
  })
  if (!response.ok) return null
  const { executable_identity: identity } = await response.json()
  return scope === readActiveScope() ? identity : null
}
