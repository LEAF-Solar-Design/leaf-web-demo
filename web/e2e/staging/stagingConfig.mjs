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

export function allowedStagingHostnames(env = process.env) {
  const defaultHost = new URL(DEFAULT_STAGING_BASE_URL).hostname
  const override = env[STAGING_ALLOW_HOST_ENV] || ''
  const allowed = new Set([defaultHost])
  if (override) {
    if (!ALLOWED_OVERRIDE_HOST.test(override) || override.toLowerCase() === PRODUCTION_HOST) {
      throw new StagingHostError(
        `${STAGING_ALLOW_HOST_ENV} must be a non-production *.leafdesign.ai hostname: ${override}`,
      )
    }
    allowed.add(override.toLowerCase())
  }
  return allowed
}

/** The same https+allowlist rule for API responses: a redirect that resolves
 * an authenticated call off-origin must fail the test, not become evidence. */
export function assertResponseOnAllowedOrigin(response, env = process.env) {
  let parsed
  try {
    parsed = new URL(response.url())
  } catch {
    throw new StagingHostError(`staging API call resolved to an invalid URL: ${response.url()}`)
  }
  if (parsed.protocol !== 'https:' || !allowedStagingHostnames(env).has(parsed.hostname.toLowerCase())) {
    throw new StagingHostError(`staging API call resolved off the allowed HTTPS origin: ${response.url()}`)
  }
  return parsed
}

/** Registers a page-wide monitor for BEARER leakage: every browser request
 * that carries an Authorization header must target an allowed https origin
 * (the app attaches the JWT to its built-in API base, so a bundle built with
 * a foreign base would otherwise exfiltrate silently). Returns the mutable
 * violations array; assert it is empty at the end of the test. */
export function collectBearerLeaks(page, env = process.env) {
  const allowed = allowedStagingHostnames(env)
  const leaks = []
  page.on('request', (request) => {
    const headers = request.headers()
    if (!headers.authorization) return
    let parsed
    try {
      parsed = new URL(request.url())
    } catch {
      leaks.push(request.url())
      return
    }
    if (parsed.protocol !== 'https:' || !allowed.has(parsed.hostname.toLowerCase())) {
      leaks.push(request.url())
    }
  })
  return leaks
}

export function assertPageOnAllowedOrigin(page, env = process.env) {
  let parsed
  try {
    parsed = new URL(page.url())
  } catch {
    throw new StagingHostError(`staging browser navigated to an invalid URL: ${page.url()}`)
  }
  if (parsed.protocol !== 'https:' || !allowedStagingHostnames(env).has(parsed.hostname.toLowerCase())) {
    throw new StagingHostError(`staging browser navigated off the allowed HTTPS origin: ${page.url()}`)
  }
  return parsed
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
  const allowed = allowedStagingHostnames(env)
  if (baseURL !== DEFAULT_STAGING_BASE_URL && parsed.protocol !== 'https:') {
    throw new StagingHostError(`LEAF_E2E_STAGING_BASE_URL must use https: ${baseURL}`)
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
