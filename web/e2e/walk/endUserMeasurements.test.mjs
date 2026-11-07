import test from 'node:test'
import assert from 'node:assert/strict'
import { runInNewContext } from 'node:vm'
import { endUser } from './lenses/index.mjs'
import { createEndUserMeasurementCapture } from './endUserMeasurements.mjs'

const reference = 'end-user:studio-dialog[ready]@desktop'
const expectedEffect = 'settings-saved'
const create = (options = {}) => createEndUserMeasurementCapture({ reference, expectedEffect, ...options })
const evaluate = (measurements) => endUser.evaluate({ schema: 'leaf.walk-evidence.v1',
  featureId: 'studio-dialog', state: 'ready', viewport: 'desktop', measurements })

// Execute the caller's real read callback against a minimal DOM. No page
// callback is replaced by a precomputed measurement result.
function resultFixture() {
  const node = { textContent: '' }
  let unavailable = false
  const document = { getElementById(id) {
    assert.equal(id, 'task-result')
    if (unavailable) throw new Error('[REDACTED]')
    return node
  } }
  return { node, failRead() { unavailable = true },
    async read() {
      const callback = () => document.getElementById('task-result').textContent
      return runInNewContext(`(${callback.toString()})()`, { document })
    } }
}

test('an explicit result observation passes the actual frozen lens as measured evidence', async () => {
  const capture = create()
  const fixture = resultFixture()
  capture.start()
  // The caller owns the real result read. The capture receives no action,
  // assertion, page, or request callback to execute on the caller's behalf.
  fixture.node.textContent = expectedEffect
  capture.observeEffect(await fixture.read())
  const measured = capture.finish()
  assert.deepEqual(measured, { reference, task: { expectedEffect,
    observedEffect: expectedEffect, manualApplyCount: 0 } })
  const verdict = evaluate(measured)
  assert.equal(verdict.status, 'pass')
  assert.equal(verdict.results[0].provenance, 'measured')
  assert.notEqual(verdict.results[0].provenance, 'inferred')
  assert.ok(Object.isFrozen(endUser))
})

test('an empty DOM read is a finding while a failed read remains UNKNOWN', async () => {
  for (const unavailable of [false, true]) {
    const capture = create()
    const fixture = resultFixture()
    capture.start()
    if (unavailable) fixture.failRead()
    let observed
    try { observed = await fixture.read() } catch { /* Optional read supplies no effect. */ }
    capture.observeEffect(observed)
    const measured = capture.finish()
    assert.equal(evaluate(measured).status, unavailable ? 'unknown' : 'finding')
    assert.equal(Object.hasOwn(measured.task, 'observedEffect'), !unavailable)
    assert.equal(JSON.stringify(measured).includes('[REDACTED]'), false)
  }
})

test('explicit mismatches, including an observed empty string, are real findings', () => {
  for (const effect of ['', 'settings-unchanged', 'Visible result differs']) {
    const capture = create()
    capture.start()
    capture.observeEffect(effect)
    const measured = capture.finish()
    assert.equal(measured.task.observedEffect, effect)
    const verdict = evaluate(measured)
    assert.equal(verdict.status, 'finding')
    assert.equal(verdict.results[0].finding.evidence.provenance, 'measured')
    assert.equal(verdict.results[0].finding.evidence.evidence['task.observedEffect'], effect)
  }
})

test('each active manual apply is counted even when the final effect matches', () => {
  const capture = create()
  capture.start()
  capture.manualApply()
  capture.manualApply()
  capture.observeEffect(expectedEffect)
  const measured = capture.finish()
  assert.equal(measured.task.manualApplyCount, 2)
  assert.equal(evaluate(measured).status, 'finding')
})

test('missing observations and unfinished captures remain UNKNOWN', () => {
  const capture = create()
  assert.equal(evaluate(capture.result()).status, 'unknown')
  capture.start()
  assert.equal(evaluate(capture.result()).status, 'unknown')
  capture.observeEffect(expectedEffect)
  assert.equal(evaluate(capture.result()).status, 'unknown')
  const missing = create()
  missing.start()
  const measured = missing.finish()
  assert.equal(Object.hasOwn(measured.task, 'observedEffect'), false)
  assert.equal(evaluate(measured).status, 'unknown')
  assert.ok(evaluate(measured).results[0].missing.includes('measurements.task.observedEffect'))
  assert.equal(Object.hasOwn(evaluate(measured).results[0], 'provenance'), false)
})

test('reachability and request success supply no default observed effect', () => {
  const capture = create({ reachable: true, requestSucceeded: true, observedEffect: expectedEffect })
  capture.start()
  const measured = capture.finish()
  assert.deepEqual(measured.task, { expectedEffect, manualApplyCount: 0 })
  assert.equal(evaluate(measured).status, 'unknown')
})

test('a further manual apply needs a fresh observation', () => {
  const capture = create()
  capture.start()
  capture.observeEffect(expectedEffect)
  capture.manualApply()
  assert.equal(evaluate(capture.finish()).status, 'unknown')
})

