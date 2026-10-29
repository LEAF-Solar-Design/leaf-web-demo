import test from 'node:test'
import assert from 'node:assert/strict'
import { runInNewContext } from 'node:vm'
import { support } from './lenses/index.mjs'
import { collectSupportMeasurements } from './supportMeasurements.mjs'

const reference = 'support:studio-error[failed]@desktop'
const evaluate = (measurements) => support.evaluate({ schema: 'leaf.walk-evidence.v1',
  featureId: 'studio-error', state: 'failed', viewport: 'desktop', measurements })

// Execute the actual browser callbacks against a minimal DOM, never return
// precomputed measurements from page.evaluate. All action methods are forbidden.
function fixture() {
  const calls = []
  const forbidden = () => { calls.push('FORBIDDEN ACTION'); throw new Error('Action forbidden') }
  const actions = { click: forbidden, focus: forbidden, scrollIntoView: forbidden,
    scrollIntoViewIfNeeded: forbidden, press: forbidden, fill: forbidden,
    setAttribute: forbidden, dispatchEvent: forbidden, evaluate: forbidden }
  function node(text) {
    return { ...actions, innerText: text, isConnected: true, visible: true, disposed: 0,
      box: { x: 20, y: 30, width: 100, height: 40 }, child: {},
      contains(other) { return other === this.child },
      getBoundingClientRect() { if (this.boxError) throw new Error('[REDACTED]'); return this.box },
      async isVisible() { if (this.visibleError) throw new Error('[REDACTED]'); return this.visible },
      async dispose() { this.disposed += 1; if (this.disposeError) throw new Error('[REDACTED]') } }
  }
  const error = node('Save failed'), explanation = node('The connection was interrupted'), recovery = node('Retry')
  function locator(handle, label) {
    return { ...actions, handle, amount: 1, visible: true, snapshot: '- button "Retry"',
      description: 'Invented name',
      async count() { calls.push(`${label}.count`); if (this.countError) throw new Error('[REDACTED]'); return this.amount },
      async isVisible() { return this.visible },
      async elementHandle(settings) {
        assert.ok(settings.timeout > 0 && settings.timeout <= 1500)
        if (this.handleError) throw new Error('[REDACTED]')
        return this.handle
      },
      async ariaSnapshot(settings) {
        assert.ok(settings.timeout > 0 && settings.timeout <= 1500)
        if (this.snapshotError) throw new Error('[REDACTED]')
        this.onSnapshot?.()
        return this.snapshot
      } }
  }
  const document = { hit: recovery, elementFromPoint(x, y) {
    calls.push(['hit', x, y])
    if (this.hitError) throw new Error('[REDACTED]')
    return this.hit
  } }
  const window = { innerWidth: 1000, innerHeight: 800, scrollTo: forbidden }
  const page = { ...actions, keyboard: { press: forbidden }, mouse: { click: forbidden },
    async evaluate(callback, argument) {
      calls.push('evaluate')
      if (this.readError) throw new Error('[REDACTED]')
      return runInNewContext(`(${callback.toString()})(argument)`, { argument, document, window })
    } }
  const options = { page, errorLocator: locator(error, 'error'),
    explanationLocator: locator(explanation, 'explanation'), recoveryLocator: locator(recovery, 'recovery'),
    reference, errorObserved: true }
  return { options, page, error, explanation, recovery, document, window, calls, node }
}

async function collect(fake) {
  const measured = await collectSupportMeasurements(fake.options)
  assert.equal(fake.calls.includes('FORBIDDEN ACTION'), false)
  return measured
}

test('actual observed evidence is consumed by the frozen support lens as measured', async () => {
  const fake = fixture()
  const result = await collect(fake)
  assert.deepEqual(result, { reference, error: { observed: true, message: 'Save failed',
    whatHappened: 'The connection was interrupted', recoveryName: 'Retry', recoveryReachable: true } })
  assert.equal(Object.isFrozen(support), true)
  assert.equal(evaluate(result).status, 'pass')
  assert.equal(evaluate(result).results[0].provenance, 'measured')
  assert.ok(fake.calls.some((call) => Array.isArray(call) && call[1] === 70 && call[2] === 50))
  for (const handle of [fake.error, fake.explanation, fake.recovery]) assert.equal(handle.disposed, 1)
})

test('blank observed text, blank accessible name and measured obstruction are findings', async () => {
  for (const change of [
    (f) => { f.error.innerText = '' },
    (f) => { f.explanation.innerText = '   ' },
    (f) => { f.options.recoveryLocator.snapshot = '- button ""' },
    (f) => { f.options.recoveryLocator.snapshot = '- button' },
    (f) => { f.document.hit = {} },
    (f) => { f.document.hit = null },
    (f) => { f.recovery.box.x = 1000 },
    (f) => { f.recovery.box.width = 0 },
  ]) {
    const fake = fixture()
    change(fake)
    const result = evaluate(await collect(fake))
    assert.equal(result.status, 'finding')
    assert.equal(result.results[0].finding.evidence.provenance, 'measured')
  }
})

