import test from 'node:test'
import assert from 'node:assert/strict'
import { qcDev } from './lenses/index.mjs'
import { createQcDevMeasurementCapture } from './qcDevMeasurements.mjs'

const reference = 'qc-dev:studio-sequence[ready]@desktop'
const baseline = () => ({ reference, action: 'save', expectedResult: 'saved',
  expectedOrder: ['save', 'saving', 'saved'] })
const evaluate = (measurements) => qcDev.evaluate({ schema: 'leaf.walk-evidence.v1',
  featureId: 'studio-sequence', state: 'ready', viewport: 'desktop', measurements })
function complete({ input = baseline(), events = input.expectedOrder, result = 'saved', omitResult = false } = {}) {
  const capture = createQcDevMeasurementCapture(input)
  capture.start()
  for (const event of events) capture.observeEvent(event)
  if (!omitResult) capture.observeResult(result)
  return { capture, measured: capture.finish() }
}

test('caller-reported complete sequence passes the actual frozen qcDev lens as measured', () => {
  const { measured } = complete()
  assert.deepEqual(measured, { reference, sequence: { action: 'save', expectedResult: 'saved',
    observedResult: 'saved', expectedOrder: ['save', 'saving', 'saved'],
    observedOrder: ['save', 'saving', 'saved'] } })
  const verdict = evaluate(measured)
  assert.equal(verdict.status, 'pass')
  assert.equal(verdict.results[0].provenance, 'measured')
  assert.ok(Object.isFrozen(qcDev))
  // This is a focused synthetic caller trace, not live campaign acceptance.
})

test('wrong, missing, reordered, and repeated observed events produce real findings', () => {
  for (const events of [
    ['save', 'unexpected', 'saved'], ['save', 'saved'],
    ['saved', 'saving', 'save'], ['save', 'saving', 'saving', 'saved'],
  ]) {
    const { measured } = complete({ events })
    assert.deepEqual(measured.sequence.observedOrder, events)
    const verdict = evaluate(measured)
    assert.equal(verdict.status, 'finding')
    assert.equal(verdict.results[0].finding.evidence.provenance, 'measured')
  }
  assert.equal(evaluate(complete({ result: 'stale' }).measured).status, 'finding')
})

test('duplicate events are preserved on both sides and can pass the real lens', () => {
  const input = baseline()
  input.expectedOrder = ['save', 'saving', 'saving', 'saved']
  const { measured } = complete({ input })
  assert.deepEqual(measured.sequence.observedOrder, input.expectedOrder)
  assert.equal(evaluate(measured).status, 'pass')
})

test('well-formed logically wrong declarations are assessed by the lens', () => {
  for (const expectedOrder of [['saving', 'saved'], ['saved', 'save'], ['save']]) {
    const input = { ...baseline(), expectedOrder }
    assert.equal(evaluate(complete({ input }).measured).status, 'finding')
  }
})

test('empty measured result is a finding; missing result or empty event trace is UNKNOWN', () => {
  const empty = complete({ result: '' }).measured
  assert.equal(empty.sequence.observedResult, '')
  assert.equal(evaluate(empty).status, 'finding')
  const missing = complete({ omitResult: true }).measured
  assert.equal(Object.hasOwn(missing.sequence, 'observedResult'), false)
  const verdict = evaluate(missing)
  assert.equal(verdict.status, 'unknown')
  assert.equal(Object.hasOwn(verdict.results[0], 'provenance'), false)
  assert.ok(verdict.results[0].missing.includes('measurements.sequence.observedResult'))
  assert.equal(evaluate(complete({ events: [] }).measured).status, 'unknown')
})

test('start and result observation never invent an action or event', () => {
  const capture = createQcDevMeasurementCapture(baseline())
  assert.equal(evaluate(capture.result()).status, 'unknown')
  capture.start()
  capture.observeResult('saved')
  assert.equal(evaluate(capture.result()).status, 'unknown')
  assert.equal(Object.hasOwn(capture.result().sequence, 'observedResult'), false)
  const measured = capture.finish()
  assert.deepEqual(measured.sequence.observedOrder, [])
  assert.equal(evaluate(measured).status, 'unknown')
})

test('invalid lifecycle permanently invalidates the trace without throwing', () => {
  for (const steps of [
    ['finish'], ['observeEvent'], ['observeResult'], ['start', 'start'],
    ['start', 'observeResult', 'observeResult'], ['start', 'finish', 'finish'],
    ['start', 'finish', 'observeEvent'], ['start', 'finish', 'observeResult'],
  ]) {
    const capture = createQcDevMeasurementCapture(baseline())
    for (const step of steps) assert.doesNotThrow(() => capture[step]('saved'))
    assert.equal(capture.result(), null)
    assert.equal(evaluate(capture.result()).status, 'unknown')
    capture.start()
    assert.equal(capture.result(), null)
  }
})

