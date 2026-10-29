import { lenses } from './lenses/index.mjs'

const personas = new Set(lenses.map(({ name }) => name))
const dangerous = new Set(['__proto__', 'prototype', 'constructor'])
const credential = /\b(?:Bearer\s+\S+|Authorization\s*[:=]|eyJ[\w-]*\.[\w-]+\.[\w-]+|(?:access_token|refresh_token|id_token|token|jwt|api[_-]?key|password|secret|set-cookie|cookie)\s*[:=]|(?:AKIA|ASIA)[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]+)|-----BEGIN [A-Z ]*PRIVATE KEY-----/i
const safeReference = (value) => typeof value === 'string' && value.length <= 200
  && /^[A-Za-z][A-Za-z0-9_.:\[\]@-]*$/.test(value) && !/\s/.test(value) && !credential.test(value)
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)

// Inspect descriptors before reading values: optional evidence must not execute getters.
// The budget covers the entire input, including fragments and their provenance.
function snapshot(input) {
  let keys = 0
  const ancestors = new Set()
  function copy(value, path, depth) {
    if (depth > 8) throw new TypeError('Invalid measurement data')
    if (value === null || typeof value === 'boolean') return value
    if (typeof value === 'number' && Number.isFinite(value)) return value
    if (typeof value === 'string') {
      const sentinel = value === 'Authorization: redacted' && path.length === 5
        && path[0] === 'fragments' && path[2] === 'measurements'
        && path[3] === 'failure' && path[4] === 'visibleText'
      if (value.length <= 2000 && (!credential.test(value) || sentinel)) return value
      throw new TypeError('Invalid measurement data')
    }
    if (typeof value !== 'object' || ancestors.has(value)) throw new TypeError('Invalid measurement data')
    const array = Array.isArray(value)
    if (Object.getPrototypeOf(value) !== (array ? Array.prototype : Object.prototype)) {
      throw new TypeError('Invalid measurement data')
    }
    const descriptors = Object.getOwnPropertyDescriptors(value)
    const names = Reflect.ownKeys(descriptors)
    const result = array ? [] : {}
    if (array && (descriptors.length.value > 128 || names.length !== descriptors.length.value + 1)) {
      throw new TypeError('Invalid measurement data')
    }
    ancestors.add(value)
    for (const key of names) {
      if (array && key === 'length') continue
      const descriptor = descriptors[key]
      if (typeof key !== 'string' || !Object.hasOwn(descriptor, 'value') || !descriptor.enumerable
        || (array ? !/^(0|[1-9][0-9]*)$/.test(key) || Number(key) >= descriptors.length.value
          : dangerous.has(key) || !/^[A-Za-z_][A-Za-z0-9_-]{0,79}$/.test(key) || /\s/.test(key) || credential.test(key))) {
        throw new TypeError('Invalid measurement data')
      }
      if (!array && ++keys > 256) throw new TypeError('Invalid measurement data')
      result[key] = copy(descriptor.value, [...path, key], depth + 1)
    }
    ancestors.delete(value)
    return result
  }
  return copy(input, [], 0)
}

function equal(a, b) {
  if (a === b) return true
  if (a === null || b === null || typeof a !== 'object' || typeof b !== 'object'
    || Array.isArray(a) !== Array.isArray(b)) return false
  const keys = Object.keys(a)
  return keys.length === Object.keys(b).length
    && keys.every((key) => Object.hasOwn(b, key) && equal(a[key], b[key]))
}

// Pure composition only. The caller supplies a source-bound bundle reference;
// neither that identity nor any observation or verdict is inferred here.
export function composePersonaMeasurementBundle(input) {
  try {
    const data = snapshot(input)
    if (!object(data) || Object.keys(data).length !== 2 || !safeReference(data.reference)
      || !Array.isArray(data.fragments) || data.fragments.length > 10) return null
    const seen = new Set()
    const sources = []
    const measurements = { reference: data.reference }
    const conflicts = new Set()
    let available = false
    function merge(target, incoming, prefix = '') {
      for (const [key, value] of Object.entries(incoming)) {
        if (!prefix && key === 'reference') continue
        const path = prefix ? `${prefix}.${key}` : key
        if (conflicts.has(path)) continue
        if (!Object.hasOwn(target, key)) target[key] = value
        else if (object(target[key]) && object(value)) merge(target[key], value, path)
        else if (!equal(target[key], value)) {
          delete target[key]
          // A prefix conflict supersedes any already-conflicted descendants.
          for (const prior of conflicts) if (prior.startsWith(`${path}.`)) conflicts.delete(prior)
          conflicts.add(path)
        }
      }
    }
    for (const fragment of data.fragments) {
      if (!object(fragment) || Object.keys(fragment).length !== 2
        || !Object.hasOwn(fragment, 'measurements') || !personas.has(fragment.persona)
        || seen.has(fragment.persona)) return null
      seen.add(fragment.persona)
      if (fragment.measurements === null) {
        sources.push({ persona: fragment.persona, reference: null, missing: true })
        continue
      }
      if (!object(fragment.measurements) || !safeReference(fragment.measurements.reference)) return null
      available = true
      sources.push({ persona: fragment.persona, reference: fragment.measurements.reference, missing: false })
      merge(measurements, fragment.measurements)
    }
    return available ? { measurements, sources, conflicts: [...conflicts].sort() } : null
  } catch {
    // Invalid data, including credential-shaped text, never enters diagnostics.
    return null
  }
}
