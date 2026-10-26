// The page owns its step's recorder; this key holds no shared recorder state.
export const stagingActionRecorder = Symbol('stagingActionRecorder')

export function createActionRecorder({ maxRequests = 40, maxActions = 20,
  exclude = [/^\/api\/jobs(?:[/?]|$)/, /^\/api\/builds(?:[/?]|$)/] } = {}) {
  const actions = []
  let current = null, truncated = false
  return {
    begin(name) {
      this.end()
      current = { action: String(name).slice(0, 80), requests: [], truncated: false }
      actions.push(current)
      if (actions.length > maxActions) {
        actions.splice(0, actions.length - maxActions)
        truncated = true
      }
    },
    record(entry) {
      if (!current || exclude.some(pattern => new RegExp(pattern.source, pattern.flags).test(entry.path))) return
      current.requests.push(entry)
      if (current.requests.length > maxRequests) {
        current.requests.splice(0, current.requests.length - maxRequests)
        current.truncated = true
      }
    },
    end() { current = null },
    snapshot() {
      return { actions: actions.map(({ action, requests, truncated }) => ({ action,
        requests: requests.map(({ method, path, status, duration_ms, error_code }) =>
          ({ method, path, status, duration_ms, error_code })), truncated })), truncated }
    },
  }
}

export function classifyStagingOutcome({ error = null, skipped = null, network = [], delivery } = {}) {
  const deliverySkip = skipped === 'infra_stale_delivery' && delivery?.decision === 'stale' && error === null
  const status = skipped === 'infra_stale_delivery' ? (deliverySkip ? 'skipped' : error !== null ? 'failed' : 'passed')
    : typeof skipped === 'string' && skipped.length ? 'skipped' : error !== null ? 'failed' : 'passed'
  const barrierFailure = status === 'failed' ? String(error?.message ?? error).split(/[\r\n]/)[0].slice(0, 200) : null
  const observations = network.filter(entry => {
    if (entry.status >= 200 && entry.status < 400) return false
    return entry.status >= 400 || entry.error_code != null || entry.status === null || entry.status === 'failed'
  }).slice(0, 20).map(({ method, path, status, error_code }) => ({ method, path, status, error_code }))
  return { status, barrierFailure, observations, ...(deliverySkip ? { reasonCode: 'infra_stale_delivery' } : {}) }
}