test('caller mutations and returned snapshots cannot rewrite the frozen baseline or trace', () => {
  const input = baseline()
  const order = input.expectedOrder
  const capture = createQcDevMeasurementCapture(input)
  input.reference = 'changed'
  input.action = 'changed'
  input.expectedResult = 'changed'
  order.reverse()
  input.expectedOrder = []
  const early = capture.result()
  early.sequence.action = 'changed'
  early.sequence.expectedOrder.push('changed')
  capture.start()
  for (const name of ['save', 'saving', 'saved']) capture.observeEvent(name)
  capture.observeResult('saved')
  const finished = capture.finish()
  finished.reference = 'changed'
  finished.sequence.observedResult = 'changed'
  finished.sequence.expectedOrder.reverse()
  finished.sequence.observedOrder.push('changed')
  assert.equal(evaluate(capture.result()).status, 'pass')
  assert.notEqual(capture.result().sequence.observedOrder, capture.result().sequence.observedOrder)
  const frozen = baseline()
  Object.freeze(frozen.expectedOrder)
  Object.freeze(frozen)
  assert.equal(evaluate(complete({ input: frozen }).measured).status, 'pass')
})

test('malformed declarations return null, without coercion, accessors, or read errors escaping', () => {
  let reads = 0
  const getter = () => { reads += 1; throw new Error('[REDACTED]') }
  const accessor = baseline()
  Object.defineProperty(accessor, 'action', { get: getter })
  const arrayAccessor = baseline()
  Object.defineProperty(arrayAccessor.expectedOrder, '0', { get: getter })
  const coercible = { toString: getter, valueOf: getter }
  const dangerousKey = baseline()
  Object.defineProperty(dangerousKey, '__proto__', { value: '[REDACTED]' })
  for (const input of [undefined, null, [], 'save', {}, accessor, arrayAccessor, dangerousKey,
    Object.create(baseline()), new Proxy({}, { ownKeys: getter }),
    { ...baseline(), action: coercible }, { ...baseline(), expectedResult: '' },
    { ...baseline(), expectedOrder: [] }, { ...baseline(), expectedOrder: ['save', ''] },
    { ...baseline(), expectedOrder: new Array(2) },
    { ...baseline(), expectedOrder: Array(257).fill('save') },
    { ...baseline(), expectedOrder: ['save', coercible] },
  ]) {
    assert.equal(createQcDevMeasurementCapture(input), null)
  }
  // Only the proxy's explicit descriptor-read failure is encountered.
  assert.equal(reads, 1)
})

test('unsafe, unbounded references and credential-shaped strings are not retained', () => {
  for (const value of ['', 'https://example.test', 'qc-dev:x[ready]@tablet',
    `qc-dev:${'x'.repeat(81)}[ready]@phone`, `${reference}\n`,
    'qc-dev:x[token=[REDACTED]]@desktop']) {
    assert.equal(createQcDevMeasurementCapture({ ...baseline(), reference: value }), null)
  }
  for (const value of ['Bearer [REDACTED]', 'Authorization: [REDACTED]',
    'password=[REDACTED]', 'cookie=[REDACTED]', 'x'.repeat(201),
    '__proto__', 'constructor', 'prototype', null, 1, {}, Symbol('unmeasured')]) {
    for (const key of ['action', 'expectedResult']) {
      assert.equal(createQcDevMeasurementCapture({ ...baseline(), [key]: value }), null)
    }
    assert.equal(createQcDevMeasurementCapture({ ...baseline(), expectedOrder: ['save', value] }), null)
    for (const method of ['observeEvent', 'observeResult']) {
      const capture = createQcDevMeasurementCapture(baseline())
      capture.start()
      assert.doesNotThrow(() => capture[method](value))
      assert.equal(capture.result(), null)
    }
  }
  const phone = { ...baseline(), reference: 'qc-dev:studio.sequence[ready]@phone' }
  assert.equal(evaluate(complete({ input: phone }).measured).status, 'pass')
})

test('event bounds invalidate rather than truncate a trace into a passing observation', () => {
  const boundary = { ...baseline(), expectedOrder: ['save', ...Array(254).fill('saving'), 'saved'] }
  assert.equal(evaluate(complete({ input: boundary }).measured).status, 'pass')
  const input = { ...baseline(), expectedOrder: Array(256).fill('save') }
  const capture = createQcDevMeasurementCapture(input)
  capture.start()
  for (let i = 0; i < 256; i += 1) capture.observeEvent('save')
  capture.observeEvent('save')
  capture.observeResult('saved')
  assert.equal(capture.finish(), null)
  assert.equal(evaluate(capture.result()).status, 'unknown')
})
