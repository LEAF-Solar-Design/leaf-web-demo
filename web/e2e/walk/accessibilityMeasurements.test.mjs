import test from 'node:test'
import assert from 'node:assert/strict'
import { runInNewContext } from 'node:vm'
import { accessibility } from './lenses/index.mjs'
import { createAccessibilityMeasurementCapture } from './accessibilityMeasurements.mjs'

const reference = 'accessibility:studio-dialog[ready]@desktop'
const verdict = (measurements) => accessibility.evaluate({ schema: 'leaf.walk-evidence.v1',
  featureId: 'studio-dialog', state: 'ready', viewport: 'desktop', measurements }).status

// The real browser callbacks execute against these DOM objects. No callback is
// replaced with a precomputed observation, and every action method is forbidden.
function fixture(options = {}) {
  const calls = []
  const forbidden = () => { calls.push('FORBIDDEN ACTION'); throw new Error('Collector attempted an action') }
  const node = () => ({ isConnected: true, visible: true, disposed: 0,
    contains(other) { return other === this || other === child },
    getAttribute(name) {
      assert.equal(name, 'aria-modal')
      if (options.modalError) throw new Error('attribute unavailable')
      return options.modal === undefined ? 'true' : options.modal
    },
    matches(selector) { assert.equal(selector, ':modal'); return options.nativeModal ?? false },
    async isVisible() { if (options.hiddenError) throw new Error('visibility unavailable'); return this.visible },
    async dispose() { this.disposed += 1; if (options.disposeError) throw new Error('dispose unavailable') },
    click: forbidden, focus: forbidden, press: forbidden, setAttribute: forbidden, close: forbidden })
  const child = {}
  const original = node(), opener = node(), returning = node(), outside = node()
  const document = { activeElement: opener }
  let evaluateError = false
  const page = { evaluate: async (callback, argument) => {
    calls.push('evaluate')
    if (evaluateError) throw new Error('page unavailable')
    return runInNewContext(`(${callback.toString()})(argument)`, { argument, document })
  }, keyboard: { press: forbidden } }
  function locator(handle, label) {
    return { handle, amount: 1, visible: true, description: 'Invented locator name',
      count: async function () { calls.push(`${label}.count`); return this.amount },
      isVisible: async function () { calls.push(`${label}.visible`); return this.visible },
      elementHandle: async function (settings) {
        assert.ok(settings.timeout > 0 && settings.timeout <= 1500)
        calls.push(`${label}.handle`)
        return this.handle
      },
      ariaSnapshot: async (settings) => {
        assert.ok(settings.timeout > 0 && settings.timeout <= 1500)
        if (options.snapshotError) throw new Error('snapshot unavailable')
        return options.snapshot ?? '- dialog "Actual Studio name":\n  - button "Done"'
      }, click: forbidden, focus: forbidden, press: forbidden }
  }
  const dialog = locator(original, 'dialog')
  const openTarget = { key: 'initial-control', locator: locator(opener, 'open') }
  const returnTarget = { key: 'launch-control', locator: locator(returning, 'return') }
  const capture = createAccessibilityMeasurementCapture({ page, dialog, openTarget, returnTarget,
    reference: options.reference ?? reference })
  return { capture, page, dialog, openTarget, returnTarget, original, opener, returning,
    outside, child, document, calls, failEvaluation(value = true) { evaluateError = value } }
}

async function complete(fake, { tab = fake.child, close = true, returned = fake.returning } = {}) {
  await fake.capture.opened()
  fake.document.activeElement = tab
  await fake.capture.afterTab()
  fake.original.visible = !close
  fake.document.activeElement = returned
  await fake.capture.afterEscape()
  assert.equal(fake.calls.includes('FORBIDDEN ACTION'), false)
  return fake.capture.result()
}

test('complete observed trace passes the frozen real accessibility lens', async () => {
  const fake = fixture()
  const result = await complete(fake)
  assert.deepEqual(result, { reference, overlay: { role: 'dialog', name: 'Actual Studio name', modal: true },
    focus: { openTarget: 'initial-control', actualOpenTarget: 'initial-control', tabRemainsInside: true,
      escapeClosed: true, returnTarget: 'launch-control', actualReturnTarget: 'launch-control' },
    diagnostics: { tabSamples: 1 } })
  assert.equal(verdict(result), 'pass')
  assert.ok(Object.isFrozen(accessibility))
  await fake.capture.dispose()
})

