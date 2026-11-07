// Optional observations only. The caller owns the refusal and complete-state hash.
// Keep these patterns equivalent to the frozen redHat lens's credential checks.
const credentialPatterns = [
  /\bBearer\s+[^\s"'<>;,]+/i,
  /\bAuthorization\s*[:=]\s*[^\r\n]+/i,
  /\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/,
  /\b(?:access_token|refresh_token|id_token|token|jwt|api[_-]?key|password|secret)\s*[:=]\s*[^\s,;]+/i,
  /\b(?:set-cookie|cookie)\s*[:=][^\r\n]*/i,
]
const sensitive = (value) => credentialPatterns.some((pattern) => pattern.test(value))
const digest = (value) => typeof value === 'string' && value.length === 64 && /^[A-Fa-f0-9]{64}$/.test(value)
const safeReference = (value) => typeof value === 'string' && value.length <= 200
  && !/[\r\n]/.test(value)
  && /^red-hat:[A-Za-z][A-Za-z0-9_.:-]{0,79}\[[A-Za-z][A-Za-z0-9_.:-]{0,79}\]@(desktop|phone)$/.test(value)
  && !sensitive(value)

// Read data properties only: optional evidence must not invoke caller getters,
// coercion, or actions. Discard unreadable records without retaining exceptions.
function dataProperties(value) {
  try {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null
    const prototype = Object.getPrototypeOf(value)
    if (prototype !== Object.prototype && prototype !== null) return null
    const properties = Object.getOwnPropertyDescriptors(value)
    if (Reflect.ownKeys(properties).some((key) => typeof key !== 'string'
      || ['__proto__', 'prototype', 'constructor'].includes(key)
      || !Object.hasOwn(properties[key], 'value'))) return null
    return properties
  } catch { return null }
}

export function createRedHatMeasurementCapture(options) {
  const reference = dataProperties(options)?.reference?.value
  if (!safeReference(reference)) return null
  const measured = { reference, failure: {}, refusal: {} }
  let phase = 0
  return {
    before(protectedStateDigest) {
      if (phase !== 0) return
      phase = 1
      if (digest(protectedStateDigest)) measured.refusal.protectedBefore = protectedStateDigest
    },
    observeRefusal(observation) {
      if (phase !== 1) return
      phase = 2
      const properties = dataProperties(observation)
      const observed = properties?.observed?.value
      const visibleText = properties?.visibleText?.value
      if (typeof observed === 'boolean') measured.refusal.observed = observed
      if (typeof visibleText === 'string') {
        // Preserve the exposure finding, but never store any part of raw copy.
        if (sensitive(visibleText)) measured.failure.visibleText = 'Authorization: redacted'
        else if (visibleText.length <= 2000) measured.failure.visibleText = visibleText
      }
    },
    after(protectedStateDigest) {
      if (phase !== 2) return
      phase = 3
      if (digest(protectedStateDigest)) measured.refusal.protectedAfter = protectedStateDigest
    },
    result() {
      return { reference, failure: { ...measured.failure }, refusal: { ...measured.refusal } }
    },
  }
}
