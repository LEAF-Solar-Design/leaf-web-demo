import test from 'node:test'
import assert from 'node:assert/strict'
import { composePersonaMeasurementBundle as compose } from './personaMeasurementBundle.mjs'
import { accessibility, endUser, mobileTouch, performance, opsSre, qcDev, redHat, lenses } from './lenses/index.mjs'

// Synthetic observations exercise the actual predicates, not browser evidence.
const fragment = (persona, measurements) => ({ persona, measurements: measurements === null ? null
  : { reference: `${persona}:drawing[ready]@phone`, ...measurements } })
const bundle = (...fragments) => ({ reference: 'bundle:drawing[ready]@phone', fragments })
const evidence = (measurements) => ({ schema: 'leaf.walk-evidence.v1', featureId: 'drawing',
  state: 'ready', viewport: 'phone', measurements })
const wait = { observed: true, progress: 'Loading drawing', reason: '' }
const timing = () => ({ baseline: { reference: 'task-study', maxDurationMs: 500, maxRequiredSteps: 2 },
  task: { durationMs: 100, requiredSteps: 1 }, wait: { ...wait } })
const effect = () => ({ task: { expectedEffect: 'drawing opened', observedEffect: 'drawing opened', manualApplyCount: 0 } })
const recovery = () => ({ wait: { ...wait }, recovery: { failureObserved: true,
  action: 'Retry', outcome: 'Drawing opened', outcomeVisible: true } })
const dialog = () => ({ overlay: { role: 'dialog', name: 'Open drawing', modal: true },
  focus: { openTarget: 'file', actualOpenTarget: 'file', tabRemainsInside: true, escapeClosed: true,
    returnTarget: 'open', actualReturnTarget: 'open' } })
const geometry = () => ({ input: { touch: true, coarsePointer: true }, viewport: { width: 390, height: 844 },
  control: { reachable: true, clipped: false, rect: { x: 10, y: 20, width: 48, height: 48 } } })
const sequence = () => ({ sequence: { action: 'open', expectedResult: 'ready', observedResult: 'ready',
  expectedOrder: ['open', 'ready'], observedOrder: ['open', 'ready'] } })
const status = (lens, result) => lens.evaluate(evidence(result?.measurements)).status

test('performance and end-user task observations coexist with explicit bundle provenance', () => {
  const input = bundle(fragment('performance', timing()), fragment('end-user', effect()))
  const result = compose(input)
  assert.equal(status(performance, result), 'pass')
  assert.equal(status(endUser, result), 'pass')
  assert.deepEqual(result.measurements.task, { durationMs: 100, requiredSteps: 1,
    expectedEffect: 'drawing opened', observedEffect: 'drawing opened', manualApplyCount: 0 })
  assert.equal(result.measurements.reference, input.reference)
  assert.deepEqual(result.sources, input.fragments.map((entry) => ({ persona: entry.persona,
    reference: entry.measurements.reference, missing: false })))
  assert.deepEqual(result.conflicts, [])
})

test('mobile geometry and accessibility dialog observations both reach real lenses', () => {
  const result = compose(bundle(fragment('mobile-touch', geometry()), fragment('accessibility', dialog())))
  assert.equal(status(mobileTouch, result), 'pass')
  assert.equal(status(accessibility, result), 'pass')
  assert.deepEqual(result.measurements.control.rect, geometry().control.rect)
})

test('identical scalar, object and atomic array observations coalesce structurally', () => {
  const first = { ...sequence(), sample: { a: 1, b: [null, { x: true }] } }
  const second = { ...sequence(), sample: { b: [null, { x: true }], a: 1 } }
  const result = compose(bundle(fragment('qc-dev', first), fragment('support', second)))
  assert.equal(status(qcDev, result), 'pass')
  assert.deepEqual(result.measurements.sample, first.sample)
  assert.deepEqual(result.conflicts, [])
})