test('each observed semantic or focus mismatch produces a real finding', async () => {
  for (const [options, setup, phases] of [
    [{ snapshot: '- alertdialog "Actual"' }],
    [{ snapshot: '- dialog ""' }],
    [{ modal: 'false' }],
    [{}, (f) => { f.document.activeElement = f.outside }],
    [{}, undefined, (f) => ({ tab: f.outside })],
    [{}, undefined, () => ({ close: false })],
    [{}, undefined, (f) => ({ returned: f.outside })],
  ]) {
    const fake = fixture(options)
    setup?.(fake)
    assert.equal(verdict(await complete(fake, phases?.(fake))), 'finding')
    await fake.capture.dispose()
  }
})

test('omitted phases and out-of-order phases stay absent and unknown', async () => {
  const fake = fixture()
  await fake.capture.afterTab()
  await fake.capture.afterEscape()
  assert.deepEqual(fake.calls, [])
  assert.deepEqual(fake.capture.result().focus, {})
  assert.equal(verdict(fake.capture.result()), 'unknown')
  await fake.capture.opened()
  assert.equal(verdict(fake.capture.result()), 'unknown')
  fake.document.activeElement = fake.child
  await fake.capture.afterTab()
  assert.equal(verdict(fake.capture.result()), 'unknown')
  assert.equal(Object.hasOwn(fake.capture.result().focus, 'escapeClosed'), false)
  await fake.capture.dispose()
  const noTab = fixture()
  await noTab.capture.opened()
  noTab.original.visible = false
  noTab.document.activeElement = noTab.returning
  await noTab.capture.afterEscape()
  assert.equal(verdict(noTab.capture.result()), 'unknown')
  assert.equal(Object.hasOwn(noTab.capture.result().focus, 'tabRemainsInside'), false)
  await noTab.capture.dispose()
})

test('false containment persists across later successful samples and repeated opened', async () => {
  const fake = fixture()
  await fake.capture.opened()
  fake.document.activeElement = fake.outside
  await fake.capture.afterTab()
  await fake.capture.opened()
  fake.document.activeElement = fake.original
  await fake.capture.afterTab()
  fake.document.activeElement = fake.child
  await fake.capture.afterTab()
  assert.equal(fake.capture.result().focus.tabRemainsInside, false)
  assert.equal(fake.capture.result().diagnostics.tabSamples, 3)
  fake.original.visible = false
  fake.document.activeElement = fake.returning
  await fake.capture.afterEscape()
  assert.equal(verdict(fake.capture.result()), 'finding')
  const before = fake.capture.result()
  await fake.capture.opened()
  await fake.capture.afterTab()
  await fake.capture.afterEscape()
  assert.deepEqual(fake.capture.result(), before)
  assert.equal(fake.calls.filter((call) => call === 'dialog.handle').length, 2)
  await fake.capture.dispose()
})

test('snapshot names come from the root and JSON escapes are decoded', async () => {
  for (const name of ['Studio "Settings"', 'Line\nTwo', 'Back\\slash', '']) {
    const fake = fixture({ snapshot: `- dialog ${JSON.stringify(name)}` })
    const result = await complete(fake)
    assert.equal(result.overlay.name, name)
    assert.notEqual(result.overlay.name, fake.dialog.description)
    assert.equal(verdict(result), name ? 'pass' : 'finding')
    await fake.capture.dispose()
  }
  const nameless = fixture({ snapshot: '- dialog' })
  assert.equal((await complete(nameless)).overlay.name, '')
  await nameless.capture.dispose()
})

test('unsupported or ambiguous root syntax supplies no role or name', async () => {
  for (const snapshot of ['dialog "Studio"', '- dialog "One"\n- dialog "Two"',
    '- dialog "Studio" [modal]', "- dialog 'Studio'", '- dialog "bad\\q"',
    '  - dialog "Indented"', '- dialog "Studio": extra', '- dialog "Studio"\n  - button "Child"',
    '- dialog "Studio":\nnot indented', '', null]) {
    const fake = fixture({ snapshot: snapshot === null ? 123 : snapshot })
    const measured = await complete(fake)
    assert.equal(Object.hasOwn(measured.overlay, 'role'), false, String(snapshot))
    assert.equal(Object.hasOwn(measured.overlay, 'name'), false, String(snapshot))
    assert.equal(verdict(measured), 'unknown')
    await fake.capture.dispose()
  }
})

test('credential-shaped and oversized names are omitted, never echoed', async () => {
  for (const name of ['Bearer top-secret', 'password=hunter2', 'Authorization: private',
    'cookie=session-private', 'eyJabc.def.ghi', 'x'.repeat(201)]) {
    const fake = fixture({ snapshot: `- dialog ${JSON.stringify(name)}` })
    const result = await complete(fake)
    assert.equal(result.overlay.role, 'dialog')
    assert.equal(Object.hasOwn(result.overlay, 'name'), false)
    assert.equal(JSON.stringify(result).includes(name), false)
    assert.equal(verdict(result), 'unknown')
    await fake.capture.dispose()
  }
})

