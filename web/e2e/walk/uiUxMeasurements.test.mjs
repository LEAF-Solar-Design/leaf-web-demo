import test from 'node:test'
import assert from 'node:assert/strict'
import { runInNewContext } from 'node:vm'
import { uiUxDev } from './lenses/index.mjs'
import { collectUiUxMeasurements } from './uiUxMeasurements.mjs'

const reference = 'ui-ux-dev:studio-layout[ready]@desktop'
const evidence = (measurements) => ({ schema: 'leaf.walk-evidence.v1', featureId: 'studio-layout',
  state: 'ready', viewport: 'desktop', measurements })
const evaluate = (measurements) => uiUxDev.evaluate(evidence(measurements))
const proxy = (measurements) => evaluate(measurements).results.find(
  (result) => result.assertion_id === 'ui-ux-dev.completion-proxy')

// Execute the actual browser read callback against DOM objects. All action APIs
// are forbidden; geometry/text come from the nodes, never precomputed samples.
function fixture() {
  const calls = []
  const forbidden = () => { calls.push('ACTION'); throw new Error('Unexpected action') }
  function node(x, y, width, height) {
    return { isConnected: true, visible: true, disposed: 0, innerText: 'Saved in Studio',
      box: { x, y, width, height },
      style: { display: 'block', visibility: 'visible', opacity: '1' },
      getBoundingClientRect() { calls.push('rect'); return this.box },
      contains(other) { return other === this.child }, child: {},
      async isVisible() { calls.push('visible'); return this.visible },
      async dispose() { this.disposed += 1; if (this.disposeError) throw new Error('[REDACTED]') },
      click: forbidden, scrollIntoView: forbidden, focus: forbidden, setAttribute: forbidden }
  }
  const first = node(10, 20, 80, 40), second = node(100, 20, 80, 40)
  const completion = node(10, 80, 170, 30)
  const window = { innerWidth: 800, innerHeight: 600,
    getComputedStyle(target) { calls.push('style'); return target.style } }
  const document = { elementFromPoint(x, y) {
    calls.push(['hit', x, y])
    if (document.hitError) throw new Error('[REDACTED]')
    return Object.hasOwn(document, 'hit') ? document.hit : completion
  } }
  const page = { async evaluate(callback, argument) {
    calls.push('evaluate')
    if (page.readError) throw new Error('[REDACTED]')
    return runInNewContext(`(${callback.toString()})(argument)`, { argument, window, document })
  }, click: forbidden, goto: forbidden, keyboard: { press: forbidden } }
  function locator(handle) {
    return { handle, amount: 1,
      async count() { calls.push('count'); if (this.readError) throw new Error('[REDACTED]'); return this.amount },
      async elementHandle(settings) {
        assert.ok(settings.timeout > 0 && settings.timeout <= 1500)
        calls.push('handle')
        return this.handle
      }, click: forbidden, focus: forbidden, scrollIntoViewIfNeeded: forbidden }
  }
  const controls = [{ id: 'save', locator: locator(first) }, { id: 'export', locator: locator(second) }]
  const completionLocator = locator(completion)
  const options = { page, controls, completionLocator, completionObserved: true, reference }
  return { options, page, first, second, completion, document, window, calls }
}

test('actual geometry and text feed the real inferred proxy; human satisfaction stays UNKNOWN', async () => {
  const fake = fixture()
  const measured = await collectUiUxMeasurements(fake.options)
  assert.deepEqual(measured, { reference, layout: { controls: [
    { id: 'save', x: 10, y: 20, width: 80, height: 40 },
    { id: 'export', x: 100, y: 20, width: 80, height: 40 },
  ] }, completion: { observed: true, acknowledgement: 'Saved in Studio', inPlace: true } })
  assert.deepEqual(proxy(measured), { assertion_id: 'ui-ux-dev.completion-proxy', status: 'pass', provenance: 'inferred' })
  const result = evaluate(measured)
  assert.equal(result.status, 'unknown')
  const human = result.results.find((entry) => entry.assertion_id === 'ui-ux-dev.human-reaction')
  assert.equal(human.status, 'unknown')
  assert.ok(human.missing.includes('measurements.humanFeedback.reaction'))
  assert.equal(Object.hasOwn(measured, 'humanFeedback'), false)
  assert.ok(fake.calls.some((entry) => Array.isArray(entry) && entry.join(',') === 'hit,95,95'))
  assert.equal(fake.calls.includes('ACTION'), false)
  for (const handle of [fake.first, fake.second, fake.completion]) assert.equal(handle.disposed, 1)
  assert.ok(Object.isFrozen(uiUxDev))
})