test('identical waits coalesce and each conflicting wait field forces UNKNOWN', () => {
  const agreed = compose(bundle(fragment('performance', timing()), fragment('ops-sre', recovery())))
  assert.equal(status(performance, agreed), 'pass')
  assert.equal(status(opsSre, agreed), 'pass')
  for (const [field, value] of [['observed', false], ['progress', 'Ready'], ['reason', 'Provider delay']]) {
    const other = recovery()
    other.wait[field] = value
    for (const fragments of [[fragment('performance', timing()), fragment('ops-sre', other)],
      [fragment('ops-sre', other), fragment('performance', timing())]]) {
      const result = compose(bundle(...fragments))
      assert.deepEqual(result.conflicts, [`wait.${field}`])
      assert.equal(Object.hasOwn(result.measurements.wait, field), false)
      assert.equal(status(performance, result), 'unknown')
      assert.equal(status(opsSre, result), 'unknown')
      assert.deepEqual(result.measurements.task, timing().task)
    }
  }
})

test('a conflicted leaf cannot be resurrected by a later matching observation', () => {
  const result = compose(bundle(fragment('performance', timing()),
    fragment('end-user', { ...effect(), task: { ...effect().task, durationMs: 200 } }),
    fragment('support', { task: { durationMs: 100 } })))
  assert.deepEqual(result.conflicts, ['task.durationMs'])
  assert.equal(Object.hasOwn(result.measurements.task, 'durationMs'), false)
  assert.equal(status(performance, result), 'unknown')
  assert.equal(status(endUser, result), 'pass')
})

test('object/scalar prefix conflicts poison the whole subtree in either order', () => {
  for (const scalar of [null, false, 12, 'unavailable', []]) {
    for (const pair of [[fragment('performance', timing()), fragment('end-user', { task: scalar })],
      [fragment('end-user', { task: scalar }), fragment('performance', timing())]]) {
      const result = compose(bundle(...pair, fragment('support', { ...effect(), task: { ...effect().task, durationMs: 100 } })))
      assert.deepEqual(result.conflicts, ['task'])
      assert.equal(Object.hasOwn(result.measurements, 'task'), false)
      assert.equal(status(performance, result), 'unknown')
      assert.equal(status(endUser, result), 'unknown')
      assert.deepEqual(result.measurements.wait, wait)
    }
  }
  const result = compose(bundle(fragment('performance', timing()),
    fragment('end-user', { task: { durationMs: 200 } }), fragment('support', { task: false }),
    fragment('unfamiliar', { task: { requiredSteps: 1 } })))
  assert.deepEqual(result.conflicts, ['task'])
  assert.equal(Object.hasOwn(result.measurements, 'task'), false)
})

test('arrays conflict atomically rather than splicing event sequences or geometry', () => {
  const result = compose(bundle(fragment('qc-dev', sequence()),
    fragment('support', { sequence: { observedOrder: ['ready', 'open'] } }),
    fragment('end-user', { sequence: { observedOrder: ['open', 'ready'] } })))
  assert.deepEqual(result.conflicts, ['sequence.observedOrder'])
  assert.equal(Object.hasOwn(result.measurements.sequence, 'observedOrder'), false)
  assert.equal(status(qcDev, result), 'unknown')
  const rects = [{ id: 'first', x: 0 }, { id: 'second', x: 50 }]
  const geometryResult = compose(bundle(fragment('mobile-touch', { layout: { controls: rects } }),
    fragment('ui-ux-dev', { layout: { controls: [{ id: 'first', x: 1 }, rects[1]] } })))
  assert.deepEqual(geometryResult.conflicts, ['layout.controls'])
  assert.deepEqual(geometryResult.measurements.layout, {})
})

test('conflict diagnostics contain sorted paths only', () => {
  const result = compose(bundle(fragment('support', { z: { field: 'first' }, a: 1 }),
    fragment('unfamiliar', { a: 2, z: { field: 'second' } })))
  assert.deepEqual(result.conflicts, ['a', 'z.field'])
  assert.equal(JSON.stringify(result).includes('first'), false)
  assert.equal(JSON.stringify(result).includes('second'), false)
})