test('modal semantics observe explicit false and native :modal', async () => {
  for (const [options, expected] of [[{ modal: 'false', nativeModal: true }, false],
    [{ modal: null, nativeModal: true }, true], [{ modal: null, nativeModal: false }, false]]) {
    const fake = fixture(options)
    const measured = await complete(fake)
    assert.equal(measured.overlay.modal, expected)
    assert.equal(verdict(measured), expected ? 'pass' : 'finding')
    await fake.capture.dispose()
  }
  const invalid = fixture({ modal: 'invalid' })
  assert.equal(verdict(await complete(invalid)), 'unknown')
  assert.equal(Object.hasOwn(invalid.capture.result().overlay, 'modal'), false)
  await invalid.capture.dispose()
})

test('ambiguous, hidden, absent or disconnected dialogs do not establish an open phase', async () => {
  for (const setup of [(f) => { f.dialog.amount = 2 }, (f) => { f.dialog.amount = 0 },
    (f) => { f.dialog.visible = false }, (f) => { f.dialog.handle = null },
    (f) => { f.original.isConnected = false },
    (f) => { f.dialog.count = async () => { throw new Error('unavailable') } }]) {
    const fake = fixture()
    setup(fake)
    assert.equal(verdict(await complete(fake)), 'unknown')
    assert.deepEqual(fake.capture.result().focus, {})
    assert.equal(fake.capture.result().diagnostics.tabSamples, 0)
    await fake.capture.dispose()
    assert.ok(fake.original.disposed <= 1)
  }
})

test('unavailable reads leave missing fields and never throw into caller assertions', async () => {
  for (const options of [{ snapshotError: true }, { modalError: true }, { hiddenError: true }]) {
    const fake = fixture(options)
    assert.equal(verdict(await complete(fake)), 'unknown')
    await fake.capture.dispose()
  }
  for (const phase of ['opened', 'afterTab', 'afterEscape']) {
    const fake = fixture()
    if (phase !== 'opened') await fake.capture.opened()
    if (phase === 'afterEscape') await fake.capture.afterTab()
    fake.failEvaluation()
    await fake.capture[phase]()
    assert.equal(verdict(fake.capture.result()), 'unknown')
    await fake.capture.dispose()
  }
  const fake = fixture()
  fake.openTarget.locator.handle = null
  fake.returnTarget.locator.elementHandle = async () => { throw new Error('target unavailable') }
  assert.equal(verdict(await complete(fake)), 'unknown')
  assert.equal(Object.hasOwn(fake.capture.result().focus, 'actualOpenTarget'), false)
  assert.equal(Object.hasOwn(fake.capture.result().focus, 'actualReturnTarget'), false)
  await fake.capture.dispose()
  const unreadVisibility = fixture()
  unreadVisibility.original.isVisible = async () => undefined
  assert.equal(verdict(await complete(unreadVisibility)), 'unknown')
  assert.equal(Object.hasOwn(unreadVisibility.capture.result().focus, 'escapeClosed'), false)
  await unreadVisibility.capture.dispose()
})

test('a replacement during the locator snapshot cannot supply original dialog semantics', async () => {
  const fake = fixture()
  fake.dialog.ariaSnapshot = async () => {
    fake.dialog.handle = fake.outside
    return '- dialog "Replacement name"'
  }
  const measured = await complete(fake)
  assert.equal(Object.hasOwn(measured.overlay, 'role'), false)
  assert.equal(Object.hasOwn(measured.overlay, 'name'), false)
  assert.equal(verdict(measured), 'unknown')
  await fake.capture.dispose()
  assert.equal(fake.original.disposed, 1)
  assert.equal(fake.outside.disposed, 1)
})

test('an unread Tab prevents a partial set of samples from passing', async () => {
  const fake = fixture()
  await fake.capture.opened()
  await fake.capture.afterTab()
  fake.failEvaluation()
  await fake.capture.afterTab()
  fake.failEvaluation(false)
  await fake.capture.afterTab()
  fake.original.visible = false
  fake.document.activeElement = fake.returning
  await fake.capture.afterEscape()
  assert.equal(fake.capture.result().diagnostics.tabSamples, 2)
  assert.equal(Object.hasOwn(fake.capture.result().focus, 'tabRemainsInside'), false)
  assert.equal(verdict(fake.capture.result()), 'unknown')
  await fake.capture.dispose()
})

