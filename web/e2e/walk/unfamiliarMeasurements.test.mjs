import test from 'node:test'
import assert from 'node:assert/strict'
import { runInNewContext } from 'node:vm'
import { unfamiliar, evaluateLenses } from './lenses/index.mjs'
import { collectUnfamiliarMeasurements } from './unfamiliarMeasurements.mjs'

const reference = 'unfamiliar:studio[ready]@desktop'
const evidence = (measurements) => ({ schema: 'leaf.walk-evidence.v1', featureId: 'studio',
  state: 'ready', viewport: 'desktop', measurements })
const verdict = (measurements) => unfamiliar.evaluate(evidence(measurements))

// Browser callbacks execute against this DOM. Only ARIA snapshots come from the
// locator API; no evaluate callback is replaced with precomputed measurements.
function fixture() {
  const calls = []
  const forbidden = () => { calls.push('FORBIDDEN'); throw new Error('Unexpected action') }
  const child = {}
  function node() {
    return { isConnected: true, visible: true, disposed: 0,
      rect: { x: 20, y: 30, width: 100, height: 40 },
      style: { display: 'block', visibility: 'visible' },
      contains: (other) => other === child,
      getBoundingClientRect() { return this.rect },
      async isVisible() { return this.visible },
      async dispose() { this.disposed++; if (this.disposeError) throw new Error('dispose unavailable') },
      click: forbidden, focus: forbidden, press: forbidden, scrollIntoView: forbidden,
      setAttribute: forbidden }
  }
  const primary = node(), navigation = node(), replacement = node()
  const document = { elementFromPoint(x, y) { calls.push(['hit', x, y]); return document.hit } }
  document.hit = primary
  const window = { innerWidth: 800, innerHeight: 600, getComputedStyle: (target) => target.style }
  const page = { evaluate: async (callback, argument) => {
    calls.push('evaluate')
    if (page.error) throw new Error('Authorization: [REDACTED]')
    return runInNewContext(`(${callback.toString()})(argument)`, { argument, document, window })
  }, click: forbidden, goto: forbidden, keyboard: { press: forbidden },
  mouse: { click: forbidden }, touchscreen: { tap: forbidden } }
  function locator(handle, snapshot) {
    return { handle, amount: 1, snapshot, description: 'Invented caller label',
      async count() { if (this.countError) throw new Error('count unavailable'); return this.amount },
      async elementHandle(settings) {
        assert.ok(settings.timeout > 0 && settings.timeout <= 1500)
        if (this.handleError) throw new Error('handle unavailable')
        return this.handle
      },
      async ariaSnapshot(settings) {
        assert.ok(settings.timeout > 0 && settings.timeout <= 1500)
        calls.push('snapshot')
        this.snapshotReads = (this.snapshotReads || 0) + 1
        if (this.snapshotError) throw new Error('secret=[REDACTED]')
        this.duringSnapshot?.()
        return this.snapshot
      }, click: forbidden, focus: forbidden, press: forbidden, scrollIntoViewIfNeeded: forbidden }
  }
  const primaryLocator = locator(primary, '- button "Create drawing"')
  const navigationLocator = locator(navigation, '- navigation "Studio":\n  - link "Drawings"')
  const input = { page, primaryLocator, navigationLocator, reference, nextStepFound: true }
  return { input, page, primaryLocator, navigationLocator, primary, navigation, replacement,
    child, document, window, calls }
}
const collect = (fake, changes = {}) => collectUnfamiliarMeasurements({ ...fake.input, ...changes })
function noActions(fake) { assert.equal(fake.calls.includes('FORBIDDEN'), false) }

test('actual DOM observations satisfy the frozen unfamiliar lens with measured provenance only', async () => {
  const fake = fixture()
  const result = await collect(fake)
  assert.deepEqual(result, { reference, primary: { role: 'button', name: 'Create drawing', reachable: true },
    navigation: { nextStepFound: true, role: 'navigation', name: 'Studio' } })
  assert.ok(Object.isFrozen(unfamiliar))
  assert.equal(verdict(result).status, 'pass')
  assert.equal(verdict(result).results[0].provenance, 'measured')
  for (const lens of evaluateLenses(evidence(result))) {
    assert.equal(lens.status, lens.lens === 'unfamiliar' ? 'pass' : 'unknown', lens.lens)
  }
  assert.ok(fake.calls.some((call) => Array.isArray(call) && call[1] === 70 && call[2] === 50))
  assert.equal(fake.primary.disposed, 1)
  assert.equal(fake.navigation.disposed, 1)
  noActions(fake)
})