test('descendant center hits measure reachable without performing recovery', async () => {
  const fake = fixture()
  fake.document.hit = fake.recovery.child
  assert.equal(evaluate(await collect(fake)).status, 'pass')
})

test('explicit false is nonapplicable and does not read DOM or invent empty measurements', async () => {
  const fake = fixture()
  fake.options.errorObserved = false
  const result = await collect(fake)
  assert.deepEqual(result, { reference, error: { observed: false } })
  assert.equal(evaluate(result).status, 'unknown')
  assert.equal(evaluate(result).results[0].provenance, undefined)
  assert.deepEqual(fake.calls, [])
  assert.equal(fake.recovery.disposed, 0)
})

test('missing, ambiguous, hidden and unreadable observations stay UNKNOWN', async () => {
  for (const field of ['errorLocator', 'explanationLocator', 'recoveryLocator']) {
    for (const change of [
      (l) => { l.amount = 0 }, (l) => { l.amount = 2 },
      (l) => { l.visible = false }, (l) => { l.handle.visible = false },
      (l) => { l.handle.isConnected = false }, (l) => { l.countError = true },
      (l) => { l.handleError = true }, (l) => { l.handle.visibleError = true },
    ]) {
      const fake = fixture()
      change(fake.options[field])
      assert.equal(evaluate(await collect(fake)).status, 'unknown', field)
    }
    const fake = fixture()
    delete fake.options[field]
    assert.equal(evaluate(await collect(fake)).status, 'unknown', field)
  }
})

test('read failures leave independent observations intact and dispose owned handles', async () => {
  for (const change of [
    (f) => { f.page.readError = true },
    (f) => { f.options.recoveryLocator.snapshotError = true },
    (f) => { f.document.hitError = true },
    (f) => { f.recovery.boxError = true },
    (f) => { f.recovery.box.width = NaN },
    (f) => { f.window.innerWidth = 0 },
    (f) => { Object.defineProperty(f.error, 'innerText', { get() { throw new Error('[REDACTED]') } }) },
  ]) {
    const fake = fixture()
    change(fake)
    const measured = await collect(fake)
    assert.equal(evaluate(measured).status, 'unknown')
    assert.equal(measured.error.observed, true)
    assert.equal(JSON.stringify(measured).includes('[REDACTED]'), false)
    for (const handle of [fake.error, fake.explanation, fake.recovery]) assert.equal(handle.disposed, 1)
  }
})

test('explanation comes from its separate observed node, never inferred from the error', async () => {
  const fake = fixture()
  fake.options.explanationLocator.handle = fake.error
  const result = await collect(fake)
  assert.equal(result.error.message, 'Save failed')
  assert.equal(Object.hasOwn(result.error, 'whatHappened'), false)
  assert.equal(evaluate(result).status, 'unknown')
  assert.equal(fake.error.disposed, 1)
  assert.equal(fake.explanation.disposed, 0)
})

test('recovery snapshot must still address the original connected visible node', async () => {
  for (const change of [
    (f) => { f.options.recoveryLocator.handle = f.node('Replacement') },
    (f) => { f.recovery.isConnected = false },
    (f) => { f.recovery.visible = false },
    (f) => { f.options.recoveryLocator.amount = 2 },
  ]) {
    const fake = fixture()
    fake.options.recoveryLocator.onSnapshot = () => change(fake)
    const result = await collect(fake)
    assert.equal(Object.hasOwn(result.error, 'recoveryName'), false)
    assert.equal(evaluate(result).status, 'unknown')
    assert.equal(fake.recovery.disposed, 1)
  }
})

test('snapshot root names decode JSON and ignore child names and locator descriptions', async () => {
  for (const name of ['Retry "save"', 'Line\nTwo', 'Back\\slash', '']) {
    const fake = fixture()
    fake.options.recoveryLocator.snapshot = `- button ${JSON.stringify(name)}:\n  - text "Invented child name"`
    const result = await collect(fake)
    assert.equal(result.error.recoveryName, name)
    assert.equal(evaluate(result).status, name ? 'pass' : 'finding')
  }
})

test('unsupported snapshot syntax never supplies an inferred name', async () => {
  for (const snapshot of [null, 12, '', '- button "Retry"\n- button "Other"',
    '- button "Retry" [disabled]', "- button 'Retry'", '- button "bad\\q"',
    '  - button "Retry"', '- button "Retry"\n  - text "Child"',
    '- button "Retry":\nnot indented', 'x'.repeat(65537)]) {
    const fake = fixture()
    fake.options.recoveryLocator.snapshot = snapshot
    const result = await collect(fake)
    assert.equal(Object.hasOwn(result.error, 'recoveryName'), false)
    assert.equal(result.error.recoveryReachable, true)
    assert.equal(evaluate(result).status, 'unknown')
  }
})