test('actual overlap, empty acknowledgement and out-of-place completion yield real findings', async () => {
  for (const setup of [
    (f) => { f.second.box.x = 89 },
    (f) => { f.completion.innerText = '' },
    (f) => { f.completion.innerText = '  \n ' },
    (f) => { f.completion.box.x = 900 },
    (f) => { f.completion.box.y = -100 },
    (f) => { f.document.hit = {} },
    (f) => { f.document.hit = null },
  ]) {
    const fake = fixture()
    setup(fake)
    const measured = await collectUiUxMeasurements(fake.options)
    assert.ok(measured)
    const result = proxy(measured)
    assert.equal(result.status, 'finding')
    assert.equal(result.finding.evidence.provenance, 'inferred')
    assert.equal(evaluate(measured).status, 'finding')
    assert.equal(Object.hasOwn(measured, 'humanFeedback'), false)
    assert.equal(fake.calls.includes('ACTION'), false)
    if (fake.completion.box.x === 900 || fake.completion.box.y === -100) {
      assert.equal(measured.completion.inPlace, false)
      assert.equal(fake.calls.some(Array.isArray), false)
    }
  }
})

test('edge-touching controls and completion descendant hits use observed geometry', async () => {
  const fake = fixture()
  fake.second.box.x = 90
  fake.document.hit = fake.completion.child
  const measured = await collectUiUxMeasurements(fake.options)
  assert.equal(measured.layout.controls[1].x, 90)
  assert.equal(measured.completion.inPlace, true)
  assert.equal(proxy(measured).status, 'pass')
})

test('explicitly unobserved completion remains UNKNOWN even with visible successful copy', async () => {
  const fake = fixture()
  fake.options.completionObserved = false
  const measured = await collectUiUxMeasurements(fake.options)
  assert.equal(measured.completion.observed, false)
  assert.equal(proxy(measured).status, 'unknown')
  assert.equal(evaluate(measured).status, 'unknown')
  assert.equal(Object.hasOwn(measured, 'humanFeedback'), false)
})

test('missing, ambiguous, hidden, disconnected or unreadable observations remain UNKNOWN', async () => {
  for (const setup of [
    (f) => { f.options.controls[0].locator.amount = 0 },
    (f) => { f.options.controls[1].locator.amount = 2 },
    (f) => { f.options.completionLocator.amount = 2 },
    (f) => { f.options.completionLocator.handle = null },
    (f) => { f.first.visible = false },
    (f) => { f.completion.visible = false },
    (f) => { f.second.isConnected = false },
    (f) => { f.completion.isConnected = false },
    (f) => { f.first.style.display = 'none' },
    (f) => { f.second.style.visibility = 'hidden' },
    (f) => { f.completion.style.opacity = '0' },
    (f) => { f.first.box.width = 0 },
    (f) => { f.second.box.height = NaN },
    (f) => { f.completion.box.x = Infinity },
    (f) => { f.window.innerWidth = 0 },
    (f) => { f.completion.innerText = undefined },
    (f) => { f.page.readError = true },
    (f) => { f.document.hitError = true },
    (f) => { f.options.controls[1].locator.readError = true },
    (f) => { f.second.getBoundingClientRect = () => { throw new Error('[REDACTED]') } },
  ]) {
    const fake = fixture()
    setup(fake)
    const measured = await collectUiUxMeasurements(fake.options)
    assert.equal(measured, null)
    assert.equal(proxy(measured).status, 'unknown')
    assert.equal(fake.calls.includes('ACTION'), false)
    for (const handle of [fake.first, fake.second, fake.completion]) assert.ok(handle.disposed <= 1)
  }
})

test('distinct safe declarations must also resolve to distinct actual connected controls', async () => {
  const fake = fixture()
  fake.options.controls[1].locator.handle = fake.first
  assert.equal(await collectUiUxMeasurements(fake.options), null)
  assert.equal(fake.first.disposed, 1)
  assert.equal(fake.second.disposed, 0)
  assert.equal(fake.completion.disposed, 1)
})

