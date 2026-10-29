import test from 'node:test'
import assert from 'node:assert/strict'
import { evaluateLenses } from './lenses/index.mjs'
import { createPerformanceMeasurementCapture } from './performanceMeasurements.mjs'

const reference = 'performance:open-drawing[ready]@desktop'
const baseline = { reference: 'task-study-v1:frozen-source', maxDurationMs: 100, maxRequiredSteps: 2 }
const noWait = { observed: false, progress: '', reason: '' }
const explained = { observed: true, progress: 'Preparing drawing', reason: 'Loading source' }
const silent = { observed: true, progress: '  ', reason: '' }
const evidenceFor = (measurements) => ({ schema: 'leaf.walk-evidence.v1',
  featureId: 'open-drawing', state: 'ready', viewport: 'desktop', measurements })
const performanceResult = (measurements) => evaluateLenses(evidenceFor(measurements))
  .find((entry) => entry.lens === 'performance')

function setup(options = {}) {
  let time = 10
  const capture = createPerformanceMeasurementCapture({ reference, baseline, now: () => time, ...options })
  return { capture, at(value) { time = value } }
}
function completed({ duration = 80, steps = 2, waits = [noWait], ...options } = {}) {
  const { capture, at } = setup(options)
  capture.start()
  for (let i = 0; i < steps; i++) capture.requiredStep()
  for (const wait of waits) capture.observeWait(wait)
  at(10 + duration)
  capture.finish()
  return capture.result()
}
function assertUnknown(capture) {
  assert.equal(capture.result(), null)
  assert.equal(performanceResult(capture.result()).status, 'unknown')
}

// Deterministic collector checks supply no real browser or campaign proof.
test('completed explicit observations feed the actual performance lens and exact fragment', () => {
  const measured = completed()
  assert.deepEqual(measured, { reference, baseline, task: { durationMs: 80, requiredSteps: 2 }, wait: noWait })
  for (const result of evaluateLenses(evidenceFor(measured))) {
    assert.equal(result.status, result.lens === 'performance' ? 'pass' : 'unknown', result.lens)
  }
  assert.equal(performanceResult(completed({ duration: 100 })).status, 'pass')
  assert.equal(performanceResult(completed({ waits: [explained] })).status, 'pass')
})

test('duration and declared action excess produce measured findings against supplied baseline', () => {
  for (const options of [{ duration: 101 }, { steps: 3 }]) {
    const measured = completed(options)
    const result = performanceResult(measured)
    assert.equal(result.status, 'finding')
    const finding = result.results[0].finding
    assert.equal(finding.assertion_id, 'performance.declared-baseline')
    assert.equal(finding.evidence.reference, reference)
    assert.equal(finding.evidence.provenance, 'measured')
    assert.equal(finding.evidence.evidence['baseline.reference'], baseline.reference)
    assert.equal(finding.evidence.evidence['task.requiredSteps'], measured.task.requiredSteps)
  }
})

test('silent wait history survives later explanation and later false observations', () => {
  for (const waits of [[silent], [silent, explained], [silent, noWait], [explained, silent, explained, noWait]]) {
    const measured = completed({ waits })
    assert.deepEqual(measured.wait, { observed: true, progress: '', reason: '' })
    assert.equal(performanceResult(measured).status, 'finding')
  }
  const measured = completed({ waits: [explained, noWait] })
  assert.deepEqual(measured.wait, explained)
  assert.equal(performanceResult(measured).status, 'pass')
})

test('safe latest nonempty evidence is trimmed and retained across empty samples', () => {
  const measured = completed({ waits: [explained,
    { observed: true, progress: '  Nearly ready  ', reason: '' }, noWait] })
  assert.deepEqual(measured.wait, { observed: true, progress: 'Nearly ready', reason: 'Loading source' })
  assert.equal(performanceResult(measured).status, 'pass')
})

test('missing baseline, wait, start or finish remains unknown', () => {
  assert.equal(performanceResult(completed({ baseline: undefined })).status, 'unknown')
  assert.equal(performanceResult(completed({ waits: [] })).status, 'unknown')
  const { capture } = setup()
  assertUnknown(capture)
  capture.start()
  capture.requiredStep()
  capture.observeWait(noWait)
  assertUnknown(capture)
  const unstarted = setup().capture
  unstarted.finish()
  assertUnknown(unstarted)
})

test('steps count only explicit declarations with no synthetic default action', () => {
  for (const steps of [0, 1, 7]) {
    assert.equal(completed({ steps }).task.requiredSteps, steps)
  }
  assert.equal(performanceResult(completed({ steps: 0, duration: 0,
    baseline: { reference: 'zero-action-study', maxDurationMs: 0, maxRequiredSteps: 0 } })).status, 'pass')
})

test('timing begins at start and result reads never sample or extend duration', () => {
  let time = 0, reads = 0
  const capture = createPerformanceMeasurementCapture({ reference, baseline,
    now: () => { reads += 1; return time } })
  assert.equal(reads, 0)
  assertUnknown(capture)
  assert.equal(reads, 0)
  time = 500
  capture.start()
  time = 510
  capture.observeWait(noWait)
  time = 550
  capture.finish()
  time = 10000
  assert.equal(capture.result().task.durationMs, 50)
  assert.equal(capture.result().task.requiredSteps, 0)
  assert.equal(reads, 3)
})