test('each observed label, role, reachability or next-step failure is a real finding', async () => {
  for (const setup of [
    (f) => { f.primaryLocator.snapshot = '- button ""' },
    (f) => { f.navigationLocator.snapshot = '- navigation' },
    (f) => { f.primaryLocator.snapshot = '- heading "Create"' },
    (f) => { f.navigationLocator.snapshot = '- region "Studio"' },
    (f) => { f.document.hit = {} },
    (f) => { f.document.hit = null },
    (f) => { f.primary.visible = false },
    (f) => { f.primary.style.visibility = 'hidden' },
    (f) => { f.primary.style.display = 'none' },
    (f) => { f.primary.rect.width = 0 },
    (f) => { f.primary.rect.x = 800 },
    (f) => { f.primary.rect.y = -100 },
    (f) => { f.input.nextStepFound = false },
  ]) {
    const fake = fixture()
    setup(fake)
    const result = verdict(await collect(fake))
    assert.equal(result.status, 'finding')
    assert.equal(result.results[0].finding.evidence.provenance, 'measured')
    noActions(fake)
  }
  const descendant = fixture()
  descendant.document.hit = descendant.child
  descendant.primaryLocator.snapshot = '- link "Create"'
  assert.equal(verdict(await collect(descendant)).status, 'pass')
})

test('next-step evidence is explicit, never inferred from successful reads or reachability', async () => {
  for (const nextStepFound of [undefined, null, 1, 'true', {}, () => true]) {
    const fake = fixture()
    const result = await collect(fake, { nextStepFound })
    assert.equal(result.primary.reachable, true)
    assert.equal(Object.hasOwn(result.navigation, 'nextStepFound'), false)
    assert.equal(verdict(result).status, 'unknown')
    assert.ok(verdict(result).results[0].missing.includes('measurements.navigation.nextStepFound'))
    noActions(fake)
  }
})

test('root names decode JSON escapes; descendants and locator descriptions never supply names', async () => {
  for (const name of ['Create "Drawing"', 'Line\nTwo', 'Back\\slash', '']) {
    const fake = fixture()
    fake.primaryLocator.snapshot = `- button ${JSON.stringify(name)}:\n  - text "Ignored child"`
    const result = await collect(fake)
    assert.equal(result.primary.name, name)
    assert.notEqual(result.primary.name, fake.primaryLocator.description)
    assert.equal(verdict(result).status, name ? 'pass' : 'finding')
  }
})

test('missing, ambiguous, unsupported and oversized snapshots stay UNKNOWN', async () => {
  for (const snapshot of ['', null, 123, '- button "One"\n- button "Two"',
    '  - button "Create"', '- button "Create" [disabled]', "- button 'Create'",
    '- button "bad\\q"', '- button "Create"\n  - text "Child"',
    '- button "Create":\nnot indented', '- constructor "Create"', 'x'.repeat(65537)]) {
    for (const key of ['primaryLocator', 'navigationLocator']) {
      const fake = fixture()
      fake[key].snapshot = snapshot
      const result = await collect(fake)
      const branch = key === 'primaryLocator' ? result.primary : result.navigation
      assert.equal(Object.hasOwn(branch, 'name'), false)
      assert.equal(Object.hasOwn(branch, 'role'), false)
      assert.equal(verdict(result).status, 'unknown')
    }
  }
})

test('replacement during either snapshot discards semantics and disposes all owned handles', async () => {
  for (const key of ['primaryLocator', 'navigationLocator']) {
    const fake = fixture()
    fake[key].duringSnapshot = () => { fake[key].handle = fake.replacement }
    const result = await collect(fake)
    const branch = key === 'primaryLocator' ? result.primary : result.navigation
    assert.equal(Object.hasOwn(branch, 'role'), false)
    assert.equal(Object.hasOwn(branch, 'name'), false)
    assert.equal(verdict(result).status, 'unknown')
    assert.equal(fake.replacement.disposed, 1)
    assert.equal(fake.primary.disposed, 1)
    assert.equal(fake.navigation.disposed, 1)
    noActions(fake)
  }
})

test('identity is checked before snapshot and ambiguity introduced by snapshot is UNKNOWN', async () => {
  for (const key of ['primaryLocator', 'navigationLocator']) {
    const fake = fixture()
    const locator = fake[key]
    const originalAcquire = locator.elementHandle
    let acquisitions = 0
    locator.elementHandle = async function (settings) {
      if (++acquisitions === 2) this.handle = fake.replacement
      return originalAcquire.call(this, settings)
    }
    const result = await collect(fake)
    assert.equal(locator.snapshotReads, undefined)
    assert.equal(verdict(result).status, 'unknown')
    assert.equal(fake.replacement.disposed, 1)
    const ambiguous = fixture()
    ambiguous[key].duringSnapshot = () => { ambiguous[key].amount = 2 }
    assert.equal(verdict(await collect(ambiguous)).status, 'unknown')
    noActions(fake)
    noActions(ambiguous)
  }
})