test('inputs and outputs are detached in both directions and repeated calls are stable', () => {
  const input = bundle(fragment('performance', timing()), fragment('qc-dev', sequence()))
  const original = structuredClone(input)
  const first = compose(input)
  const second = compose(input)
  assert.deepEqual(first, second)
  assert.deepEqual(input, original)
  first.measurements.task.durationMs = 9000
  first.measurements.sequence.observedOrder[0] = 'changed'
  first.sources[0].reference = 'changed'
  first.conflicts.push('changed')
  assert.deepEqual(input, original)
  assert.deepEqual(compose(input), second)
  input.fragments[0].measurements.baseline.maxDurationMs = 0
  input.fragments[1].measurements.sequence.observedOrder.push('changed')
  assert.equal(second.measurements.baseline.maxDurationMs, 500)
  assert.deepEqual(second.measurements.sequence.observedOrder, ['open', 'ready'])
})

test('recursive composition accepts frozen observations without mutating them', () => {
  function freeze(value) {
    if (value && typeof value === 'object') {
      Object.values(value).forEach(freeze)
      Object.freeze(value)
    }
    return value
  }
  const input = freeze(bundle(fragment('performance', timing()), fragment('end-user', effect()),
    fragment('qc-dev', sequence())))
  const result = compose(input)
  assert.equal(status(performance, result), 'pass')
  assert.equal(status(endUser, result), 'pass')
  assert.equal(status(qcDev, result), 'pass')
  assert.deepEqual(input.fragments[0].measurements.task, timing().task)
})

test('missing fragments have explicit provenance without invented measurements', () => {
  const result = compose(bundle(fragment('accessibility', null), fragment('performance', timing()), fragment('end-user', null)))
  assert.deepEqual(result.sources, [
    { persona: 'accessibility', reference: null, missing: true },
    { persona: 'performance', reference: 'performance:drawing[ready]@phone', missing: false },
    { persona: 'end-user', reference: null, missing: true },
  ])
  assert.equal(status(accessibility, result), 'unknown')
  assert.equal(status(endUser, result), 'unknown')
  assert.equal(status(performance, result), 'pass')
  assert.equal(compose(bundle(fragment('accessibility', null))), null)
  assert.equal(compose(bundle()), null)
  const sparse = compose(bundle(fragment('end-user', { task: { expectedEffect: 'opened' } })))
  assert.equal(status(endUser, sparse), 'unknown')
  assert.deepEqual(sparse.measurements.task, { expectedEffect: 'opened' })
})

test('only the exact redacted sentinel at failure.visibleText preserves the leakage finding', () => {
  const refusal = { observed: true, protectedBefore: 'unchanged', protectedAfter: 'unchanged' }
  const result = compose(bundle(fragment('red-hat', { failure: { visibleText: 'Authorization: redacted' }, refusal })))
  assert.equal(result.measurements.failure.visibleText, 'Authorization: redacted')
  assert.equal(status(redHat, result), 'finding')
  assert.equal(JSON.stringify(redHat.evaluate(evidence(result.measurements))).includes('Authorization:'), false)
  for (const measurements of [{ failure: { visibleText: 'Authorization: redacted extra' } },
    { failure: { other: 'Authorization: redacted' } }, { other: { visibleText: 'Authorization: redacted' } },
    { failure: { visibleText: ['Authorization: redacted'] } }, { reference: 'Authorization: redacted' }]) {
    assert.equal(compose(bundle(fragment('red-hat', measurements))), null)
  }
})

test('credential-shaped data is rejected without echoing values or invoking coercion', () => {
  const credentials = [ ['Bear', 'er ', 'private-value'].join(''),
    ['Authorization', ': Basic ', 'private-value'].join(''), ['pass', 'word=private-value'].join(''),
    ['access_', 'token:private-value'].join(''), ['Cookie', ':session=private-value'].join(''),
    ['eyJhbGci', 'cGF5bG9hZA', 'c2lnbmF0dXJl'].join('.'), 'AKIA' + 'X'.repeat(16), 'ghp_' + 'x'.repeat(30) ]
  for (const secret of credentials) {
    const input = bundle(fragment('red-hat', { failure: { visibleText: secret } }))
    let result
    assert.doesNotThrow(() => { result = compose(input) })
    assert.equal(result, null)
    assert.equal(JSON.stringify(result).includes(secret), false)
    assert.equal(compose({ ...input, reference: secret }), null)
    assert.equal(compose(bundle(fragment('support', { nested: [secret] }))), null)
  }
})

