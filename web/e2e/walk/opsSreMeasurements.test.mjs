import test from 'node:test'
import assert from 'node:assert/strict'
import { opsSre } from './lenses/index.mjs'
import { createOpsSreMeasurementCapture } from './opsSreMeasurements.mjs'

const reference = 'ops-sre:studio-dialog[ready]@desktop'
const create = () => createOpsSreMeasurementCapture({ reference })
const evaluate = (measurements) => opsSre.evaluate({ schema: 'leaf.walk-evidence.v1',
  featureId: 'studio-dialog', state: 'ready', viewport: 'desktop', measurements })
const wait = { observed: true, progress: 'Loading Studio', reason: '' }
const recovered = { action: 'Retry', outcome: 'Studio content visible', outcomeVisible: true }
function complete(waitSample = wait, failure = true, recovery = recovered) {
  const capture = create()
  capture.observeWait(waitSample)
  capture.observeFailure(failure)
  if (recovery !== undefined) capture.observeRecovery(recovery)
  return capture.finish()
}

test('explicit wait and visible recovery feed the actual frozen lens as measured evidence', () => {
  const result = complete()
  assert.deepEqual(result, { reference, wait, recovery: { failureObserved: true, ...recovered } })
  const verdict = evaluate(result)
  assert.equal(verdict.status, 'pass')
  assert.equal(verdict.results[0].provenance, 'measured')
  assert.ok(Object.isFrozen(opsSre))
  assert.equal(JSON.stringify(verdict).includes('inferred'), false)
})

test('explicit empty strings and false visibility are measured findings, not missing reads', () => {
  for (const sample of [
    { action: '', outcome: 'Visible', outcomeVisible: true },
    { action: 'Retry', outcome: '', outcomeVisible: true },
    { action: 'Retry', outcome: 'Visible', outcomeVisible: false },
    { action: '', outcome: '', outcomeVisible: false },
  ]) {
    const result = complete(wait, true, sample)
    assert.deepEqual(result.recovery, { failureObserved: true, ...sample })
    const verdict = evaluate(result)
    assert.equal(verdict.status, 'finding')
    assert.equal(verdict.results[0].finding.evidence.provenance, 'measured')
  }
  for (const progress of ['', '   ']) {
    assert.equal(evaluate(complete({ observed: true, progress, reason: '' })).status, 'finding')
  }
  assert.equal(evaluate(complete({ observed: true, progress: '', reason: 'Waiting for content' })).status, 'pass')
})

test('explicit no-failure allows empty recovery; no wait and no failure remains nonapplicable UNKNOWN', () => {
  const capture = create()
  capture.observeWait(wait)
  capture.observeFailure(false)
  const result = capture.finish()
  assert.deepEqual(result.recovery, { failureObserved: false, action: '', outcome: '', outcomeVisible: false })
  assert.equal(evaluate(result).status, 'pass')
  const absent = complete({ observed: false, progress: '', reason: '' }, false,
    { action: '', outcome: '', outcomeVisible: false })
  const verdict = evaluate(absent)
  assert.equal(verdict.status, 'unknown')
  assert.equal(verdict.results[0].reason, 'Required observation modality is absent')
  assert.equal(evaluate(complete({ observed: false, progress: '', reason: '' })).status, 'pass')
})

test('missing phases and recovery after a failure stay UNKNOWN, without invented success', () => {
  for (const setup of [() => {}, (c) => c.observeWait(wait), (c) => c.observeFailure(false),
    (c) => { c.observeWait(wait); c.observeFailure(true) },
    (c) => { c.observeWait(wait); c.observeFailure(false); c.observeFailure(true) }]) {
    const capture = create()
    setup(capture)
    assert.equal(capture.result(), null)
    assert.equal(capture.finish(), null)
    assert.equal(evaluate(capture.result()).status, 'unknown')
  }
})

test('observed wait and failure are monotonic, including an unexplained earlier wait', () => {
  const capture = create()
  capture.observeWait({ observed: true, progress: '', reason: '' })
  capture.observeWait(wait)
  capture.observeWait({ observed: false, progress: 'Done', reason: '' })
  capture.observeFailure(true)
  capture.observeRecovery(recovered)
  capture.observeFailure(false)
  capture.observeFailure(true)
  const result = capture.finish()
  assert.deepEqual(result.wait, { observed: true, progress: '', reason: '' })
  assert.equal(result.recovery.failureObserved, true)
  assert.equal(evaluate(result).status, 'finding')
  const explained = create()
  explained.observeWait(wait)
  explained.observeWait({ observed: false, progress: '', reason: '' })
  explained.observeFailure(false)
  assert.deepEqual(explained.finish().wait, wait)
  const whitespace = create()
  whitespace.observeWait({ observed: true, progress: '  ', reason: '\t' })
  whitespace.observeWait(wait)
  whitespace.observeFailure(false)
  assert.deepEqual(whitespace.finish().wait, { observed: true, progress: '  ', reason: '\t' })
  assert.equal(evaluate(whitespace.result()).status, 'finding')
})

