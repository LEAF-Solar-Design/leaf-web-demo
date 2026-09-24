// studio.leafautomation.ai is the Studio door origin; it serves the same
// production build as the two platform hosts. Nothing else is admitted.
export const PROD_HOSTS = Object.freeze(['platform.leafdesign.ai', 'app.leafdesign.ai', 'studio.leafautomation.ai'])

const EXPECTED_SHA = /^[0-9a-f]{40}$/

function parseProdUrl(value) {
  if (typeof value !== 'string' || Buffer.byteLength(value, 'utf8') > 4096) {
    throw new Error('production driver refuses invalid or oversized URL: not a production host')
  }
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    throw new Error('production driver refuses invalid URL: not a production host')
  }
  if (parsed.protocol !== 'https:' || !PROD_HOSTS.includes(parsed.host) || parsed.username || parsed.password) {
    throw new Error(`production driver refuses ${parsed.host}: not a production host`)
  }
  return parsed
}

export function resolveProdBaseUrl(env = process.env) {
  const value = env.LEAF_E2E_PROD_BASE_URL
  if (!value) throw new Error('LEAF_E2E_PROD_BASE_URL is not set')
  parseProdUrl(value)
  return value
}

export function assertProdResponse(url) {
  return parseProdUrl(url)
}

// The candidate sha a release run promotes. Null when unset or empty; fails
// closed on anything but exactly 40 lowercase hex characters.
export function resolveExpectedSha(env = process.env) {
  const value = env.LEAF_E2E_EXPECTED_SHA
  if (value === undefined || value === '') return null
  if (typeof value !== 'string' || !EXPECTED_SHA.test(value)) {
    throw new Error('LEAF_E2E_EXPECTED_SHA must be 40 lowercase hex characters')
  }
  return value
}

// A required run (LEAF_E2E_PROD_REQUIRED=1) must fail, never skip, when its
// target or its expected candidate is missing.
export function requireProdTarget(env = process.env) {
  if (env.LEAF_E2E_PROD_REQUIRED !== '1') return
  if (!env.LEAF_E2E_PROD_BASE_URL) {
    throw new Error('LEAF_E2E_PROD_REQUIRED=1 but LEAF_E2E_PROD_BASE_URL is not set')
  }
  if (resolveExpectedSha(env) === null) {
    throw new Error('LEAF_E2E_PROD_REQUIRED=1 but LEAF_E2E_EXPECTED_SHA is not set')
  }
}