test('input validation rejects unsafe or missing declarations without DOM reads or coercion', async () => {
  for (const change of [
    { reference: '' }, { reference: 'https://example.test' },
    { reference: 'ui-ux-dev:x[ready]@tablet' },
    { reference: `${reference}\nBearer [REDACTED]` },
    { reference: `ui-ux-dev:${'x'.repeat(81)}[ready]@desktop` },
    { reference: 'ui-ux-dev:constructor[ready]@desktop' },
    { completionObserved: undefined }, { completionObserved: 'true' },
    { controls: [] }, { controls: [{ id: 'save' }] }, { controls: Array(101) },
    { controls: [{ id: 'save' }, { id: 'save' }] },
  ]) {
    const fake = fixture()
    assert.equal(await collectUiUxMeasurements({ ...fake.options, ...change }), null)
    assert.deepEqual(fake.calls, [])
  }
  for (const id of ['__proto__', 'constructor', 'prototype', 'safe.constructor', '#button', '',
    'token=[REDACTED]', 'x'.repeat(81), { toString() { assert.fail('coercion') } }]) {
    const fake = fixture()
    fake.options.controls[0].id = id
    assert.equal(await collectUiUxMeasurements(fake.options), null)
    assert.deepEqual(fake.calls, [])
  }
  assert.equal(await collectUiUxMeasurements(), null)
  assert.equal(await collectUiUxMeasurements(null), null)
})

test('declaration accessors are never invoked and inherited declarations are absent', async () => {
  let getterReads = 0
  const getter = () => { getterReads += 1; return '[REDACTED]' }
  for (const setup of [
    (f) => Object.defineProperty(f.options, 'reference', { get: getter }),
    (f) => Object.defineProperty(f.options.controls[0], 'id', { get: getter }),
    (f) => Object.defineProperty(f.options.controls, '0', { get: getter }),
    (f) => { f.options.controls[0] = Object.create(f.options.controls[0]) },
  ]) {
    const fake = fixture()
    setup(fake)
    assert.equal(await collectUiUxMeasurements(fake.options), null)
    assert.deepEqual(fake.calls, [])
  }
  for (const setup of [
    (f) => Object.defineProperty(f.options.controls[0].locator, 'count', { get: getter }),
    (f) => Object.defineProperty(f.page, 'evaluate', { get: getter }),
    (f) => Object.defineProperty(f.first, 'isVisible', { get: getter }),
  ]) {
    const fake = fixture()
    setup(fake)
    assert.equal(await collectUiUxMeasurements(fake.options), null)
  }
  assert.equal(getterReads, 0)
})

test('prototype read methods remain usable without accepting inherited declarations', async () => {
  const fake = fixture()
  for (const object of [fake.page, fake.options.controls[0].locator, fake.first]) {
    const prototype = {}
    for (const key of Object.keys(object)) {
      if (typeof object[key] === 'function') {
        prototype[key] = object[key]
        delete object[key]
      }
    }
    Object.setPrototypeOf(object, prototype)
  }
  assert.equal(proxy(await collectUiUxMeasurements(fake.options)).status, 'pass')
  assert.equal(fake.first.disposed, 1)
})

test('credential-shaped or oversized completion text is never retained in output', async () => {
  for (const text of ['Bearer [REDACTED]', 'Authorization: [REDACTED]', 'password=[REDACTED]',
    'cookie=[REDACTED]', 'x'.repeat(2001)]) {
    const fake = fixture()
    fake.completion.innerText = text
    assert.equal(await collectUiUxMeasurements(fake.options), null)
    for (const handle of [fake.first, fake.second, fake.completion]) assert.equal(handle.disposed, 1)
  }
})

test('declarations are copied before awaits and returned geometry is detached', async () => {
  const fake = fixture()
  const collecting = collectUiUxMeasurements(fake.options)
  fake.options.controls[0].id = 'rewritten'
  fake.options.controls[1].locator = null
  fake.options.controls.length = 0
  fake.options.completionObserved = false
  const measured = await collecting
  assert.equal(measured.layout.controls[0].id, 'save')
  assert.equal(measured.layout.controls[1].id, 'export')
  assert.equal(measured.completion.observed, true)
  fake.first.box.x = 500
  assert.equal(measured.layout.controls[0].x, 10)
  measured.layout.controls[0].width = 999
  assert.equal(fake.first.box.width, 80)
  assert.deepEqual(JSON.parse(JSON.stringify(measured)), measured)
})

test('frozen caller declarations are accepted and cleanup failure cannot escape', async () => {
  const fake = fixture()
  Object.freeze(fake.options.controls[0])
  Object.freeze(fake.options.controls[1])
  Object.freeze(fake.options.controls)
  Object.freeze(fake.options)
  fake.first.disposeError = true
  const measured = await collectUiUxMeasurements(fake.options)
  assert.equal(proxy(measured).status, 'pass')
  for (const handle of [fake.first, fake.second, fake.completion]) assert.equal(handle.disposed, 1)
  const failed = fixture()
  failed.page.readError = true
  failed.first.disposeError = true
  assert.equal(await collectUiUxMeasurements(failed.options), null)
  for (const handle of [failed.first, failed.second, failed.completion]) assert.equal(handle.disposed, 1)
})
