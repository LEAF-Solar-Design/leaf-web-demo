// Mandatory, fail-hard deployed identity capture for the staging proof suite.
//
// Unlike scripts/deployed_authored_cad_acceptance.mjs (~lines 127-203), this
// helper does not require tenant JWTs, an allowed-host allowlist, or a pinned
// expected revision -- staging may be mid-reconvergence, so no specific
// source_revision is ever asserted. It DOES require the call to succeed and
// to name a real revision: identity is load-bearing evidence stamped into
// every receipt's `source_commit`, so a silent failure here must never let a
// spec fall through to a stale or fabricated commit.
const SOURCE_REVISION = /^[0-9a-f]{7,64}$/i

export class StagingIdentityError extends Error {}

/**
 * Fetches the deployed identity from the staging origin's readiness
 * endpoint. No Authorization header is sent; /api/ready does not require
 * one. Throws StagingIdentityError -- failing the calling test -- on any
 * non-2xx response, a non-JSON body, or a missing/malformed source_revision.
 *
 * @param {import('@playwright/test').APIRequestContext} request
 * @param {{ requireEnvironment?: string }} [options]
 * @returns {Promise<{source_revision: string, ready: boolean, degraded_mode: boolean, environment: string|null, dependencies: Record<string, string>, endpoint: string, raw: unknown}>}
 */
export async function captureStagingIdentity(request, { requireEnvironment = 'staging' } = {}) {
  let response
  try {
    response = await request.get('/api/ready', { timeout: 10_000 })
  } catch (error) {
    throw new StagingIdentityError(`GET /api/ready failed: ${error?.name || 'Error'} ${error?.message || ''}`.trim())
  }
  if (!response.ok()) {
    throw new StagingIdentityError(`GET /api/ready returned HTTP ${response.status()}, expected 2xx`)
  }
  let body
  try {
    body = await response.json()
  } catch {
    throw new StagingIdentityError('GET /api/ready did not return a JSON body')
  }
  const sourceRevision = body?.source_revision
  if (typeof sourceRevision !== 'string' || !SOURCE_REVISION.test(sourceRevision)) {
    throw new StagingIdentityError(
      `GET /api/ready returned a missing or malformed source_revision: ${JSON.stringify(sourceRevision)}`,
    )
  }
  if (body?.environment !== undefined && body.environment !== requireEnvironment) {
    throw new StagingIdentityError(
      `GET /api/ready reported environment ${JSON.stringify(body.environment)}, expected ${JSON.stringify(requireEnvironment)}`,
    )
  }
  const dependencies = body?.dependencies && typeof body.dependencies === 'object'
    ? Object.fromEntries(
      Object.entries(body.dependencies).map(([name, value]) => [name, value?.state ?? 'unknown']),
    )
    : {}
  return {
    source_revision: sourceRevision,
    ready: body.ready === true,
    degraded_mode: body.degraded_mode === true,
    environment: body.environment ?? null,
    dependencies,
    endpoint: `GET /api/ready ${response.status()}`,
    raw: body,
  }
}
