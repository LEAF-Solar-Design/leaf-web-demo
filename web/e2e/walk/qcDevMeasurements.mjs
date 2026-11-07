// Optional caller-reported semantic observations; this capture performs no action
// or assertion. It does not establish live acceptance or verify a caller's report.
const maxEvents = 256
const credential = /\b(?:Bearer\s+\S+|Authorization\s*[:=]|eyJ[\w-]*\.[\w-]+\.[\w-]+|(?:access_token|refresh_token|id_token|token|jwt|api[_-]?key|password|secret|set-cookie|cookie)\s*[:=])/i
const dangerous = new Set(['__proto__', 'prototype', 'constructor'])
const safeText = (value, empty = false) => typeof value === 'string' && value.length <= 200
  && (empty || value.trim().length > 0) && !credential.test(value) && !dangerous.has(value)
const safeReference = (value) => safeText(value)
  && !/\s/.test(value)
  && /^qc-dev:[A-Za-z][A-Za-z0-9_.:-]{0,79}\[[A-Za-z][A-Za-z0-9_.:-]{0,79}\]@(desktop|phone)$/.test(value)

// Read only own data properties. Accessors and unsupported shapes are never
// coerced or invoked. Proxy/descriptor read failures are caught at the boundary.
function copyOrder(value) {
  if (!Array.isArray(value)) return null
  const descriptors = Object.getOwnPropertyDescriptors(value)
  const length = descriptors.length?.value
  if (!Number.isInteger(length) || length < 1 || length > maxEvents
    || Reflect.ownKeys(descriptors).length !== length + 1) return null
  const result = []
  for (let i = 0; i < length; i += 1) {
    const descriptor = descriptors[i]
    if (!descriptor || !Object.hasOwn(descriptor, 'value') || !safeText(descriptor.value)) return null
    result.push(descriptor.value)
  }
  return Object.freeze(result)
}

function copyBaseline(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return null
  const prototype = Object.getPrototypeOf(input)
  if (prototype !== Object.prototype && prototype !== null) return null
  const descriptors = Object.getOwnPropertyDescriptors(input)
  const keys = ['reference', 'action', 'expectedResult', 'expectedOrder']
  if (Reflect.ownKeys(descriptors).some((key) => !keys.includes(key))) return null
  const values = {}
  for (const key of keys) {
    const descriptor = descriptors[key]
    if (!descriptor || !Object.hasOwn(descriptor, 'value')) return null
    values[key] = descriptor.value
  }
  if (!safeReference(values.reference) || !safeText(values.action) || !safeText(values.expectedResult)) return null
  const expectedOrder = copyOrder(values.expectedOrder)
  return expectedOrder ? Object.freeze({ reference: values.reference, action: values.action,
    expectedResult: values.expectedResult, expectedOrder }) : null
}

// Invalid baselines return null. The baseline is copied/frozen at creation,
// before start. A well-formed but logically wrong baseline belongs to the lens.
// Any invalid lifecycle/observation poisons this capture (result() becomes null).
// Until finish, observed fields are withheld. A missing result remains absent;
// observeResult('') records a measured empty result. Only observeEvent appends
// events, preserving every duplicate in caller order, including result events.
export function createQcDevMeasurementCapture(input) {
  let baseline
  try { baseline = copyBaseline(input) } catch { return null }
  if (!baseline) return null
  let phase = 'created'
  let observedOrder = []
  let observedResult
  let hasResult = false
  const invalidate = () => {
    phase = 'invalid'
    baseline = null
    observedOrder = []
    observedResult = undefined
    hasResult = false
  }
  const result = () => {
    if (phase === 'invalid') return null
    const sequence = { action: baseline.action, expectedResult: baseline.expectedResult,
      expectedOrder: [...baseline.expectedOrder] }
    if (phase === 'finished') {
      sequence.observedOrder = [...observedOrder]
      if (hasResult) sequence.observedResult = observedResult
    }
    return { reference: baseline.reference, sequence }
  }
  return {
    start() {
      if (phase !== 'created') { invalidate(); return }
      phase = 'started'
    },
    observeEvent(name) {
      if (phase !== 'started' || !safeText(name) || observedOrder.length >= maxEvents) {
        invalidate(); return
      }
      observedOrder.push(name)
    },
    observeResult(name) {
      if (phase !== 'started' || hasResult || !safeText(name, true)) { invalidate(); return }
      observedResult = name
      hasResult = true
    },
    finish() {
      if (phase !== 'started') { invalidate(); return null }
      phase = 'finished'
      return result()
    },
    result,
  }
}
