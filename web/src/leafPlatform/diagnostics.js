const PHASES = new Set([
  'start', 'hello-sent', 'handshake', 'handshake-rejected', 'envelope-rejected',
  'bind-sent', 'bind-result', 'command-sent', 'command-outcome', 'timeout',
])
const VALUES = {
  kind: new Set(['webview', 'none', 'ready', 'unbound', 'handshake', 'bind', 'command']),
  status: new Set(['accepted', 'rejected', 'applied', 'stale', 'unknown', 'superseded']),
  action: new Set(['select', 'focus']),
}
const REJECTIONS = new Set(['origin', 'shape', 'timestamp-format', 'lifetime', 'signature',
  'replay', 'session', 'capacity', 'verb'])

export function sanitizeDetail(phase, detail = {}) {
  const safe = {}
  if (!detail || typeof detail !== 'object') return safe
  for (const key of ['kind', 'reason', 'status', 'action', 'ms', 'count']) {
    const value = detail[key]
    if (value === undefined) continue
    if (key === 'ms' || key === 'count') {
      if (Number.isSafeInteger(value) && value >= 0) safe[key] = value
    } else if (key === 'reason') {
      safe.reason = (phase === 'bind-result' || phase === 'command-outcome')
        ? (typeof value === 'string' && value === value.trim() && /^[a-z_]{1,40}$/.test(value) ? value : 'other')
        : (REJECTIONS.has(value) ? value : 'other')
    } else if (VALUES[key].has(value)) safe[key] = value.slice(0, 80)
  }
  return safe
}

// Header metadata is local only. Keep it single-line and strip credential and ID shapes.
function headerText(value) {
  if (typeof value !== 'string') return 'unknown'
  return value.slice(0, 512)
    .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, '[redacted]')
    .replace(/(?:sessionKey|signature|token|objectId|objectHandles?)\s*[:=]\s*\S+/gi, '[redacted]')
    .replace(/[A-Za-z0-9+/_=-]{32,}/g, '[redacted]')
    .replace(/https?:\/\/\S+/gi, '[redacted]')
    .replace(/[^\x20-\x7e]/g, ' ').slice(0, 80)
}

export function createDiagnostics({ now = Date.now, capacity = 200 } = {}) {
  const limit = Number.isFinite(capacity) ? Math.max(0, Math.min(200, Math.floor(capacity))) : 200
  const log = []
  return {
    record(phase, detail = {}) {
      if (!PHASES.has(phase) || limit === 0) return
      log.push({ time: new Date(now()).toISOString(), phase, detail: sanitizeDetail(phase, detail) })
      if (log.length > limit) log.splice(0, log.length - limit)
    },
    entries() { return log.map((entry) => ({ ...entry, detail: { ...entry.detail } })) },
    clear() { log.length = 0 },
    snapshot({ userAgent = globalThis.navigator?.userAgent, buildId } = {}) {
      const origin = globalThis.window?.location?.origin ?? 'unknown'
      const header = `Connection details  origin=${origin}${buildId ? `  build=${headerText(buildId)}` : ''}  userAgent=${headerText(userAgent)}`
      return [header, ...log.map(({ time, phase, detail }) =>
        `${time}  ${phase}${Object.entries(detail).map(([key, value]) => `  ${key}=${value}`).join('')}`)].join('\n')
    },
  }
}

let shared
export function getDiagnostics() {
  shared ??= createDiagnostics()
  return shared
}

export default getDiagnostics