test('replacement dialogs and target nodes cannot rewrite the original identity', async () => {
  const fake = fixture()
  await fake.capture.opened()
  fake.dialog.handle = fake.outside
  fake.original.isConnected = false
  fake.document.activeElement = fake.outside
  await fake.capture.afterTab()
  fake.returnTarget.locator.handle = fake.outside
  fake.returning.isConnected = false
  await fake.capture.afterEscape()
  const measured = fake.capture.result()
  assert.equal(measured.focus.tabRemainsInside, false)
  assert.equal(measured.focus.escapeClosed, true)
  assert.equal(measured.focus.actualReturnTarget, '__focus_mismatch__')
  assert.equal(verdict(measured), 'finding')
  await fake.capture.dispose()
  assert.equal(fake.outside.disposed, 0)
  const detached = fixture()
  detached.opener.isConnected = false
  assert.equal((await complete(detached)).focus.actualOpenTarget, '__focus_mismatch__')
  await detached.capture.dispose()
  const closed = fixture()
  await closed.capture.opened()
  closed.document.activeElement = closed.child
  await closed.capture.afterTab()
  closed.original.isConnected = false
  closed.document.activeElement = closed.returning
  await closed.capture.afterEscape()
  assert.equal(verdict(closed.capture.result()), 'pass')
  await closed.capture.dispose()
})

test('disposal is explicit, idempotent, safe when incomplete, and releases shared handles once', async () => {
  const fake = fixture({ disposeError: true })
  fake.returnTarget.locator.handle = fake.opener
  await fake.capture.opened()
  await Promise.all([fake.capture.dispose(), fake.capture.dispose()])
  assert.equal(fake.original.disposed, 1)
  assert.equal(fake.opener.disposed, 1)
  assert.equal(fake.returning.disposed, 0)
  const before = fake.capture.result(), calls = fake.calls.length
  await fake.capture.opened()
  await fake.capture.afterTab()
  await fake.capture.afterEscape()
  assert.deepEqual(fake.capture.result(), before)
  assert.equal(fake.calls.length, calls)
  const incomplete = fixture()
  await incomplete.capture.dispose()
  await incomplete.capture.opened()
  assert.deepEqual(incomplete.calls, [])
})

test('references, declared safe keys and the reserved sentinel are validated before observation', () => {
  const base = { reference, openTarget: { key: 'initial' }, returnTarget: { key: 'return' } }
  for (const value of ['', 'https://example.test', 'accessibility:x[ready]@tablet',
    'accessibility:x[token=private]@desktop', `accessibility:${'x'.repeat(81)}[ready]@phone`,
    `${reference}\nBearer private`]) {
    assert.throws(() => createAccessibilityMeasurementCapture({ ...base, reference: value }), TypeError)
  }
  for (const key of ['', '__focus_mismatch__', 'arbitrary DOM id', '#target', 'x'.repeat(81), null]) {
    for (const target of ['openTarget', 'returnTarget']) {
      assert.throws(() => createAccessibilityMeasurementCapture({ ...base, [target]: { key } }), TypeError)
    }
  }
  assert.throws(() => createAccessibilityMeasurementCapture(), TypeError)
  assert.doesNotThrow(() => createAccessibilityMeasurementCapture({ ...base,
    reference: 'accessibility:studio.dialog[ready]@phone' }))
})

test('results are detached JSON; caller merge preserves mobile fields and frozen inputs', async () => {
  const fake = fixture()
  Object.freeze(fake.openTarget)
  Object.freeze(fake.returnTarget)
  const evidence = Object.freeze({ measurements: Object.freeze({
    input: Object.freeze({ touch: true }), control: Object.freeze({ reachable: true }),
    viewport: Object.freeze({ width: 390, height: 844 }),
  }) })
  const measured = await complete(fake)
  const merged = { ...evidence.measurements, ...measured }
  assert.equal(merged.input, evidence.measurements.input)
  assert.equal(merged.control, evidence.measurements.control)
  assert.equal(merged.viewport, evidence.measurements.viewport)
  assert.equal(Object.hasOwn(evidence.measurements, 'overlay'), false)
  measured.focus.actualOpenTarget = 'rewritten'
  measured.overlay.name = 'rewritten'
  measured.diagnostics.tabSamples = 999
  assert.equal(verdict(fake.capture.result()), 'pass')
  assert.equal(fake.capture.result().diagnostics.tabSamples, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(fake.capture.result())), fake.capture.result())
  await fake.capture.dispose()
  const mutable = fixture()
  mutable.openTarget.key = 'changed-after-creation'
  mutable.returnTarget.key = 'changed-after-creation'
  assert.equal(verdict(await complete(mutable)), 'pass')
  assert.equal(mutable.capture.result().focus.openTarget, 'initial-control')
  await mutable.capture.dispose()
})
