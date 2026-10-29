// Caller-supplied observations only. This capture never acts, retries, or infers
// a visible outcome from request success. Missing evidence stays UNKNOWN.
const credential = /\b(?:Bearer\s+\S+|Authorization\s*[:=]|eyJ[\w-]*\.[\w-]+\.[\w-]+|(?:access_token|refresh_token|id_token|token|jwt|api[_-]?key|password|secret|set-cookie|cookie)\s*[:=])/i
const safeText = (value) => typeof value === 'string' && value.length <= 2000 && !credential.test(value)
const safeReference = (value) => typeof value === 'string' && value.length <= 200
  && /^ops-sre:[A-Za-z][A-Za-z0-9_.:-]{0,79}\[[A-Za-z][A-Za-z0-9_.:-]{0,79}\]@(desktop|phone)$/.test(value)
  && !/[\r\n]/.test(value) && !credential.test(value)

// Read only own data properties: no caller getters, coercion, or extra keys.
// Proxies may reject introspection; that is unavailable evidence, not a verdict.
function record(value, keys) {
  try {
    if (!value || typeof value !== 'object') return null
    const prototype = Object.getPrototypeOf(value)
    if (prototype !== Object.prototype && prototype !== null) return null
    const descriptors = Object.getOwnPropertyDescriptors(value)
    const names = Reflect.ownKeys(descriptors)
    if (names.length !== keys.length || names.some((key) => !keys.includes(key))) return null
    const copy = {}
    for (const key of keys) {
      const descriptor = descriptors[key]
      if (!descriptor || !Object.hasOwn(descriptor, 'value')) return null
      copy[key] = descriptor.value
    }
    return copy
  } catch { return null }
}

export function createOpsSreMeasurementCapture(options) {
  const declaration = record(options, ['reference'])
  if (!declaration || !safeReference(declaration.reference)) return null
  const reference = declaration.reference
  let wait, failure, recovery
  let silentWait = false, invalid = false, finished = false
  const reject = () => { invalid = true; return null }
  const snapshot = () => {
    if (invalid || !finished || !wait || failure === undefined || !recovery) return null
    return { reference, wait: { ...wait }, recovery: { failureObserved: failure, ...recovery } }
  }
  return {
    observeWait(value) {
      if (finished) return null
      const sample = record(value, ['observed', 'progress', 'reason'])
      if (!sample || typeof sample.observed !== 'boolean'
        || !safeText(sample.progress) || !safeText(sample.reason)) return reject()
      // A later explanation cannot explain an earlier silent interval. A later
      // no-wait sample likewise cannot erase a wait that was actually observed.
      if (!silentWait) {
        silentWait = sample.observed && !sample.progress.trim() && !sample.reason.trim()
        if (!wait?.observed || sample.observed) wait = sample
      }
      return undefined
    },
    observeFailure(value) {
      if (finished) return null
      if (typeof value !== 'boolean') return reject()
      if (failure !== true) {
        failure = value
        // Explicit absence of failure makes recovery nonapplicable. These empty
        // fields are not a claim that any recovery succeeded.
        recovery = value ? undefined : { action: '', outcome: '', outcomeVisible: false }
      }
      return undefined
    },
    observeRecovery(value) {
      if (finished) return null
      const sample = record(value, ['action', 'outcome', 'outcomeVisible'])
      if (failure === undefined || !sample || !safeText(sample.action) || !safeText(sample.outcome)
        || typeof sample.outcomeVisible !== 'boolean') return reject()
      recovery = sample
      return undefined
    },
    finish() {
      finished = true
      return snapshot()
    },
    result() { return snapshot() },
  }
}
