import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { redHat } from './lenses/index.mjs'
import { createRedHatMeasurementCapture } from './redHatMeasurements.mjs'

const reference = 'red-hat:studio-refusal[ready]@desktop'
const evaluate = (measurements) => redHat.evaluate({ schema: 'leaf.walk-evidence.v1',
  featureId: 'studio-refusal', state: 'ready', viewport: 'desktop', measurements })
// This caller hashes the entire relevant fixture state, never a UI label or a
// precomputed measurement. The collector receives only the resulting digest.
const hash = (state) => createHash('sha256').update(JSON.stringify(state)).digest('hex')
const state = () => ({ projects: [{ id: 'project-one', revision: 3 }], grants: ['read'], effects: [] })
function capture() { return createRedHatMeasurementCapture({ reference }) }
function complete({ observed = true, visibleText = 'Access denied.', change } = {}) {
  const protectedState = state()
  const collector = capture()
  collector.before(hash(protectedState))
  collector.observeRefusal({ observed, visibleText })
  change?.(protectedState)
  collector.after(hash(protectedState))
  return collector
}

test('caller-observed refusal and unchanged complete state pass the actual frozen lens as measured', () => {
  const result = complete().result()
  const expectedDigest = hash(state())
  assert.deepEqual(result, { reference, failure: { visibleText: 'Access denied.' },
    refusal: { observed: true, protectedBefore: expectedDigest, protectedAfter: expectedDigest } })
  const verdict = evaluate(result)
  assert.equal(verdict.status, 'pass')
  assert.equal(verdict.results[0].provenance, 'measured')
  assert.notEqual(verdict.results[0].provenance, 'inferred')
  assert.ok(Object.isFrozen(redHat))
})

test('each protected state change remains an actual lens finding', () => {
  for (const change of [(s) => { s.projects[0].revision += 1 },
    (s) => { s.grants.push('write') }, (s) => { s.effects.push('unexpected write') }]) {
    const result = complete({ change }).result()
    assert.notEqual(result.refusal.protectedBefore, result.refusal.protectedAfter)
    const verdict = evaluate(result)
    assert.equal(verdict.status, 'finding')
    assert.equal(verdict.results[0].finding.evidence.provenance, 'measured')
  }
})

test('redacted synthetic credential patterns retain exposure findings without retaining raw copy', () => {
  for (const marker of ['Bearer redacted', 'Authorization: redacted-fixture',
    'eyJredacted.redacted.redacted', 'access_token=redacted', 'refresh_token=redacted',
    'id_token=redacted', 'token=redacted', 'jwt=redacted', 'api_key=redacted',
    'api-key=redacted', 'password=redacted', 'secret=redacted',
    'set-cookie=redacted', 'cookie=redacted', 'cookie=']) {
    for (const suffix of ['', 'x'.repeat(2001)]) {
      const raw = `Failure fixture: ${marker}; ${suffix}`
      const result = complete({ visibleText: raw }).result()
      assert.equal(result.failure.visibleText, 'Authorization: redacted')
      assert.equal(JSON.stringify(result).includes(raw), false)
      assert.equal(JSON.stringify(result).includes(marker), false)
      const verdict = evaluate(result)
      assert.equal(verdict.status, 'finding')
      assert.equal(JSON.stringify(verdict).includes(raw), false)
      assert.equal(JSON.stringify(verdict).includes(marker), false)
      assert.equal(verdict.results[0].finding.evidence.evidence['failure.visibleText'], '[REDACTED]')
    }
  }
})

test('missing observations, empty failure copy, and nonapplicable false remain UNKNOWN', () => {
  assert.equal(evaluate(complete({ observed: false }).result()).status, 'unknown')
  const empty = complete({ visibleText: '' }).result()
  assert.equal(empty.failure.visibleText, '')
  assert.equal(evaluate(empty).status, 'unknown') // The actual lens requires nonempty text.
  for (const missing of ['before', 'observeRefusal', 'after']) {
    const collector = capture()
    if (missing !== 'before') collector.before(hash(state()))
    if (missing !== 'observeRefusal') collector.observeRefusal({ observed: true, visibleText: 'Denied.' })
    if (missing !== 'after') collector.after(hash(state()))
    const verdict = evaluate(collector.result())
    assert.equal(verdict.status, 'unknown')
    assert.equal(Object.hasOwn(verdict.results[0], 'provenance'), false)
  }
  for (const observation of [undefined, null, {}, { observed: true }, { visibleText: 'Denied.' },
    { observed: 'true', visibleText: 'Denied.' }, { observed: true, visibleText: 123 },
    { reachable: true, requestSucceeded: true }]) {
    const collector = capture()
    collector.before(hash(state()))
    collector.observeRefusal(observation)
    collector.after(hash(state()))
    assert.equal(evaluate(collector.result()).status, 'unknown')
  }
})

