// Optional caller observations only. This capture never performs the task or
// infers its effect from reachability, activation, or a successful request.
const identifier = /^[A-Za-z][A-Za-z0-9_-]*(?:[.:][A-Za-z0-9_-]+)*$/
const dangerous = new Set(['__proto__', 'prototype', 'constructor'])
const credential = /\b(?:Bearer\s+\S+|Authorization\s*[:=]|eyJ[\w-]*\.[\w-]+\.[\w-]+|(?:access_token|refresh_token|id_token|token|jwt|api[_-]?key|password|secret|set-cookie|cookie)\s*[:=])/i
const safeText = (value) => typeof value === 'string' && value.length <= 200
  && !/[\u0000-\u001f\u007f]/.test(value) && !credential.test(value)
const safeIdentifier = (value) => safeText(value) && value.length <= 80
  && identifier.test(value) && value.split(/[.:]/).every((part) => !dangerous.has(part))

function declaration(options) {
  try {
    if (!options || typeof options !== 'object' || Array.isArray(options)) return null
    const prototype = Object.getPrototypeOf(options)
    if (prototype !== Object.prototype && prototype !== null) return null
    if (Reflect.ownKeys(options).some((key) => dangerous.has(key))) return null
    // Read own data properties only; accessors and inherited declarations are
    // not observations. Unknown option values are never read or retained.
    const reference = Object.getOwnPropertyDescriptor(options, 'reference')
    const expected = Object.getOwnPropertyDescriptor(options, 'expectedEffect')
    if (!reference || !Object.hasOwn(reference, 'value')
      || !expected || !Object.hasOwn(expected, 'value')) return null
    const value = reference.value
    if (!safeText(value)) return null
    const match = /^end-user:([^\[\]]+)\[([^\[\]]+)\]@(desktop|phone)$/.exec(value)
    if (!match || !safeIdentifier(match[1]) || !safeIdentifier(match[2])
      || !safeIdentifier(expected.value)) return null
    return { reference: value, expectedEffect: expected.value }
  } catch { return null }
}

export function createEndUserMeasurementCapture(options) {
  const declared = declaration(options)
  options = undefined
  if (!declared) return null
  let phase = 'idle'
  let manualApplyCount = 0
  let observedEffect
  const invalidate = () => { phase = 'invalid'; observedEffect = undefined }
  const result = () => {
    const task = { expectedEffect: declared.expectedEffect }
    // A partial or invalid trace cannot accidentally pass the existing lens.
    if (phase === 'finished') {
      task.manualApplyCount = manualApplyCount
      if (observedEffect !== undefined) task.observedEffect = observedEffect
    }
    return { reference: declared.reference, task }
  }
  return {
    start() {
      if (phase !== 'idle') { invalidate(); return }
      phase = 'active'
    },
    manualApply() {
      if (phase !== 'active' || manualApplyCount === Number.MAX_SAFE_INTEGER) {
        invalidate(); return
      }
      manualApplyCount += 1
      // A further apply needs a fresh read of the resulting effect.
      observedEffect = undefined
    },
    observeEffect(value) {
      if (phase !== 'active' || !safeText(value) || dangerous.has(value)) {
        invalidate(); return
      }
      observedEffect = value
    },
    finish() {
      if (phase !== 'active') invalidate()
      else phase = 'finished'
      return result()
    },
    result,
  }
}