test('credential-shaped and oversized observed strings are omitted without echo or coercion', async () => {
  for (const text of ['Bearer [REDACTED]', 'Authorization: [REDACTED]', 'token=[REDACTED]',
    'password: [REDACTED]', 'cookie: [REDACTED]', 'x'.repeat(2001),
    { toString() { assert.fail('Unexpected coercion') } }]) {
    for (const field of ['message', 'whatHappened', 'recoveryName']) {
      const fake = fixture()
      if (field === 'message') fake.error.innerText = text
      if (field === 'whatHappened') fake.explanation.innerText = text
      if (field === 'recoveryName') fake.options.recoveryLocator.snapshot = typeof text === 'string'
        ? `- button ${JSON.stringify(text)}` : text
      const result = await collect(fake)
      assert.equal(Object.hasOwn(result.error, field), false)
      assert.equal(evaluate(result).status, 'unknown')
      assert.equal(JSON.stringify(result).includes('[REDACTED]'), false)
    }
  }
  const fake = fixture()
  fake.options.recoveryLocator.snapshot = `- button "${'x'.repeat(201)}"`
  assert.equal(Object.hasOwn((await collect(fake)).error, 'recoveryName'), false)
})

test('invalid declarations return null before reading or invoking accessors', async () => {
  for (const referenceValue of ['', null, 42, {}, 'https://example.test',
    'support:x[failed]@tablet', 'support:constructor[failed]@desktop',
    'support:x[prototype]@phone', `support:${'x'.repeat(81)}[failed]@desktop`,
    `${reference}\nBearer [REDACTED]`]) {
    const fake = fixture()
    fake.options.reference = referenceValue
    assert.equal(await collect(fake), null)
    assert.deepEqual(fake.calls, [])
  }
  for (const errorObserved of [undefined, null, 0, 1, 'true', {}]) {
    const fake = fixture()
    fake.options.errorObserved = errorObserved
    assert.equal(await collect(fake), null)
    assert.deepEqual(fake.calls, [])
  }
  for (const key of ['reference', 'errorObserved', 'unused']) {
    const fake = fixture()
    Object.defineProperty(fake.options, key, { get() { assert.fail('Declaration getter invoked') } })
    assert.equal(await collect(fake), null)
  }
  for (const key of ['__proto__', 'constructor', 'prototype']) {
    const fake = fixture()
    Object.defineProperty(fake.options, key, { value: {} })
    assert.equal(await collect(fake), null)
  }
  for (const options of [undefined, null, [], 'invalid']) assert.equal(await collectSupportMeasurements(options), null)
  const inherited = Object.create({ get reference() { assert.fail('Inherited getter invoked') } })
  inherited.errorObserved = true
  assert.equal(await collectSupportMeasurements(inherited), null)
})

test('bounded actual text is preserved exactly and a safe phone reference is accepted', async () => {
  const fake = fixture()
  fake.options.reference = 'support:studio.error[failed]@phone'
  fake.error.innerText = `  ${'x'.repeat(1996)}  `
  fake.explanation.innerText = '  The connection was interrupted.\nYour edits remain here.  '
  fake.options.recoveryLocator.snapshot = `- button "${'r'.repeat(200)}"`
  const result = await collect(fake)
  assert.equal(result.reference, fake.options.reference)
  assert.equal(result.error.message, fake.error.innerText)
  assert.equal(result.error.whatHappened, fake.explanation.innerText)
  assert.equal(result.error.recoveryName.length, 200)
  assert.equal(evaluate(result).status, 'pass')
})

test('input declarations are copied across awaits and outputs are detached JSON', async () => {
  const fake = fixture()
  const pending = collect(fake)
  fake.options.reference = 'support:changed[failed]@phone'
  fake.options.errorObserved = false
  fake.options.explanationLocator = null
  const result = await pending
  assert.equal(result.reference, reference)
  assert.equal(evaluate(result).status, 'pass')
  result.error.message = 'Changed output'
  assert.equal(fake.error.innerText, 'Save failed')
  assert.deepEqual(JSON.parse(JSON.stringify(result)), result)
  const frozen = fixture()
  Object.freeze(frozen.options)
  assert.equal(evaluate(await collect(frozen)).status, 'pass')
})

test('cleanup failures cannot change evidence or dispose caller-owned unacquired handles', async () => {
  const fake = fixture()
  fake.error.disposeError = true
  fake.options.explanationLocator.amount = 0
  const result = await collect(fake)
  assert.equal(result.error.message, 'Save failed')
  assert.equal(evaluate(result).status, 'unknown')
  assert.equal(fake.error.disposed, 1)
  assert.equal(fake.recovery.disposed, 1)
  assert.equal(fake.explanation.disposed, 0)
})
