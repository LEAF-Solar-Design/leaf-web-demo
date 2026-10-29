// Optional observations only; the caller owns actions and semantic assertions.
const credential = /\b(?:Bearer\s+\S+|Authorization\s*[:=]|eyJ[\w-]*\.[\w-]+\.[\w-]+|(?:access_token|refresh_token|id_token|token|jwt|api[_-]?key|password|secret|set-cookie|cookie)\s*[:=])/i
const safeText = (value) => typeof value === 'string' && value.length <= 200
  && !/[\u0000-\u001f\u007f-\u009f]/.test(value) && !credential.test(value)
const safeReference = (value) => safeText(value) && value.trim().length > 0
const time = (value) => Number.isFinite(value) && value >= 0

export function createPerformanceMeasurementCapture(options = {}) {
  let reference, baseline, now
  let poisoned = false, started = false, finished = false
  let first, last, durationMs, requiredSteps = 0
  let waitSeen = false, observed = false, silent = false, progress = '', reason = ''
  try {
    reference = options.reference
    const source = options.baseline
    baseline = { reference: source.reference, maxDurationMs: source.maxDurationMs,
      maxRequiredSteps: source.maxRequiredSteps }
    now = options.now === undefined ? () => performance.now() : options.now
    if (!safeReference(reference) || !safeReference(baseline.reference)
      || !time(baseline.maxDurationMs) || !Number.isSafeInteger(baseline.maxRequiredSteps)
      || baseline.maxRequiredSteps < 0 || typeof now !== 'function') poisoned = true
  } catch { poisoned = true }

  const sample = () => {
    if (poisoned) return undefined
    try {
      const value = now()
      if (!time(value) || (last !== undefined && value < last)) poisoned = true
      else { last = value; return value }
    } catch { poisoned = true }
    return undefined
  }
  const active = () => {
    if (!started || finished) poisoned = true
    if (poisoned) return false
    sample()
    return !poisoned
  }
  return {
    start() {
      if (started || finished) poisoned = true
      if (poisoned) return
      started = true
      first = sample()
    },
    requiredStep() {
      if (!active()) return
      if (requiredSteps === Number.MAX_SAFE_INTEGER) poisoned = true
      else requiredSteps += 1
    },
    observeWait(wait) {
      if (!active()) return
      try {
        const nextObserved = wait.observed, nextProgress = wait.progress, nextReason = wait.reason
        if (typeof nextObserved !== 'boolean' || !safeText(nextProgress) || !safeText(nextReason)) {
          poisoned = true
          return
        }
        waitSeen = true
        observed ||= nextObserved
        const p = nextProgress.trim(), r = nextReason.trim()
        silent ||= nextObserved && !p && !r
        if (p) progress = p
        if (r) reason = r
      } catch { poisoned = true }
    },
    // Call only after the actual effect assertion. The caller owns merging this
    // fragment and choosing the observation bundle's shared measurements.reference.
    finish() {
      if (!active()) return
      durationMs = last - first
      finished = true
    },
    result() {
      if (poisoned || !finished || !waitSeen) return null
      return { reference, baseline: { ...baseline }, task: { durationMs, requiredSteps },
        wait: { observed, progress: silent ? '' : progress, reason: silent ? '' : reason } }
    },
  }
}