test('recovery is the supplied visible outcome, never an HTTP status or reachability inference', () => {
  const capture = create()
  capture.observeWait(wait)
  capture.observeFailure(true)
  capture.observeRecovery({ action: 'Retry', outcome: 200, outcomeVisible: true })
  assert.equal(capture.finish(), null)
  assert.equal(evaluate(capture.result()).status, 'unknown')
  assert.equal(evaluate(complete(wait, true, { action: 'Retry', outcome: 'Request accepted', outcomeVisible: false })).status,
    'finding')
})

test('invalid lifecycle is harmless and a finished snapshot cannot be rewritten', () => {
  const early = create()
  assert.equal(early.observeRecovery(recovered), null)
  early.observeWait(wait)
  early.observeFailure(true)
  early.observeRecovery(recovered)
  assert.equal(early.finish(), null)
  const capture = create()
  capture.observeWait(wait)
  capture.observeFailure(false)
  const before = capture.finish()
  assert.equal(capture.observeWait({ observed: true, progress: '', reason: '' }), null)
  assert.equal(capture.observeFailure(true), null)
  assert.equal(capture.observeRecovery(recovered), null)
  assert.deepEqual(capture.finish(), before)
  assert.deepEqual(capture.result(), before)
})

test('declarations and observations are copied and all returned records are detached', () => {
  const options = { reference }, sample = { ...wait }, recovery = { ...recovered }
  const capture = createOpsSreMeasurementCapture(options)
  options.reference = 'changed'
  capture.observeWait(sample)
  capture.observeFailure(true)
  capture.observeRecovery(recovery)
  sample.progress = 'changed'
  recovery.outcome = 'changed'
  const result = capture.finish()
  assert.deepEqual(result, complete())
  result.reference = 'changed'
  result.wait.progress = ''
  result.recovery.outcomeVisible = false
  assert.deepEqual(capture.result(), complete())
  assert.deepEqual(JSON.parse(JSON.stringify(capture.result())), capture.result())
  assert.doesNotThrow(() => complete(Object.freeze({ ...wait }), true, Object.freeze({ ...recovered })))
})

test('safe bounded references reject credentials, wrong modalities, dangerous keys and getters', () => {
  for (const value of [undefined, null, '', 1, {}, 'https://example.test',
    'ops-sre:studio[ready]@tablet', 'ops-sre:studio[token=[REDACTED]]@desktop',
    `ops-sre:${'x'.repeat(81)}[ready]@desktop`, `${reference}\n`, `${reference}\nBearer [REDACTED]`]) {
    assert.equal(createOpsSreMeasurementCapture({ reference: value }), null)
  }
  assert.equal(createOpsSreMeasurementCapture(), null)
  assert.ok(createOpsSreMeasurementCapture({ reference: 'ops-sre:studio.dialog[ready]@phone' }))
  let reads = 0
  const getter = Object.defineProperty({}, 'reference', { get() { reads++; throw new Error('[REDACTED]') } })
  for (const options of [getter, Object.create({ reference }),
    JSON.parse('{"reference":"ops-sre:studio[ready]@desktop","__proto__":{}}'),
    { reference, constructor: 'unexpected' }, { reference, [Symbol('extra')]: true },
    new Proxy({}, { ownKeys() { throw new Error('[REDACTED]') } })]) {
    assert.equal(createOpsSreMeasurementCapture(options), null)
  }
  assert.equal(reads, 0)
})

test('unreadable, credential-shaped, oversized and mistyped samples remain UNKNOWN without coercion', () => {
  let reads = 0
  const unreadable = Object.defineProperty({ progress: '', reason: '' }, 'observed',
    { enumerable: true, get() { reads++; throw new Error('[REDACTED]') } })
  const coercible = { toString() { reads++; throw new Error('[REDACTED]') } }
  for (const sample of [null, unreadable, { ...wait, observed: 1 }, { ...wait, progress: coercible },
    { ...wait, reason: undefined }, { ...wait, progress: 'x'.repeat(2001) },
    { ...wait, reason: 'Authorization: [REDACTED]' }, { ...wait, extra: 'unexpected' },
    JSON.parse('{"observed":true,"progress":"","reason":"","__proto__":{}}'),
    new Proxy({}, { getPrototypeOf() { throw new Error('[REDACTED]') } })]) {
    const capture = create()
    assert.doesNotThrow(() => capture.observeWait(sample))
    capture.observeWait(wait)
    capture.observeFailure(false)
    assert.equal(capture.finish(), null)
    assert.equal(evaluate(capture.result()).status, 'unknown')
  }
  for (const sample of [{ ...recovered, action: 'Bearer [REDACTED]' },
    { ...recovered, outcome: 'cookie=[REDACTED]' }, { ...recovered, outcomeVisible: 'true' },
    { ...recovered, outcome: 'x'.repeat(2001) }]) {
    const capture = create()
    capture.observeWait(wait)
    capture.observeFailure(true)
    capture.observeRecovery(sample)
    assert.equal(capture.finish(), null)
  }
  for (const value of [undefined, null, 0, 'false', coercible]) {
    const capture = create()
    capture.observeWait(wait)
    assert.doesNotThrow(() => capture.observeFailure(value))
    assert.equal(capture.finish(), null)
  }
  assert.equal(reads, 0)
})
