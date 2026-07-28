// Shared, single-source-of-truth config for the staging proof suite: which
// base URL to hit and which hosts that base URL is allowed to resolve to.
//
// Deliberately does NOT reuse LEAF_E2E_PROD_BASE_URL. That variable already
// means something else (playwright.prod.config.mjs points it at a real
// production-like target for e2e/prod). Reusing it here would let an operator
// who has it set for a prod run silently point this staging suite at
// production while every receipt still claims evidence_tier "staging".
export const STAGING_BASE_URL_ENV = 'LEAF_E2E_STAGING_BASE_URL'
export const STAGING_ALLOW_HOST_ENV = 'LEAF_E2E_STAGING_ALLOW_HOST'
export const DEFAULT_STAGING_BASE_URL = 'https://platform-staging.leafdesign.ai'

export class StagingHostError extends Error {}

export function resolveStagingBaseURL(env = process.env) {
  return env[STAGING_BASE_URL_ENV] || DEFAULT_STAGING_BASE_URL
}

/**
 * Throws unless the resolved base URL's hostname is exactly the default
 * staging host, or exactly matches an explicit LEAF_E2E_STAGING_ALLOW_HOST
 * override. This must run before any spec makes a network call.
 */
export function assertAllowedStagingHost(baseURL, env = process.env) {
  let parsed
  try {
    parsed = new URL(baseURL)
  } catch {
    throw new StagingHostError(`LEAF_E2E_STAGING_BASE_URL is not a valid absolute URL: ${baseURL}`)
  }
  const defaultHost = new URL(DEFAULT_STAGING_BASE_URL).hostname
  const override = env[STAGING_ALLOW_HOST_ENV] || ''
  const allowed = new Set([defaultHost])
  if (override) allowed.add(override)
  if (!allowed.has(parsed.hostname)) {
    throw new StagingHostError(
      `refusing to run the staging proof suite against host "${parsed.hostname}". ` +
      `It must equal "${defaultHost}", or exactly match ${STAGING_ALLOW_HOST_ENV} if that is set. ` +
      'This guard exists so a stray production base URL can never masquerade as staging evidence.',
    )
  }
  return parsed
}
