export const PROD_HOSTS = Object.freeze(['platform.leafdesign.ai', 'app.leafdesign.ai'])

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