test('invalid lifecycle calls cannot be repaired into a passing trace', () => {
  const traces = [
    ['manualApply', 'start', 'observeEffect', 'finish'],
    ['observeEffect', 'start', 'observeEffect', 'finish'],
    ['finish', 'start', 'observeEffect', 'finish'],
    ['start', 'start', 'observeEffect', 'finish'],
    ['start', 'observeEffect', 'finish', 'manualApply'],
    ['start', 'observeEffect', 'finish', 'observeEffect'],
    ['start', 'observeEffect', 'finish', 'start'],
    ['start', 'observeEffect', 'finish', 'finish'],
  ]
  for (const trace of traces) {
    const capture = create()
    for (const method of trace) assert.doesNotThrow(() => capture[method](expectedEffect))
    const measured = capture.result()
    assert.equal(evaluate(measured).status, 'unknown', trace.join(','))
    assert.equal(Object.hasOwn(measured.task, 'manualApplyCount'), false)
    assert.equal(Object.hasOwn(measured.task, 'observedEffect'), false)
  }
})

test('invalid declarations return null without coercing values or calling getters', () => {
  let calls = 0
  const hostile = { toString() { calls += 1; throw new Error('[REDACTED]') } }
  for (const value of [undefined, null, '', [], 7, hostile,
    { reference, get expectedEffect() { calls += 1; throw new Error('[REDACTED]') } },
    { expectedEffect, get reference() { calls += 1; throw new Error('[REDACTED]') } },
    Object.create({ reference, expectedEffect }),
    new Proxy({}, { getOwnPropertyDescriptor() { throw new Error('[REDACTED]') } }),
    new Proxy({}, { getPrototypeOf() { throw new Error('[REDACTED]') } }),
  ]) assert.equal(createEndUserMeasurementCapture(value), null)
  for (const key of ['reference', 'expectedEffect']) {
    assert.equal(create({ [key]: hostile }), null)
  }
  assert.equal(calls, 0)
})

test('references and expectations are bounded safe declarations', () => {
  for (const value of ['', 'https://example.test', 'accessibility:studio[ready]@desktop',
    'end-user:studio[ready]@tablet', 'end-user:studio[token=REDACTED]@desktop',
    'end-user:constructor[ready]@desktop', `end-user:${'x'.repeat(81)}[ready]@phone`,
    `${reference}\n`, `${reference}\nAuthorization: [REDACTED]`]) {
    assert.equal(create({ reference: value }), null)
  }
  for (const value of ['', ' ', 'arbitrary content', 'constructor', '__proto__', 'prototype',
    'task.constructor', 'x'.repeat(81), 'Bearer [REDACTED]', 'password=[REDACTED]', null]) {
    assert.equal(create({ expectedEffect: value }), null)
  }
  for (const key of ['__proto__', 'constructor', 'prototype']) {
    const options = JSON.parse(`{"${key}":"[REDACTED]"}`)
    assert.equal(create(options), null)
  }
  assert.ok(create({ reference: 'end-user:studio.dialog[ready]@phone', expectedEffect: 'task:saved' }))
})

test('unavailable, credential-shaped and unsupported observations remain absent', () => {
  let calls = 0
  const hostile = { toString() { calls += 1; throw new Error('[REDACTED]') } }
  for (const value of [undefined, null, false, 0, {}, [], hostile, 'x'.repeat(201),
    '\n', 'Bearer [REDACTED]', 'Authorization: [REDACTED]', 'password=[REDACTED]',
    'cookie=[REDACTED]', 'token=[REDACTED]', '__proto__', 'constructor', 'prototype']) {
    const capture = create()
    capture.start()
    assert.doesNotThrow(() => capture.observeEffect(value))
    // A later valid read must not conceal an incomplete observation trace.
    capture.observeEffect(expectedEffect)
    const measured = capture.finish()
    assert.equal(Object.hasOwn(measured.task, 'observedEffect'), false)
    assert.equal(evaluate(measured).status, 'unknown')
    assert.equal(JSON.stringify(measured).includes('[REDACTED]'), false)
  }
  assert.equal(calls, 0)
})

test('declarations are copied, results are detached, and unrelated callbacks are never invoked', () => {
  const forbidden = () => assert.fail('observer invoked a functional action or assertion')
  const options = { reference, expectedEffect, activate: forbidden, assert: forbidden,
    get page() { return forbidden() } }
  const capture = createEndUserMeasurementCapture(options)
  options.reference = 'changed'
  options.expectedEffect = 'changed'
  capture.start()
  capture.observeEffect(expectedEffect)
  const measured = capture.finish()
  measured.task.expectedEffect = 'changed'
  measured.task.observedEffect = 'changed'
  measured.task.manualApplyCount = 99
  measured.reference = 'changed'
  assert.deepEqual(capture.result(), { reference, task: { expectedEffect,
    observedEffect: expectedEffect, manualApplyCount: 0 } })
  assert.equal(evaluate(capture.result()).status, 'pass')
  const existing = Object.freeze({ input: Object.freeze({ touch: true }) })
  const merged = { ...existing, ...capture.result() }
  assert.equal(merged.input, existing.input)
  assert.equal(Object.hasOwn(existing, 'task'), false)
  const frozen = createEndUserMeasurementCapture(Object.freeze({ reference, expectedEffect }))
  frozen.start()
  frozen.observeEffect('')
  assert.equal(evaluate(frozen.finish()).status, 'finding')
})
