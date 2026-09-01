// Browser-safe reader for the consume-only ios_surface contract
// (GET /api/ios-surface/status). Mirrors the iosShipReadiness.js fetch idiom
// (shared api.js client, X-Tenant-Id, auth headers, noteUnauthorized) but is
// strictly READ-ONLY: it never launches, dispatches, or mutates.
//
// The server is the authority: it validates the contract against
// leaf.ios-ship-surface.v1, drops unknown fields, and fails closed on anything
// secret-shaped (server/routers/ios_surface.py). This reader therefore only
// distinguishes the three server outcomes and never re-validates field shapes
// the component already treats defensively (IosSurface.deriveState):
//   status "available" -> return the sanitized contract (for <IosSurface contract=>)
//   status "unavailable" (flag on, upstream down/absent) -> null (never-configured)
//   404 "refused" (flag off) / any network error -> null
// It NEVER throws and NEVER returns a stale contract: every call is live.
import { authHeaders, config, noteUnauthorized } from '../api.js'
import { fetchWithBudget } from '../fetchBudget.js'

export const IOS_SURFACE_CONTRACT_SCHEMA = 'leaf.ios-ship-surface.v1'

async function surfaceFetch(path, init, fetchImpl) {
  const headers = {
    'X-Tenant-Id': config.tenant,
    ...(init?.headers || {}),
    ...authHeaders(),
  }
  return noteUnauthorized(
    await fetchWithBudget(fetchImpl, `${config.apiBase}${path}`, { ...(init || {}), headers }),
    path,
    headers.Authorization,
  )
}

// Returns the sanitized contract object, or null when the surface is refused
// (flag off), unavailable (no upstream), or unreachable. Null is the exact
// value IosSurface treats as 'never-configured', so the caller can thread the
// result straight into <IosSurface contract={...} /> with no branching.
export async function fetchIosSurfaceStatus({ projectId, revision, fetchImpl = globalThis.fetch } = {}) {
  if (!projectId || !revision) return null
  try {
    const path = `/api/ios-surface/status?project_id=${encodeURIComponent(projectId)}`
      + `&revision=${encodeURIComponent(revision)}`
    const res = await surfaceFetch(path, { headers: { accept: 'application/json' } }, fetchImpl)
    if (!res.ok) return null // 404 refused (flag off) or any error status
    const data = await res.json().catch(() => null)
    if (!data || data.status !== 'available') return null
    const contract = data.contract
    if (!contract || contract.schema !== IOS_SURFACE_CONTRACT_SCHEMA) return null
    return contract
  } catch {
    return null // unreachable: truthful "nothing published", never a stale contract
  }
}
