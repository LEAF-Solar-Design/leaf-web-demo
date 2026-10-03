// Deployment defaults are the source of truth; local identity and routing
// are supplied by the walk instead of inherited from the deployed image.
export function shippedViteFlags(dockerfileText) {
  const flags = {}
  for (const line of dockerfileText.split(/\r?\n/)) {
    const match = line.match(/^\s*ARG\s+(VITE_[A-Z0-9_]+)\s*=\s*("[^"]*"|'[^']*'|[^\s#]*)\s*(?:#.*)?$/)
    if (!match) continue
    const [, name, raw] = match
    if (['VITE_API_BASE', 'VITE_MOCK', 'VITE_TENANT_ID'].includes(name) || name.startsWith('VITE_AUTH0_')) continue
    flags[name] = /^["']/.test(raw) ? raw.slice(1, -1) : raw
  }
  return flags
}

export function productionBundleCacheKey(flags, engineUnavailableReason = null) {
  return JSON.stringify({ flags: Object.fromEntries(Object.entries(flags).sort(([a], [b]) => a.localeCompare(b))), engine_unavailable_reason: engineUnavailableReason })
}
