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
export const PRODUCTION_HOSTS = Object.freeze(['app.leafdesign.ai', 'platform.leafdesign.ai'])
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
    if (PRODUCTION_HOSTS.includes(override.toLowerCase())) {
      throw new StagingHostError(`${STAGING_ALLOW_HOST_ENV}: ${override} is a production host`)
    }
    if (!ALLOWED_OVERRIDE_HOST.test(override)) {
      throw new StagingHostError(
        `${STAGING_ALLOW_HOST_ENV} must be a non-production *.leafdesign.ai hostname: ${override}`,
      )
    }
    allowed.add(override.toLowerCase())
  }
  return allowed
}

/** Full-origin allowlist: ports are part of an origin, and URL.origin
 * omits the default 443 for https, so `https://host:4443` never equals
 * `https://host`. Every guard below compares EXACT origins. */
export function allowedStagingOrigins(env = process.env) {
  return new Set([...allowedStagingHostnames(env)].map((hostname) => `https://${hostname}`))
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
  if (!allowedStagingOrigins(env).has(parsed.origin.toLowerCase())) {
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
  const allowed = allowedStagingOrigins(env)
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
    if (!allowed.has(parsed.origin.toLowerCase())) {
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
  if (!allowedStagingOrigins(env).has(parsed.origin.toLowerCase())) {
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
  if (baseURL !== DEFAULT_STAGING_BASE_URL && parsed.protocol !== 'https:') {
    throw new StagingHostError(`LEAF_E2E_STAGING_BASE_URL must use https: ${baseURL}`)
  }
  // Full ORIGIN, not hostname: a nonstandard port on an allowed hostname is a
  // different origin and must be refused too.
  if (!allowedStagingOrigins(env).has(parsed.origin.toLowerCase())) {
    throw new StagingHostError(
      `refusing to run the staging proof suite against origin "${parsed.origin}". ` +
      `It must equal "https://${defaultHost}", or exactly match ${STAGING_ALLOW_HOST_ENV} if that is set. ` +
      'This guard exists so a stray production base URL can never masquerade as staging evidence.',
    )
  }
  return parsed
}
