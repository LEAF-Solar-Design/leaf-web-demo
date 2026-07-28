import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

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
const ALLOWED_OVERRIDE_HOST = /^[a-z0-9-]+(\.[a-z0-9-]+)*\.leafdesign\.ai$/i
const PRODUCTION_HOST = 'platform.leafdesign.ai'
const HERE = dirname(fileURLToPath(import.meta.url))
export const STAGING_OUTPUT_ROOT = resolve(HERE, '..', '..', '..', 'artifacts', 'unified-surface-proof', 'staging')

export class StagingHostError extends Error {}

export function resolveStagingBaseURL(env = process.env) {
  return env[STAGING_BASE_URL_ENV] || DEFAULT_STAGING_BASE_URL
}

export function stagingProofPath(...segments) {
  return join(STAGING_OUTPUT_ROOT, ...segments)
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
  if (baseURL !== DEFAULT_STAGING_BASE_URL && parsed.protocol !== 'https:') {
    throw new StagingHostError(`LEAF_E2E_STAGING_BASE_URL must use https: ${baseURL}`)
  }
  if (override) {
    if (!ALLOWED_OVERRIDE_HOST.test(override) || override.toLowerCase() === PRODUCTION_HOST) {
      throw new StagingHostError(
        `${STAGING_ALLOW_HOST_ENV} must be a non-production *.leafdesign.ai hostname: ${override}`,
      )
    }
    allowed.add(override.toLowerCase())
  }
  if (!allowed.has(parsed.hostname.toLowerCase())) {
    throw new StagingHostError(
      `refusing to run the staging proof suite against host "${parsed.hostname}". ` +
      `It must equal "${defaultHost}", or exactly match ${STAGING_ALLOW_HOST_ENV} if that is set. ` +
      'This guard exists so a stray production base URL can never masquerade as staging evidence.',
    )
  }
  return parsed
}