test('only exact SHA256 strings are accepted, with no coercion or state invention', () => {
  const coercible = { toString() { assert.fail('must not coerce') } }
  for (const invalid of [undefined, null, '', 'a'.repeat(63), 'a'.repeat(65),
    'z'.repeat(64), ` ${hash(state())}`, `${hash(state())}\n`, 'Protected work unchanged', state(), coercible]) {
    for (const phase of ['before', 'after']) {
      const collector = capture()
      collector.before(phase === 'before' ? invalid : hash(state()))
      collector.observeRefusal({ observed: true, visibleText: 'Denied.' })
      collector.after(phase === 'after' ? invalid : hash(state()))
      const result = collector.result()
      assert.equal(Object.hasOwn(result.refusal, phase === 'before' ? 'protectedBefore' : 'protectedAfter'), false)
      assert.equal(evaluate(result).status, 'unknown')
    }
  }
  const collector = capture()
  const upper = hash(state()).toUpperCase()
  collector.before(upper)
  collector.observeRefusal({ observed: true, visibleText: 'Denied.' })
  collector.after(upper)
  assert.equal(collector.result().refusal.protectedBefore, upper)
})

test('safe copy is bounded without truncating oversized copy into a pass', () => {
  const bounded = complete({ visibleText: 'x'.repeat(2000) }).result()
  assert.equal(bounded.failure.visibleText.length, 2000)
  assert.equal(evaluate(bounded).status, 'pass')
  const oversized = complete({ visibleText: 'x'.repeat(2001) }).result()
  assert.deepEqual(oversized.failure, {})
  assert.equal(evaluate(oversized).status, 'unknown')
})

test('invalid references and unreadable records are nonthrowing and never execute getters or actions', () => {
  const forbidden = () => assert.fail('must not execute caller code')
  for (const value of [undefined, null, '', 'https://example.test',
    'red-hat:x[ready]@tablet', `red-hat:${'x'.repeat(81)}[ready]@phone`,
    `${reference}\n`, 'red-hat:eyJredacted.redacted.redacted[ready]@phone',
    { toString: forbidden }]) {
    assert.equal(createRedHatMeasurementCapture({ reference: value }), null)
  }
  const getter = Object.defineProperty({}, 'reference', { get: forbidden })
  const unreadable = new Proxy({}, { ownKeys() { throw new Error('redacted fixture read failure') } })
  const revoked = Proxy.revocable({}, {})
  revoked.revoke()
  for (const input of [undefined, null, getter, unreadable, revoked.proxy,
    JSON.parse('{"__proto__":{},"reference":"red-hat:x[ready]@phone"}')]) {
    assert.equal(createRedHatMeasurementCapture(input), null)
  }
  for (const observation of [unreadable, revoked.proxy,
    Object.defineProperty({ observed: true }, 'visibleText', { get: forbidden }),
    JSON.parse('{"observed":true,"visibleText":"Denied.","constructor":{}}'),
    JSON.parse('{"observed":true,"visibleText":"Denied.","prototype":{}}')]) {
    const collector = capture()
    collector.before(hash(state()))
    assert.doesNotThrow(() => collector.observeRefusal(observation))
    collector.after(hash(state()))
    assert.deepEqual(collector.result().failure, {})
    assert.equal(evaluate(collector.result()).status, 'unknown')
  }
  const collector = capture()
  collector.before(hash(state()))
  collector.observeRefusal({ observed: true, visibleText: 'Denied.', action: forbidden, assertion: forbidden })
  collector.after(hash(state()))
  assert.equal(evaluate(collector.result()).status, 'pass')
})

test('phase ordering and first samples prevent rewrites of findings or missing evidence', () => {
  const collector = capture()
  collector.observeRefusal({ observed: true, visibleText: 'Denied.' })
  collector.after(hash(state()))
  assert.deepEqual(collector.result(), { reference, failure: {}, refusal: {} })
  collector.before(undefined)
  collector.before(hash(state()))
  collector.observeRefusal({ observed: true, visibleText: 'Denied.' })
  collector.after(hash(state()))
  assert.equal(evaluate(collector.result()).status, 'unknown')
  const finding = complete({ visibleText: 'Bearer redacted' })
  const original = finding.result()
  finding.before(hash(state()))
  finding.observeRefusal({ observed: true, visibleText: 'Denied.' })
  finding.after(hash(state()))
  assert.deepEqual(finding.result(), original)
  assert.equal(evaluate(finding.result()).status, 'finding')
})

test('inputs are copied and every result is detached JSON without extra caller authority', () => {
  const options = { reference }
  const collector = createRedHatMeasurementCapture(options)
  options.reference = 'rewritten'
  const observation = Object.freeze({ observed: true, visibleText: 'Denied.' })
  collector.before(hash(state()))
  collector.observeRefusal(observation)
  collector.after(hash(state()))
  const result = collector.result()
  result.reference = 'rewritten'
  result.failure.visibleText = 'Bearer redacted'
  result.refusal.protectedAfter = 'rewritten'
  assert.equal(collector.result().reference, reference)
  assert.equal(evaluate(collector.result()).status, 'pass')
  assert.deepEqual(JSON.parse(JSON.stringify(collector.result())), collector.result())
  const mutable = { observed: true, visibleText: 'Denied.' }
  const second = capture()
  second.before(hash(state()))
  second.observeRefusal(mutable)
  mutable.observed = false
  mutable.visibleText = 'Bearer redacted'
  second.after(hash(state()))
  assert.equal(evaluate(second.result()).status, 'pass')
})