test('absent, ambiguous, detached and unreadable observations never become empty findings', async () => {
  for (const setup of [
    (f) => { f.primaryLocator.amount = 0 },
    (f) => { f.navigationLocator.amount = 2 },
    (f) => { f.primaryLocator.handle = null },
    (f) => { f.primary.isConnected = false },
    (f) => { f.primaryLocator.countError = true },
    (f) => { f.navigationLocator.handleError = true },
    (f) => { f.primaryLocator.snapshotError = true },
    (f) => { f.page.error = true },
    (f) => { f.primary.isVisible = async () => { throw new Error('unavailable') } },
    (f) => { f.primary.rect.x = NaN },
    (f) => { f.window.innerWidth = 0 },
    (f) => { f.document.elementFromPoint = () => { throw new Error('unavailable') } },
    (f) => { f.primaryLocator.duringSnapshot = () => { f.primary.isConnected = false } },
  ]) {
    const fake = fixture()
    setup(fake)
    const result = await collect(fake)
    assert.equal(verdict(result).status, 'unknown')
    assert.equal(JSON.stringify(result).includes('[REDACTED]'), false)
    noActions(fake)
  }
})

test('credential-shaped and oversized names are omitted without retaining exception text', async () => {
  for (const name of ['Bearer [REDACTED]', 'Authorization: [REDACTED]', 'token=[REDACTED]',
    'password=[REDACTED]', 'cookie=[REDACTED]', 'x'.repeat(201)]) {
    const fake = fixture()
    fake.primaryLocator.snapshot = `- button ${JSON.stringify(name)}`
    const result = await collect(fake)
    assert.equal(result.primary.role, 'button')
    assert.equal(Object.hasOwn(result.primary, 'name'), false)
    assert.equal(JSON.stringify(result).includes(name), false)
    assert.equal(verdict(result).status, 'unknown')
  }
})

test('invalid inputs return null before reads, without getters or coercion', async () => {
  assert.equal(await collectUnfamiliarMeasurements(), null)
  assert.equal(await collectUnfamiliarMeasurements(null), null)
  let coercions = 0
  for (const value of ['', 'https://example.test', 'unfamiliar:studio[ready]@tablet',
    'unfamiliar:constructor[ready]@desktop', 'unfamiliar:x[__proto__]@desktop',
    'unfamiliar:x[token=[REDACTED]]@desktop', `${reference}\n`,
    `unfamiliar:${'x'.repeat(81)}[ready]@phone`, { toString() { coercions++; return reference } }]) {
    const fake = fixture()
    assert.equal(await collect(fake, { reference: value }), null)
    assert.deepEqual(fake.calls, [])
  }
  assert.equal(coercions, 0)
  for (const key of ['page', 'primaryLocator', 'navigationLocator', 'reference', 'nextStepFound']) {
    const fake = fixture()
    let getterCalls = 0
    Object.defineProperty(fake.input, key, { get() { getterCalls++; return reference } })
    assert.equal(await collectUnfamiliarMeasurements(fake.input), null)
    assert.equal(getterCalls, 0)
    assert.deepEqual(fake.calls, [])
  }
  const fake = fixture()
  assert.equal(await collect(fake, { reference: 'unfamiliar:studio[ready]@phone' }).then(verdict).then((r) => r.status), 'pass')
})

test('inputs are captured before awaits, outputs are detached, cleanup is best effort and deduplicated', async () => {
  const fake = fixture()
  const pending = collectUnfamiliarMeasurements(fake.input)
  fake.input.nextStepFound = false
  fake.input.reference = 'rewritten'
  fake.input.primaryLocator = null
  const result = await pending
  assert.equal(verdict(result).status, 'pass')
  result.primary.name = 'rewritten'
  result.navigation.nextStepFound = false
  assert.equal(fake.primaryLocator.snapshot, '- button "Create drawing"')
  const frozen = fixture()
  Object.freeze(frozen.input)
  frozen.primary.disposeError = true
  assert.equal(verdict(await collectUnfamiliarMeasurements(frozen.input)).status, 'pass')
  assert.equal(frozen.primary.disposed, 1)
  assert.equal(frozen.navigation.disposed, 1)
  const shared = fixture()
  shared.navigationLocator.handle = shared.primary
  assert.equal(verdict(await collect(shared)).status, 'pass')
  assert.equal(shared.primary.disposed, 1)
  assert.equal(shared.navigation.disposed, 0)
  noActions(fake)
  noActions(frozen)
  noActions(shared)
})