test('invalid lifecycle permanently poisons the capture', () => {
  for (const operation of [
    (c) => { c.requiredStep(); c.start(); c.observeWait(noWait); c.finish() },
    (c) => { c.observeWait(noWait); c.start(); c.observeWait(noWait); c.finish() },
    (c) => { c.finish(); c.start(); c.observeWait(noWait); c.finish() },
    (c) => { c.start(); c.start(); c.observeWait(noWait); c.finish() },
    (c) => { c.start(); c.observeWait(noWait); c.finish(); c.finish() },
    (c) => { c.start(); c.observeWait(noWait); c.finish(); c.requiredStep() },
    (c) => { c.start(); c.observeWait(noWait); c.finish(); c.observeWait(noWait) },
    (c) => { c.start(); c.observeWait(noWait); c.finish(); c.start() },
  ]) {
    const { capture } = setup()
    assert.doesNotThrow(() => operation(capture))
    assertUnknown(capture)
  }
})

test('invalid and backward clock samples fail closed at every observation boundary', () => {
  for (const bad of [NaN, Infinity, -1, '20', null, {}, 9]) {
    for (const boundary of ['requiredStep', 'observeWait', 'finish']) {
      const { capture, at } = setup()
      capture.start()
      capture.observeWait(noWait)
      at(bad)
      assert.doesNotThrow(() => capture[boundary](noWait))
      at(100)
      capture.finish()
      assertUnknown(capture)
    }
  }
  for (const bad of [NaN, Infinity, -1, undefined]) {
    assert.equal(completed({ now: () => bad }), null)
  }
  const { capture, at } = setup()
  capture.start()
  at(50)
  capture.requiredStep()
  at(40)
  capture.observeWait(noWait)
  at(80)
  capture.finish()
  assertUnknown(capture)
})

test('injected clock exceptions never escape or reset poison', () => {
  for (const failAt of [1, 2, 3, 4]) {
    let reads = 0
    const { capture } = setup({ now: () => {
      reads += 1
      if (reads === failAt) throw new Error('[REDACTED]')
      return reads
    } })
    assert.doesNotThrow(() => {
      capture.start()
      capture.requiredStep()
      capture.observeWait(noWait)
      capture.finish()
    })
    assertUnknown(capture)
  }
})

test('creation copies baseline and outputs are detached without touching caller evidence', () => {
  const supplied = { ...baseline }
  const options = { reference, baseline: supplied, now: () => 10 }
  const capture = createPerformanceMeasurementCapture(options)
  supplied.reference = 'changed-study'
  supplied.maxDurationMs = 0
  supplied.maxRequiredSteps = 0
  options.reference = 'changed-reference'
  options.now = () => NaN
  capture.start()
  const wait = { ...explained }
  capture.observeWait(wait)
  wait.progress = 'changed'
  capture.requiredStep()
  capture.finish()
  const first = capture.result()
  const snapshot = structuredClone(first)
  first.baseline.reference = 'changed-output'
  first.task.requiredSteps = 50
  first.wait.observed = false
  first.reference = 'changed-output'
  assert.deepEqual(capture.result(), snapshot)
  assert.deepEqual(snapshot.baseline, baseline)
  assert.equal(snapshot.reference, reference)
  assert.deepEqual(snapshot.wait, explained)
  const evidence = evidenceFor({ reference: 'shared-bundle', existing: { value: 1 } })
  const before = structuredClone(evidence)
  capture.result()
  assert.deepEqual(evidence, before)
})

test('unsafe, oversized and non-string text is omitted by failing closed, without coercion', () => {
  const unsafe = ['token=[REDACTED]', 'Authorization: [REDACTED]', 'Bearer [REDACTED]',
    'cookie=[REDACTED]', 'api_key=[REDACTED]', 'password=[REDACTED]',
    'x'.repeat(201), 'visible\ntext', 'visible\u007ftext', 12, null,
    { toString() { throw new Error('must not coerce') } }]
  for (const value of unsafe) {
    assert.equal(completed({ reference: value }), null)
    assert.equal(completed({ baseline: { ...baseline, reference: value } }), null)
    for (const key of ['progress', 'reason']) {
      assert.equal(completed({ waits: [{ ...explained, [key]: value }] }), null)
    }
  }
  for (const value of ['', '  ']) {
    assert.equal(completed({ reference: value }), null)
    assert.equal(completed({ baseline: { ...baseline, reference: value } }), null)
  }
  assert.equal(completed({ reference: 'x'.repeat(200) }).reference.length, 200)
  assert.equal(completed({ waits: [{ ...explained, progress: 'x'.repeat(200) }] }).wait.progress.length, 200)
})

test('malformed optional inputs and throwing accessors do not escape into functional flow', () => {
  for (const options of [undefined, null, {}, { baseline: null },
    { get reference() { throw new Error('[REDACTED]') } }]) {
    let capture
    assert.doesNotThrow(() => { capture = createPerformanceMeasurementCapture(options) })
    assert.doesNotThrow(() => { capture.start(); capture.observeWait(noWait); capture.finish() })
    assertUnknown(capture)
  }
  for (const changes of [{ maxDurationMs: -1 }, { maxDurationMs: NaN }, { maxDurationMs: '100' },
    { maxRequiredSteps: -1 }, { maxRequiredSteps: 1.5 }, { maxRequiredSteps: Number.MAX_SAFE_INTEGER + 1 }]) {
    assert.equal(completed({ baseline: { ...baseline, ...changes } }), null)
  }
  for (const wait of [undefined, null, {}, { ...noWait, observed: 'false' },
    { get observed() { throw new Error('[REDACTED]') } }]) {
    assert.doesNotThrow(() => assert.equal(completed({ waits: [wait] }), null))
  }
  assert.equal(completed({ now: null }), null)
})
