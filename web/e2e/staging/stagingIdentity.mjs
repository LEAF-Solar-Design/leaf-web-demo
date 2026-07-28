// Read-only deployed identity capture for the staging proof suite.
//
// Unlike scripts/deployed_authored_cad_acceptance.mjs (~lines 127-203), this
// helper does not require tenant JWTs, an allowed-host allowlist, or a pinned
// expected revision. It only records what /api/ready reports right now. The
// staging environment tonight is mid-reconvergence, so callers must not
// assert a specific source_revision -- only capture and report it.
const SOURCE_SHA_LIKE = /^[a-f0-9]{6,64}$/

/**
 * Fetches the deployed identity from the staging origin's public readiness
 * endpoint. No Authorization header is sent; /api/ready does not require one.
 *
 * @param {import('@playwright/test').APIRequestContext} request
 * @returns {Promise<{observed: boolean, source_revision: string|null, ready: boolean|null, degraded_mode: boolean|null, dependencies: Record<string, string>|null, raw: unknown}>}
 */
export async function captureStagingIdentity(request) {
  try {
    const response = await request.get('/api/ready', { timeout: 10_000 })
    const body = await response.json().catch(() => null)
    if (!response.ok() || !body) {
      return {
        observed: false,
        source_revision: null,
        ready: null,
        degraded_mode: null,
        dependencies: null,
        raw: { status: response.status() },
      }
    }
    const sourceRevision = typeof body.source_revision === 'string' && SOURCE_SHA_LIKE.test(body.source_revision)
      ? body.source_revision
      : (typeof body.source_revision === 'string' ? body.source_revision : null)
    const dependencies = body.dependencies && typeof body.dependencies === 'object'
      ? Object.fromEntries(
        Object.entries(body.dependencies).map(([name, value]) => [name, value?.state ?? 'unknown']),
      )
      : null
    return {
      observed: true,
      source_revision: sourceRevision,
      ready: body.ready === true,
      degraded_mode: body.degraded_mode === true,
      dependencies,
      raw: body,
    }
  } catch (error) {
    return {
      observed: false,
      source_revision: null,
      ready: null,
      degraded_mode: null,
      dependencies: null,
      raw: { error: error?.name || 'Error', message: error?.message },
    }
  }
}