test('container shape, exact persona names and unique persona ownership are enforced', () => {
  for (const persona of lenses.map((entry) => entry.name)) {
    assert.ok(compose(bundle(fragment(persona, {}))))
  }
  assert.ok(compose(bundle(...lenses.map(({ name }) => fragment(name, {})))))
  for (const input of [undefined, null, false, [], {}, { reference: 'bundle' },
    { ...bundle(fragment('support', {})), extra: true },
    { reference: 'bundle', fragments: {} }, { reference: 12, fragments: [] },
    bundle({ persona: 'support' }), bundle({ persona: 'support', measurements: {} }),
    bundle({ ...fragment('support', {}), extra: true }),
    bundle(fragment('support', {}), fragment('support', {})),
    bundle(fragment('support', null), fragment('support', {})),
    bundle(...Array.from({ length: 11 }, () => fragment('support', {})))]) {
    assert.equal(compose(input), null)
  }
  for (const persona of ['endUser', 'mobileTouch', 'redHat', 'Performance', 'support ', '__proto__', 1]) {
    assert.equal(compose(bundle(fragment(persona, {}))), null)
  }
  for (const reference of ['', 'has spaces', 'bad\nreference', 'trailing\n', 'x'.repeat(201), {}, 1]) {
    assert.equal(compose({ ...bundle(fragment('support', {})), reference }), null)
    assert.equal(compose(bundle(fragment('support', { reference }))), null)
  }
})

test('non-JSON values, prototypes, accessors, dangerous keys and ambiguous paths fail closed', () => {
  let calls = 0
  const getter = Object.defineProperty({}, 'value', { enumerable: true, get() { calls++; return 1 } })
  const coercible = { toString() { calls++; return 'safe' } }
  const cycle = {}; cycle.self = cycle
  const symbolKey = { [Symbol('value')]: 1 }
  const hidden = Object.defineProperty({}, 'hidden', { value: 1 })
  const extraArray = [1]; extraArray.extra = true
  const arrayGetter = [1]
  Object.defineProperty(arrayGetter, '0', { enumerable: true, get() { calls++; return 1 } })
  for (const value of [undefined, NaN, Infinity, -Infinity, () => 1, 1n, Symbol('value'),
    new Date(), new Number(1), Object.create({ inherited: true }), Object.create(null),
    getter, coercible, cycle, symbolKey, hidden, extraArray, arrayGetter, Array(1),
    JSON.parse('{"__proto__":{"polluted":true}}'), { constructor: 1 }, { prototype: 1 }, { 'task.durationMs': 100 }]) {
    assert.equal(compose(bundle(fragment('support', { value }))), null)
  }
  const inputGetter = Object.defineProperty({}, 'reference', { enumerable: true, get() { calls++; return 'bundle' } })
  assert.equal(compose(inputGetter), null)
  const personaGetter = Object.defineProperty({ measurements: null }, 'persona', {
    enumerable: true, get() { calls++; return 'support' },
  })
  assert.equal(compose(bundle(personaGetter)), null)
  assert.equal(calls, 0)
  assert.equal({}.polluted, undefined)
})

test('text, array, depth and total object-key budgets have bounded edges', () => {
  assert.ok(compose(bundle(fragment('support', { text: 'x'.repeat(2000), samples: Array(128).fill(1) }))))
  assert.equal(compose(bundle(fragment('support', { text: 'x'.repeat(2001) }))), null)
  assert.equal(compose(bundle(fragment('support', { samples: Array(129).fill(1) }))), null)
  assert.ok(compose(bundle(fragment('support', { a: { b: { c: { d: { e: true } } } } }))))
  assert.equal(compose(bundle(fragment('support', { a: { b: { c: { d: { e: { f: true } } } } } }))), null)
  // Two root keys, two fragment keys and measurements.reference consume five.
  const fields = Object.fromEntries(Array.from({ length: 251 }, (_, i) => [`field${i}`, i]))
  assert.ok(compose(bundle(fragment('support', fields))))
  assert.equal(compose(bundle(fragment('support', { ...fields, extra: true }))), null)
})
