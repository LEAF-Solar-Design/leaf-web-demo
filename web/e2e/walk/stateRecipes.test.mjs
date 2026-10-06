import test from 'node:test'
import assert from 'node:assert/strict'
import { ACTIONS, accessibleName } from '../../src/lib/actionRegistry.js'
import { holdJobRoutes, openHistory, previewVersion, startPendingRun, runProbe, assertEffect, setupStep, requireNoDrawing, VERSIONLESS_DRAWING_REASON, solarCalibrationFailure,
  assertEngineMode, setJobRail, solarBrowserCatalogRequired, barNoRung, captureBarNoRung, assertBarNoRung, installGeometryObserver,
  captureEngineRefusal, assertEngineRefusal, assertCopiedGeometry, expectedVersionHead, assertVersionTransition, establishZoomBaseline } from './fixtures.mjs'
import { solarDocumentProbe, authorAvailability, disclosureEvidence, phoneRailAvailability, completeSolarReadiness, UnsupportedLocalError, recoverFailedCatalog } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'
import { captureEngineRepeat, activateEngineRepeat } from './fixtures.mjs'
import { faultRouteOnce, drawingRequest, setupVersionFault, finishVersionFault,
  setupHistoryFault, assertHistoryRecovery, setupRealProject } from './fixtures.mjs'
import { CENSUS_DISCLOSURES, censusDisclosureState, setupCensusDisclosure, setupCensusScript,
  setupCensusDownloadOnly, activateCensusFileChooser, captureCensusEffect, assertCensusEffect } from './fixtures.mjs'

const censusAssertions = (value) => ({
  toBe: (expected) => assert.equal(value, expected),
  toBeTruthy: () => assert.ok(value),
  toBeGreaterThan: (expected) => assert.ok(value > expected),
  toBeVisible: async () => assert.equal(value.visible, true),
  toBeHidden: async () => assert.equal(value.visible, false),
  toBeDisabled: async () => assert.equal(value.disabled, true),
  toBeEnabled: async () => assert.equal(value.disabled, false),
  toHaveCount: async (expected) => assert.equal(value.countValue ?? 1, expected),
  toHaveAttribute: async (name, expected) => assert.equal(await value.getAttribute(name), expected),
  toHaveValue: async (expected) => assert.equal(value.text, expected),
  toHaveText: async (expected) => assert.equal(value.text, expected),
  toContainText: async (expected) => assert.ok(value.text.includes(expected)),
})

test('Repeat focuses the drawing landmark, presses Enter and restores its tab stop', async () => {
  for (const original of [null, '0']) {
    const events = []
    let tabindex = original, focused = false
    const element = {
      setAttribute: (name, value) => { assert.equal(name, 'tabindex'); tabindex = value },
      removeAttribute: (name) => { assert.equal(name, 'tabindex'); tabindex = null },
      focus: () => { focused = true; events.push('focus') },
    }
    const locator = { getAttribute: async () => tabindex, evaluate: async (fn, arg) => fn(element, arg) }
    const runtime = { cleanup: [], page: { keyboard: { press: async (key) => { assert.ok(focused); events.push(key) } } } }
    await activateEngineRepeat(runtime, locator, () => ({ toBeFocused: async () => assert.ok(focused) }))
    assert.deepEqual(events, ['focus', 'Enter'])
    assert.equal(tabindex, '-1')
    await runtime.cleanup[0]()
    assert.equal(tabindex, original)
  }
})

test('Repeat baseline dismisses an old operand prompt and observes worker messages', async () => {
  const events = []
  const dispatches = [{ type: 'loadDocument' }]
  const runtime = { evidence: {}, page: {
    keyboard: { press: async (key) => events.push(key) },
    getByRole: () => ({ fill: async (value) => { assert.equal(value, ''); events.push('empty-bar') } }),
    getByTestId: () => ({ visible: false }),
    evaluate: async (fn) => fn.toString().includes('structuredClone') ? structuredClone(dispatches) : undefined,
  } }
  assert.deepEqual(await captureEngineRepeat({ state: 'engine-busy' }, runtime, censusAssertions), { dispatches })
  assert.deepEqual(events, ['Escape', 'empty-bar'])
  assert.deepEqual(runtime.evidence.repeatBaseline, { dispatches })
})

test('every Repeat oracle distinguishes a prompt, an announced refusal and a silent ignored key', async () => {
  const entry = buildFeatureMap().entries.find((row) => row.id === 'action:engine-repeat')
  assert.deepEqual([...entry.states].sort(), ['engine-busy', 'engine-crashed', 'engine-not-parsed', 'no-command-to-repeat', 'no-drawing', 'ready'])
  for (const state of entry.states) {
    const probe = resolveProbe(entry, state)
    for (const fault of [null, 'dispatch', ...(state === 'ready' ? ['wrong-command'] : ['armed']),
      ...(['engine-busy', 'no-command-to-repeat'].includes(state) ? ['wrong-refusal'] : [])]) {
      const prompt = { visible: state === 'ready' || fault === 'armed', name: fault === 'wrong-command' ? 'CIRCLE command' : 'LINE command' }
      const status = { text: fault === 'wrong-refusal' ? 'unrelated status' : probe.assertion.reason,
        innerText: async () => status.text, isVisible: async () => false }
      let statusReads = 0
      const before = { dispatches: [{ type: 'loadDocument' }] }
      const runtime = { evidence: {}, page: {
        getByTestId: (id) => id === 'cockpit-prompt' ? prompt : { getByRole: (role, options) => {
          assert.equal(role, 'status'); assert.deepEqual(options, { includeHidden: true }); statusReads++; return status
        } },
        evaluate: async (fn) => fn.toString().includes('structuredClone')
          ? [...before.dispatches, ...(fault === 'dispatch' ? [{ type: 'edit', op: 'createLine' }] : [])] : undefined,
      } }
      const assertions = (value) => ({ ...censusAssertions(value),
        toEqual: (expected) => assert.deepEqual(value, expected),
        toHaveAccessibleName: async (expected) => assert.equal(value.name, expected),
      })
      const check = () => assertEffect(probe, runtime, {}, before, assertions)
      if (fault) await assert.rejects(check())
      else {
        await check()
        const ignored = ['no-drawing', 'engine-not-parsed', 'engine-crashed'].includes(state)
        assert.equal(runtime.evidence.repeatIgnored, ignored ? true : undefined)
        assert.equal(statusReads, ignored || state === 'ready' ? 0 : 1)
        assert.deepEqual(runtime.evidence.repeatDispatches.after, before.dispatches)
        if (statusReads) assert.deepEqual(runtime.evidence.repeatRefusal, { text: probe.assertion.reason, visible: false })
      }
    }
  }
})

test('failed catalog recovery listens before Retry, awaits its reply and retries only once', async () => {
  const events = []
  let visible = true, resolveResponse
  const reason = "Couldn't load tools: startup timed out Retry"
  const status = {
    filter: (options) => { assert.match(reason, options.hasText); return status },
    isVisible: async () => visible,
    innerText: async () => reason,
    getByRole: (role, options) => {
      assert.equal(role, 'button')
      assert.deepEqual(options, { name: 'Retry', exact: true })
      return { click: async (options) => { assert.equal(options.timeout, 15_000); events.push('click') } }
    },
  }
  const page = {
    getByRole: (role, options) => {
      assert.equal(role, 'toolbar')
      assert.deepEqual(options, { name: 'Drafting tools', exact: true })
      return { getByRole: (role) => { assert.equal(role, 'status'); return status } }
    },
    waitForResponse: (predicate, options) => {
      assert.equal(options.timeout, 15_000)
      const reply = (method, path, ok) => ({ request: () => ({ method: () => method }),
        url: () => `http://walk${path}`, ok: () => ok })
      assert.equal(predicate(reply('GET', '/api/capabilities?drawing_id=private', true)), true)
      assert.equal(predicate(reply('POST', '/api/capabilities', true)), false)
      assert.equal(predicate(reply('GET', '/api/capabilities', false)), false)
      assert.equal(predicate(reply('GET', '/api/tools', true)), false)
      events.push('listen')
      return new Promise((resolve) => { resolveResponse = () => { events.push('response'); resolve() } })
    },
  }
  const assertions = (value) => ({ toBeHidden: async (options) => {
    assert.equal(value, status); assert.equal(options.timeout, 15_000)
    assert.equal(visible, false); events.push('hidden')
  } })
  const runtime = { evidence: {} }
  const recovery = recoverFailedCatalog(page, runtime, assertions)
  // Let the visibility, reason and click awaits settle while the reply remains pending.
  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual(events, ['listen', 'click'])
  await recoverFailedCatalog(page, runtime, assertions)
  assert.deepEqual(events, ['listen', 'click'])
  visible = false
  resolveResponse()
  await recovery
  assert.deepEqual(events, ['listen', 'click', 'response', 'hidden'])
  assert.deepEqual(runtime.evidence.catalogRecovery, { reason, retried: true })
  visible = true
  await recoverFailedCatalog(page, runtime, assertions)
  assert.deepEqual(events, ['listen', 'click', 'response', 'hidden'])
})

test('catalog recovery leaves a page without a visible failure untouched', async () => {
  const runtime = { evidence: {} }
  const page = { getByRole: () => ({ getByRole: () => ({ filter: () => ({
    isVisible: async () => false,
    getByRole: () => assert.fail('no Retry without a visible failure'),
  }) }) }), waitForResponse: () => assert.fail('no response wait without a failure') }
  await recoverFailedCatalog(page, runtime)
  assert.deepEqual(runtime.evidence, {})
  assert.equal(runtime.catalogRecoveryAttempted, undefined)
})
censusAssertions.poll = (callback) => ({
  toBe: async (expected) => assert.equal(await callback(), expected),
  toBeGreaterThan: async (expected) => assert.ok(await callback() > expected),
})

test('C2 disclosures click into each initial state, reject a wrong effect and close before restoring viewport', async () => {
  for (const target of Object.keys(CENSUS_DISCLOSURES)) {
    for (const expanded of [false, true]) {
      let open = false
      const events = []
      const body = { getAttribute: async () => open ? 'true' : null,
        evaluate: async (read) => read({ open }) }
      const button = { visible: true, getAttribute: async () => String(open),
        click: async (options) => { assert.equal(options.timeout, 15_000); open = !open; events.push('click') } }
      const page = { locator: (selector) => {
        assert.ok([CENSUS_DISCLOSURES[target].body, '.drawing-objects-content'].includes(selector))
        return selector === '.drawing-objects-content' ? { countValue: open ? 1 : 0 } : body
      }, getByRole: () => button, viewportSize: () => ({ width: 1440, height: 900 }),
      setViewportSize: async (size) => events.push(size.width) }
      const runtime = { page, cleanup: [], evidence: {} }
      await setupCensusDisclosure(runtime, { target, expanded, control: { role: 'button', name: 'Objects' } }, censusAssertions)
      assert.equal(await censusDisclosureState(page, target, censusAssertions), expanded)
      const probe = { assertion: { target, value: !expanded } }
      await assert.rejects(assertCensusEffect(probe, runtime, button, {}, censusAssertions), assert.AssertionError)
      await button.click({ timeout: 15_000 })
      // Import's input visibility follows the real pane posture.
      page.getByLabel = () => ({ visible: open })
      await assertCensusEffect(probe, runtime, button, {}, censusAssertions)
      for (const cleanup of runtime.cleanup.reverse()) await cleanup()
      assert.equal(open, false)
      assert.equal(runtime.evidence.censusDisclosure.cleaned, true)
      if (target === 'ribbon-overflow-expanded') assert.equal(events.at(-1), 1440)
    }
  }
})

test('C2 script setup holds genuine replies only for running and drains them before clearing text', async () => {
  for (const running of [false, true]) {
    const events = []
    const input = { visible: true, disabled: false, text: '', fill: async (text) => { input.text = text; events.push(['text', text]) } }
    const status = { phase: 'idle', text: '', getAttribute: async () => status.phase }
    const transport = { hold: false, pending: [] }
    const page = {
      getByRole: (role) => role === 'textbox' ? input : { click: async () => {
        assert.equal(input.text, 'line 0,0 10,10')
        assert.equal(transport.hold, true)
        status.phase = 'running'; input.disabled = true
        transport.pending.push(() => { events.push('delivered'); input.disabled = false; status.phase = 'done'; status.text = 'Script ran 1 command.' })
      } },
      getByTestId: () => status,
      evaluate: async (read) => {
        globalThis.__walkEngineTransport = transport
        try { return read() } finally { delete globalThis.__walkEngineTransport }
      },
    }
    const runtime = { page, cleanup: [], evidence: {} }
    await setupCensusScript(runtime, { text: 'line 0,0 10,10', running }, censusAssertions)
    assert.equal(input.disabled, running)
    if (!running) await assertCensusEffect({ assertion: { target: 'ribbon-script-text' }, locator: { inputValue: input.text } }, runtime, input, {}, censusAssertions)
    await runtime.cleanup[0]()
    assert.equal(transport.hold, false)
    assert.equal(transport.pending.length, 0)
    assert.equal(input.text, '')
    assert.equal(runtime.evidence.censusScript.cleaned, true)
    if (running) assert.ok(events.indexOf('delivered') < events.findLastIndex((event) => Array.isArray(event) && event[1] === ''))
  }
})

test('C2 file picker registers before clicking, observes the chooser identity and clears its reference', async () => {
  const events = []
  const chooser = { element: () => ({ getAttribute: async (name) => name === 'accept' ? '.scr,.txt' : 'Script file' }), isMultiple: () => false }
  const runtime = { cleanup: [], evidence: {}, page: { waitForEvent: async (name, options) => {
    assert.equal(name, 'filechooser'); assert.equal(options.timeout, 15_000); events.push('listen'); return chooser
  } } }
  await activateCensusFileChooser(runtime, { click: async () => events.push('click') })
  assert.deepEqual(events, ['listen', 'click'])
  await assertCensusEffect({ assertion: { target: 'script-file-picker' } }, runtime, {}, {}, censusAssertions)
  chooser.isMultiple = () => true
  await assert.rejects(assertCensusEffect({ assertion: { target: 'script-file-picker' } }, runtime, {}, {}, censusAssertions), assert.AssertionError)
  await runtime.cleanup[0]()
  assert.equal(runtime.censusFileChooser, undefined)
  assert.equal(runtime.evidence.censusFilePicker.cleaned, true)
})

test('C2 busy recipes reuse worker transport cleanup that releases every queued real reply', async () => {
  const originalWorker = globalThis.Worker
  const originalTransport = globalThis.__walkEngineTransport
  let listener
  const events = []
  globalThis.Worker = class {
    addEventListener(_type, callback) { listener = callback }
    removeEventListener(_type, callback) { assert.equal(callback, listener); events.push('removed') }
  }
  try {
    const runtime = { page: { addInitScript: async (install) => install(), evaluate: async (read) => read() }, cleanup: [], evidence: {} }
    await setupStep({}, runtime, { kind: 'prepare-engine-transport' }, censusAssertions)
    const worker = new globalThis.Worker('/engine/worker-browser.js')
    const delivered = (event) => events.push(event.data)
    worker.addEventListener('message', delivered)
    globalThis.__walkEngineTransport.hold = true
    listener({ data: 'actual worker reply' })
    assert.deepEqual(events, [])
    assert.equal(globalThis.__walkEngineTransport.pending.length, 1)
    await runtime.cleanup[0]()
    await runtime.cleanup[0]()
    assert.equal(globalThis.__walkEngineTransport.hold, false)
    assert.equal(globalThis.__walkEngineTransport.pending.length, 0)
    worker.removeEventListener('message', delivered)
    assert.deepEqual(events, ['actual worker reply', 'removed'])
  } finally {
    globalThis.Worker = originalWorker
    globalThis.__walkEngineTransport = originalTransport
  }
})

test('C2 download-only page uses the public demo and restores identity before closing', async () => {
  const events = []
  const originalStorage = globalThis.localStorage
  const storage = new Map([['leaf.jwt', 'original']])
  globalThis.localStorage = { getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) }
  try {
    const fact = { visible: true }
    fact.getByRole = () => fact; fact.filter = () => fact
    const fresh = { addInitScript: async (script, arg) => script(arg), goto: async (url) => {
      assert.equal(storage.has('leaf.jwt'), false); events.push(url)
    }, getByRole: () => fact, evaluate: async (script, arg) => script(arg),
    close: async () => {
      assert.equal(storage.get('leaf.jwt'), 'original')
      assert.equal(storage.has('leaf.coach.dismissed.v1'), false)
      events.push('closed')
    } }
    const ordinary = { context: () => ({ newPage: async () => fresh,
      storageState: async () => ({ origins: [{ localStorage: [{ name: 'leaf.jwt', value: 'original' }] }] }) }) }
    const runtime = { page: ordinary, cleanup: [], evidence: {} }
    await setupCensusDownloadOnly(runtime, censusAssertions)
    assert.equal(runtime.page, fresh)
    await runtime.cleanup[0]()
    assert.equal(runtime.page, ordinary)
    assert.equal(runtime.evidence.censusDownloadOnly.cleaned, true)
    assert.deepEqual(events, ['/app?demo=1&surface=cad', 'closed'])
  } finally { globalThis.localStorage = originalStorage }
})

test('C2 edit and script oracles require the exact count delta, completion and opposite history step', async () => {
  for (const target of ['engine-undo-edit', 'engine-redo-edit', 'script-run']) {
    const after = target === 'engine-undo-edit' ? 4 : 6
    const count = { innerText: async () => String(after) }
    const status = { text: 'Script ran 1 command.', getAttribute: async () => 'done' }
    const history = { disabled: false }
    const page = { getByTestId: (id) => id === 'cad-edit-entity-count' ? count : status,
      getByRole: () => ({ getByRole: (_role, options) => {
        assert.equal(options.name, target === 'engine-undo-edit' ? 'Redo edit' : 'Undo edit'); return history
      } }) }
    const runtime = { page, evidence: {} }
    const probe = { assertion: { target } }
    await assertCensusEffect(probe, runtime, {}, { count: 5 }, censusAssertions)
    assert.equal(runtime.evidence.censusEdit.after, after)
    await assert.rejects(assertCensusEffect(probe, runtime, {}, { count: 15 }, censusAssertions), assert.AssertionError)
    history.disabled = true
    await assert.rejects(assertCensusEffect(probe, runtime, {}, { count: 5 }, censusAssertions), assert.AssertionError)
    if (target === 'script-run') {
      history.disabled = false; status.text = 'Script stopped.'
      await assert.rejects(assertCensusEffect(probe, runtime, {}, { count: 5 }, censusAssertions), assert.AssertionError)
    }
  }
})

test('C2 save captures the private chain and requires a successful receipt and exactly one persisted version', async () => {
  let chain = { head: 1, versions: [{ v: 1 }] }
  let receipt = { new_version: { version: 2 } }
  let ok = true
  const count = { innerText: async () => '5' }
  const status = { text: 'Saved as version 2' }
  const page = { request: { get: async (path, options) => {
    assert.equal(path, '/api/drawings/private/versions'); assert.equal(options.timeout, 15_000)
    return { ok: () => true, json: async () => chain }
  } }, getByRole: () => ({ disabled: false }),
  getByTestId: (id) => id === 'cad-edit-entity-count' ? count : { getByRole: () => status },
  waitForResponse: async (match, options) => {
    assert.equal(options.timeout, 60_000)
    const reply = (method, path) => ({ request: () => ({ method: () => method }), url: () => `http://walk${path}` })
    assert.equal(match(reply('POST', '/api/drawings/private/versions/plan')), true)
    assert.equal(match(reply('GET', '/api/drawings/private/versions/plan')), false)
    assert.equal(match(reply('POST', '/api/drawings/other/versions/plan')), false)
    return { ok: () => ok, json: async () => receipt }
  } }
  const runtime = { page, drawingId: 'private', evidence: {} }
  const probe = { locator: { role: 'button', name: 'Save version' }, assertion: { target: 'engine-save-version' } }
  const before = await captureCensusEffect(probe, runtime, censusAssertions)
  assert.equal(before.versions.head, 1)
  chain = { head: 2, versions: [{ v: 1 }, { v: 2 }] }
  await assertCensusEffect(probe, runtime, {}, before, censusAssertions)
  ok = false
  await assert.rejects(assertCensusEffect(probe, runtime, {}, before, censusAssertions), assert.AssertionError)
  ok = true; receipt = { new_version: { version: 1 } }
  await assert.rejects(assertCensusEffect(probe, runtime, {}, before, censusAssertions), assert.AssertionError)
  receipt = { new_version: { version: 2 } }; chain.versions.push({ v: 3 })
  await assert.rejects(assertCensusEffect(probe, runtime, {}, before, censusAssertions), assert.AssertionError)
})

test('one-shot route fault passes subsequent traffic and removes only its handler once', async () => {
  for (const failAbort of [false, true]) {
    let handler
    const calls = []
    const page = {
      route: async (pattern, callback) => { calls.push(pattern); handler = callback },
      unroute: async (pattern, callback) => { assert.equal(callback, handler); calls.push('unroute') },
    }
    const remove = await faultRouteOnce(page, '**/versions?*', () => calls.push('fault'))
    const route = {
      request: () => ({ url: () => 'http://walk/versions?include_deltas=1' }),
      abort: async (code) => { calls.push(code); if (failAbort) throw new Error('abort failed') },
      continue: async () => calls.push('continue'),
    }
    if (failAbort) await assert.rejects(handler(route), /abort failed/)
    else await handler(route)
    await handler(route)
    if (failAbort) {
      await assert.rejects(remove(), /abort failed/)
      await assert.rejects(remove(), /abort failed/)
    } else await Promise.all([remove(), remove()])
    assert.deepEqual(calls, ['**/versions?*', 'fault', 'failed', 'continue', 'unroute'])
  }
})

const faultAssertions = (value) => ({
  toBe: (expected) => assert.equal(value, expected),
  toBeTruthy: () => assert.ok(value),
  toBeGreaterThan: (expected) => assert.ok(value > expected),
  toBeEnabled: async () => assert.equal(value.disabled, false),
  toBeVisible: async () => assert.equal(value.visible, true),
  toBeHidden: async () => assert.equal(value.visible, false),
  toHaveCount: async (expected) => assert.equal(value.countValue, expected),
  toHaveAttribute: async (key, expected) => assert.equal(value.attributes[key], expected),
  toContainText: async (expected) => assert.ok(value.text.includes(expected)),
})
faultAssertions.poll = (callback) => ({ toBe: async (expected) => assert.equal(await callback(), expected) })

test('drawing response matcher requires exact path and method', () => {
  const match = drawingRequest('/api/drawings/private/undo', 'POST')
  const response = (path, method) => ({ url: () => `http://walk${path}`, request: () => ({ method: () => method }) })
  assert.equal(match(response('/api/drawings/private/undo', 'POST')), true)
  assert.equal(match(response('/api/drawings/private/undo', 'GET')), false)
  assert.equal(match(response('/api/drawings/other/undo', 'POST')), false)
})

test('version-change setup starts genuine undo and redo and cleanup forwards the held upstream response', async () => {
  for (const redo of [false, true]) {
    let handler
    let pending
    const events = []
    const operation = redo ? 'redo' : 'undo'
    const response = { dispose: async () => events.push('disposed') }
    const button = { disabled: false, click: async (options) => {
      assert.equal(options.timeout, 15_000)
      pending = handler({ request: () => ({ url: () => `http://walk/api/drawings/private/${operation}` }),
        fetch: async (options) => { assert.equal(options.timeout, 15_000); events.push('fetched'); return response },
        fulfill: async (options) => { assert.equal(options.response, response); events.push('forwarded') },
        continue: async () => assert.fail('mutation must reach the server before its response is held') })
    } }
    const runtime = { drawingId: 'private', cleanup: [], evidence: {}, page: {
      route: async (pattern, callback) => { assert.equal(pattern, `**/api/drawings/private/${operation}`); handler = callback },
      unroute: async (pattern, callback) => { assert.equal(callback, handler); events.push('unroute') },
      getByRole: () => ({ getByRole: (role, options) => {
        assert.equal(options.name, `${redo ? 'Redo' : 'Undo'} version`); return button
      } }),
    } }
    const setup = async (probe, actual, recipe) => {
      assert.equal(actual, runtime)
      assert.deepEqual(recipe, { kind: 'saved-version-history', redo })
      events.push('seed')
      runtime.evidence.savedVersionHistory = { head: redo ? 1 : 2, latest: 2, versions: [{ v: 1 }, { v: 2 }] }
    }
    await setupVersionFault({}, runtime, { kind: 'hold-version-change', redo }, faultAssertions, setup)
    assert.deepEqual(events, ['seed', 'fetched'])
    assert.equal(runtime.evidence.versionFault.expected, redo ? 2 : 1)
    assert.equal(runtime.cleanup.length, 1)
    await runtime.cleanup[0]()
    await pending
    await runtime.releaseVersionFault()
    assert.deepEqual(events, ['seed', 'fetched', 'forwarded', 'disposed', 'unroute'])
  }
})

test('restore fault commits through History, then closes History while the head lock remains', async () => {
  let handler
  const events = []
  const lock = { visible: true, attributes: { 'data-head': '3', role: 'alert' } }
  const response = { ok: () => true, json: async () => ({ head: 3, restored_from: 1 }) }
  const panel = { visible: true, getByTestId: (id) => {
    assert.equal(id, 'vh-row-v1')
    return { getByRole: (role, options) => ({ click: async () => {
      events.push(options.name)
      if (options.name === 'Restore v1') await handler({
        request: () => ({ url: () => 'http://walk/api/drawings/private/intake?version=head' }),
        abort: async () => events.push('abort'),
      })
    } }) }
  }, getByRole: () => ({ click: async () => { panel.visible = false; events.push('close') } }) }
  const runtime = { drawingId: 'private', cleanup: [], evidence: {}, page: {
    route: async (pattern, callback) => { assert.equal(pattern, '**/api/drawings/private/intake?version=head'); handler = callback },
    unroute: async () => events.push('unroute'),
    getByRole: () => panel, getByTestId: () => lock,
    waitForResponse: async (predicate, options) => {
      assert.equal(options.timeout, 15_000)
      assert.equal(predicate({ url: () => 'http://walk/api/drawings/private/versions/1/restore', request: () => ({ method: () => 'POST' }) }), true)
      return response
    },
  } }
  const setup = async () => { runtime.evidence.savedVersionHistory = { head: 2, latest: 2, versions: [{ v: 1 }, { v: 2 }] } }
  await setupVersionFault({}, runtime, { kind: 'fault-restored-head' }, faultAssertions, setup, async () => events.push('history'))
  assert.deepEqual(events, ['history', 'Restore', 'Restore v1', 'abort', 'close'])
  assert.equal(lock.visible, true)
  assert.equal(runtime.evidence.versionFault.expected, 3)
  await runtime.cleanup[0]()
  await runtime.cleanup[0]()
  assert.equal(events.filter((event) => event === 'unroute').length, 1)
})

test('version fault recovery removes the fault before fetching and rejects an unseated head', async () => {
  for (const kind of ['hold-version-change', 'fault-restored-head']) {
    for (const wrongVersion of [false, true]) {
      const events = []
      const fault = { kind, expected: 3, requests: ['http://walk/api/drawings/private/redo'] }
      const lock = { countValue: 0, getByRole: () => ({ click: async () => events.push('retry') }) }
      const runtime = { drawingId: 'private', evidence: { versionFault: fault },
        releaseVersionFault: async () => events.push('release'), page: {
          waitForResponse: async () => ({ ok: () => true, json: async () => ({ head: 3, version: wrongVersion ? 2 : 3, intake: {} }) }),
          getByTestId: (id) => id === 'unreadable-head-lock' ? lock : { count: async () => 0 },
          getByRole: () => ({ getByRole: () => ({ disabled: false }) }),
          locator: () => ({ text: 'Advanced to version 3' }),
        },
      }
      if (wrongVersion) await assert.rejects(finishVersionFault(runtime, faultAssertions), assert.AssertionError)
      else {
        await finishVersionFault(runtime, faultAssertions)
        assert.deepEqual(fault.recovered, { head: 3, version: 3 })
      }
      assert.deepEqual(events, kind === 'fault-restored-head' ? ['release', 'retry'] : ['release'])
    }
  }
})

test('History fault is removed before R and its oracle rejects duplicate recovery requests', async () => {
  let handler
  let observe
  let removals = 0
  let off = 0
  const node = { visible: true, countValue: 0, first() { return this } }
  const runtime = { drawingId: 'private', cleanup: [], evidence: {}, page: {
    route: async (pattern, callback) => { assert.equal(pattern, '**/api/drawings/private/versions?*'); handler = callback },
    unroute: async () => { removals++ },
    getByRole: () => ({ getByRole: () => node }),
    on: (event, callback) => { assert.equal(removals, 1); observe = callback },
    off: (event, callback) => { assert.equal(callback, observe); off++ },
  } }
  const history = async () => handler({ request: () => ({ url: () => 'http://walk/api/drawings/private/versions?include_deltas=1' }), abort: async () => {} })
  await setupHistoryFault({}, runtime, faultAssertions, history)
  assert.equal(runtime.evidence.historyFault.failed.length, 1)
  observe({ method: () => 'GET', url: () => 'http://walk/api/drawings/other/versions' })
  observe({ method: () => 'GET', url: () => 'http://walk/api/drawings/private/versions?include_deltas=1' })
  await assertHistoryRecovery(runtime, faultAssertions)
  observe({ method: () => 'GET', url: () => 'http://walk/api/drawings/private/versions?include_deltas=1' })
  await assert.rejects(assertHistoryRecovery(runtime, faultAssertions), assert.AssertionError)
  for (const cleanup of runtime.cleanup.reverse()) await cleanup()
  assert.equal(removals, 1)
  assert.equal(off, 1)
})

test('project Escape setup uses bounded org/project API calls and restores persisted org in cleanup', async () => {
  const events = []
  const board = { visible: true, getByRole: () => ({ click: async () => events.push('open-project') }) }
  const runtime = { cleanup: [], evidence: {}, page: {
    request: { post: async (path, options) => {
      assert.equal(options.timeout, 15_000)
      events.push(path)
      if (path === '/api/projects') assert.deepEqual(options.headers, { 'X-Org-Id': 'org' })
      return { ok: () => true, json: async () => path === '/api/orgs' ? { org: { org_id: 'org' } } : { project: { project_id: 'project' } } }
    } },
    evaluate: async (callback, value) => { events.push(['storage', value]); return 'previous-org' },
    reload: async (options) => { assert.equal(options.timeout, 60_000); events.push('reload') },
    getByRole: (role, options) => {
      if (role === 'region') return board
      if (role === 'dialog') return { visible: false }
      if (options.name instanceof RegExp) return { countValue: 0 }
      return { isVisible: async () => true, click: async () => {
        events.push(options.name)
        if (options.name === 'Return to drawing') board.visible = false
      } }
    },
    getByTestId: () => ({ locator: () => ({ text: 'no selection' }) }),
  } }
  // Keep the product name stable across the API and summary observation.
  let name
  const post = runtime.page.request.post
  runtime.page.request.post = async (path, options) => { name = options.data.name; return post(path, options) }
  runtime.page.locator = (selector) => selector === '.workspace-summary' ? { get text() { return name } } : { countValue: 0 }
  const assertions = (value) => ({ ...faultAssertions(value), toHaveText: async (expected) => assert.equal(value.text, expected) })
  await setupRealProject(runtime, assertions)
  assert.equal(runtime.evidence.escapeProject.projectId, 'project')
  assert.deepEqual(events.slice(0, 4), ['/api/orgs', '/api/projects', ['storage', 'org'], 'reload'])
  assert.ok(events.includes('open-project'))
  await runtime.cleanup[0]()
  assert.deepEqual(events.slice(-2), ['Close workspace', ['storage', 'previous-org']])
})

test('Solar no-saved-drawing chooses outline and selection oracles only after verified starter projection', () => {
  const map = buildFeatureMap()
  for (const op of ['create-rectangle', 'array-rect', 'move', 'rotate']) {
    const probe = resolveProbe(map.entries.find((entry) => entry.id === `action:solar-panels-${op}`), 'no-drawing')
    assert.equal(probe.setup.steps.at(-1).kind, 'require-solar-document')
    assert.equal(solarDocumentProbe(probe, { documentId: '' }), probe)
    const document = { documentId: 'solar-starter.dxf', count: 8, selection: 'no selection' }
    const seated = solarDocumentProbe(probe, document)
    assert.equal(seated.assertion.assertionId, probe.assertion.assertionId)
    assert.equal(probe.assertion.kind, 'disabled_with_reason')
    if (op === 'create-rectangle') {
      assert.equal(seated.assertion.kind, 'opens')
      assert.equal(seated.assertion.target, 'cockpit-prompt')
      assert.equal(seated.locator.name, 'Panel outline')
      assert.ok(seated.assertion.verb)
    } else {
      assert.equal(seated.assertion.reason, 'select an entity in the drawing')
      assert.equal(seated.locator.disabledVariants[0].reason, seated.assertion.reason)
    }
    for (const invalid of [{ documentId: 'foreign.dxf' }, { count: 0 }, { selection: 'sel A1' }]) {
      assert.throws(() => solarDocumentProbe(probe, { ...document, ...invalid }), /parsed starter/)
    }
  }
  const other = { kind: 'surface' }
  assert.equal(solarDocumentProbe(other, {}), other)
})

test('Author ready preflight records real policy and stack configuration, refusing only stage-off readiness', async () => {
  for (const available of [false, true]) {
    const runtime = { evidence: {}, stack: { env: { LEAF_CUSTOMIZATION_R5_MODE: 'off' } },
      testInfo: { attach: async () => {} }, page: { request: { get: async (path, options) => {
        assert.equal(path, '/api/entitlements')
        assert.equal(options.timeout, 15_000)
        return { ok: () => true, status: () => 200, json: async () => ({ availability: { author_stage: available } }) }
      } } } }
    const assertions = (value) => ({ toBe: (expected) => assert.equal(value, expected) })
    const probe = { featureId: 'action:author-tool', state: 'ready' }
    if (available) await authorAvailability(probe, runtime, assertions)
    else await assert.rejects(authorAvailability(probe, runtime, assertions), (error) => {
      assert.ok(error instanceof UnsupportedLocalError)
      assert.match(error.reason, /LEAF_CUSTOMIZATION_R5_MODE=off/)
      return true
    })
    assert.equal(runtime.evidence.authorAvailability.policy.availability.author_stage, available)
    assert.equal(runtime.evidence.authorAvailability.configuration.LEAF_CUSTOMIZATION_R5_MODE, 'off')
  }
  for (const probe of [{ featureId: 'action:author-tool', state: 'unentitled' }, { featureId: 'action:fit', state: 'ready' }]) {
    await authorAvailability(probe, { page: { request: { get: () => assert.fail('unrelated preflight') } } })
  }
})

test('policy diagnostics distinguish absent groups, collapsed overflow and visible disclosure', async () => {
  for (const [rendered, visible, disclosure] of [[0, false, null], [1, false, 'false'], [1, true, 'true']]) {
    const page = { getByRole: () => ({ getByRole: (role, options) => {
      if (role === 'group') {
        assert.equal(options.name, 'Author')
        assert.equal(options.includeHidden, true)
        return { count: async () => rendered, isVisible: async () => visible }
      }
      return { count: async () => disclosure === null ? 0 : 1, getAttribute: async () => disclosure }
    } }) }
    assert.deepEqual(await disclosureEvidence(page, 'Author'), { group: 'Author', rendered, visible, disclosure })
  }
})

test('empty surface oracles assert actual host contracts without Studio landmarks', async () => {
  for (const [state, role, name] of [['signed-out', 'heading', 'You are not signed in'],
    ['no-drawing', 'text', 'No drawing yet. Upload a DWG or DXF to begin.']]) {
    const node = { visible: true }
    const runtime = { evidence: {}, page: {
      getByRole: (actualRole, options) => { assert.equal(actualRole, role); assert.equal(options.name, name); return node },
      getByText: (text) => { assert.equal(role, 'text'); assert.equal(text, name); return node },
    } }
    await assertEffect({ state, assertion: { target: 'surface:solar' } }, runtime, null, {},
      (value) => ({ toBeVisible: async () => assert.equal(value.visible, true) }))
    assert.deepEqual(runtime.evidence.surfaceHost, { host: '/try', contract: state === 'signed-out' ? 'signed-out' : 'empty' })
  }
})

test('Solar projection hold observes real requests and releases each route once', async () => {
  let handler
  let continued = 0
  let unroutes = 0
  const runtime = { cleanup: [], evidence: {}, page: {
    route: async (pattern, callback) => { assert.equal(pattern, '**/api/drawings/*/dxf?*'); handler = callback },
    unroute: async (pattern, callback) => { assert.equal(callback, handler); unroutes++ },
  } }
  await setupStep({}, runtime, { kind: 'hold-solar-projection' })
  const url = 'http://walk/api/drawings/private/dxf?version=head'
  const pending = handler({ request: () => ({ url: () => url }), continue: async () => { continued++ } })
  await Promise.resolve()
  assert.equal(continued, 0)
  assert.deepEqual(runtime.evidence.solarProjectionRequests, [url])
  await runtime.releaseSolarProjection()
  await pending
  await runtime.cleanup[0]()
  assert.equal(continued, 1)
  assert.equal(unroutes, 1)
})

test('Solar pending setup requires a held projection, Beta badge and absent projected document', async () => {
  const badge = { attribute: 'beta' }, tab = { text: 'Solar CAD Beta', locator: () => badge }, card = { attribute: null }
  const assertions = (value) => ({
    toHaveAttribute: async (name, expected) => assert.equal(value.attribute, expected),
    toContainText: async (expected) => assert.ok(value.text.includes(expected)),
    not: { toHaveAttribute: async () => assert.equal(value.attribute, null) },
  })
  assertions.poll = (callback) => ({ toBeGreaterThan: async (expected) => assert.ok(await callback() > expected) })
  const runtime = { releaseSolarProjection: async () => {}, evidence: { solarProjectionRequests: ['held.dxf'] }, page: {
    request: { get: async (path, options) => {
      assert.equal(path, '/api/health'); assert.equal(options.timeout, 15_000)
      return { ok: () => true, json: async () => ({ aps_live: false }) }
    } },
    getByRole: () => ({ getByRole: () => tab }), locator: () => card,
  } }
  const recipe = { kind: 'require-surface-context', surface: 'solar', context: { solarReady: false } }
  await setupStep({}, runtime, recipe, assertions)
  assert.equal(runtime.evidence.solarReadiness.pending, 'Beta')
  runtime.evidence.solarProjectionRequests = []
  await assert.rejects(setupStep({}, runtime, recipe, assertions), assert.AssertionError)
  runtime.evidence.solarProjectionRequests = ['held.dxf']
  badge.attribute = 'available'
  await assert.rejects(setupStep({}, runtime, recipe, assertions), assert.AssertionError)
  badge.attribute = 'beta'; card.attribute = 'already-projected.dxf'
  await assert.rejects(setupStep({}, runtime, recipe, assertions), assert.AssertionError)
})

test('phone Rail availability records absent product groups without relabeling a drawer control', async () => {
  for (const rendered of [0, 1]) {
    const runtime = { evidence: {}, testInfo: { project: { name: 'phone' }, attach: async () => {} }, page: {
      getByRole: () => ({ getByRole: (role, options) => role === 'group'
        ? { count: async () => rendered, isVisible: async () => !!rendered }
        : { count: async () => 1, getAttribute: async () => 'true' } }),
    } }
    const probe = { featureId: 'action:rail-expand', state: 'ready' }
    if (rendered) await phoneRailAvailability(probe, runtime)
    else await assert.rejects(phoneRailAvailability(probe, runtime), (error) => {
      assert.ok(error instanceof UnsupportedLocalError)
      assert.match(error.reason, /wideViewport/)
      return true
    })
    assert.equal(runtime.evidence.phoneRail.rendered, rendered)
    assert.equal(runtime.evidence.phoneRail.disclosure, 'true')
  }
  await phoneRailAvailability({ featureId: 'action:fit' }, {})
  await phoneRailAvailability({ featureId: 'action:rail-expand' }, { testInfo: { project: { name: 'desktop' } } })
})

test('Solar readiness releases transport before requiring the real head projection and Ready badge', async () => {
  const events = []
  const card = { attribute: 'private-v2.dxf' }, badge = { attribute: 'available' }
  const locator = { text: 'Solar CAD Ready', locator: () => badge }
  const runtime = { drawingId: 'private', evidence: { solarReadiness: { pending: 'Beta' } },
    releaseSolarProjection: async () => events.push('release'), page: { locator: () => card } }
  const assertions = (value) => ({
    toHaveAttribute: async (name, expected) => {
      if (expected instanceof RegExp) assert.match(value.attribute, expected)
      else assert.equal(value.attribute, expected)
    },
    toContainText: async (expected) => assert.ok(value.text.includes(expected)),
  })
  const workspace = async () => { assert.deepEqual(events, ['release']); events.push('projected') }
  await completeSolarReadiness({}, runtime, locator, assertions, workspace)
  assert.deepEqual(events, ['release', 'projected'])
  assert.deepEqual(runtime.evidence.solarReadiness, { pending: 'Beta', released: 'Ready' })
  events.length = 0; card.attribute = 'solar-starter.dxf'
  await assert.rejects(completeSolarReadiness({}, runtime, locator, assertions, workspace), assert.AssertionError)
  events.length = 0; card.attribute = 'private-v2.dxf'; badge.attribute = 'beta'
  await assert.rejects(completeSolarReadiness({}, runtime, locator, assertions, workspace), assert.AssertionError)
  await completeSolarReadiness({}, {}, null, assertions, () => assert.fail('ordinary surface must not release'))
})

test('phone Properties oracle follows Plan drawer state for both toggle directions', async () => {
  for (const visible of [false, true]) {
    const runtime = { evidence: {}, testInfo: { project: { name: 'phone' } }, page: {
      getByRole: (role, options) => {
        assert.equal(role, 'group'); assert.equal(options.name, 'Workspace panels')
        return { getByRole: (role, options) => { assert.equal(options.name, 'Plan'); return { expanded: String(!visible) } } }
      },
    } }
    await assertEffect({ assertion: { target: 'properties-pane' } }, runtime, null, { visible }, (value) => ({
      toHaveAttribute: async (name, expected) => assert.equal(value.expanded, expected),
    }))
    assert.deepEqual(runtime.evidence.phoneProperties, { host: 'Plan', before: visible, after: !visible })
  }
})


test('engine mode oracle checks provider publication, persistence and the other mode', async () => {
  for (const mode of ['ortho', 'osnap']) {
    const initial = { live: true, ortho: false, osnap: true }
    const expected = { ...initial, [mode]: !initial[mode] }
    const tabs = []
    const locator = { pressed: String(expected[mode]) }
    const runtime = { initialEngineModes: initial, evidence: {}, page: {
      evaluate: async () => ({ ...expected }),
      getByRole: (role, options) => {
        assert.equal(role, 'tablist')
        assert.equal(options.name, 'Ribbon')
        return { getByRole: (role, options) => ({ click: async () => tabs.push(options.name) }) }
      },
    } }
    const assertions = (value) => ({
      toBe: (other) => assert.equal(value, other),
      toEqual: (other) => assert.deepEqual(value, other),
      toHaveAttribute: async (name, other) => {
        assert.equal(name, 'aria-pressed')
        assert.equal(value.pressed, other)
      },
    })
    assertions.poll = (callback) => ({ toEqual: async (other) => assert.deepEqual(await callback(), other) })
    const probe = { assertion: { target: 'engine-mode:' + mode, value: expected[mode] } }
    await assertEngineMode(probe, runtime, locator, assertions)
    assert.deepEqual(tabs, ['View', 'Draw'])
    assert.deepEqual(runtime.evidence.engineMode, { mode, expected, observed: expected })
    runtime.page.evaluate = async () => ({ ...expected, [mode]: initial[mode] })
    await assert.rejects(assertEngineMode(probe, runtime, locator, assertions), /Expected values/)
  }
})

// Exercise the real runner seams without booting the worker-stack fixture.
const expect = (value) => ({
  toBe: (expected) => assert.equal(value, expected),
  toBeTruthy: () => assert.ok(value),
  toBeGreaterThan: (expected) => assert.ok(value > expected),
  toBeVisible: async () => assert.equal(value.visible, true),
  toBeHidden: async () => assert.equal(value.visible, false),
  toHaveCount: async (expected) => assert.equal(value.countValue, expected),
})

test('failed drawing setup accepts an empty canvas and requires the product no-drawing signal on each surface', async () => {
  for (const surface of ['cad', 'solar', 'browser', 'ios']) {
    const undo = { visible: true, disabled: true }
    const page = {
      goto: async (url) => assert.equal(url, `/app?surface=${surface}&drawing=missing.invalid`),
      waitForResponse: async (predicate) => {
        const response = { url: () => 'http://walk/api/session?dwg=missing.invalid',
          status: () => 404, json: async () => ({ error: 'missing drawing' }) }
        assert.equal(predicate(response), true)
        return response
      },
      locator: () => assert.fail('canvas count cannot establish drawing readiness'),
      getByRole: (role, options) => {
        if (role === 'alert') return { filter: () => ({ visible: true }) }
        assert.equal(role, 'toolbar')
        assert.deepEqual(options, { name: 'Quick access', exact: true })
        return { getByRole: (role, options) => {
          assert.equal(role, 'button')
          assert.deepEqual(options, { name: 'Undo version (unavailable: no versioned drawing)', exact: true })
          return undo
        } }
      },
    }
    const assertions = (value) => ({ ...expect(value),
      toContain: (expected) => assert.ok(value.includes(expected)),
      toBeDisabled: async () => assert.equal(value.disabled, true),
    })
    const runtime = { page, evidence: {} }
    await setupStep({}, runtime, { kind: 'open-failed-drawing',
      url: `/app?surface=${surface}&drawing=missing.invalid` }, assertions)
    assert.equal(runtime.failedDrawing, true)
    assert.equal(runtime.evidence.failedDrawing.status, 404)
    undo.disabled = false
    await assert.rejects(requireNoDrawing(page, assertions), assert.AssertionError)
    undo.disabled = true
    undo.visible = false
    await assert.rejects(requireNoDrawing(page, assertions), assert.AssertionError)
  }
})

test('ready Solar catalog setup requires the browser drawing query and exact enabled tool', async () => {
  for (const [name, familyId, placement, surface, tab] of [
    ['solar-settings', 'stringing', undefined, 'solar', 'Manage'],
    ['solar-string-data', 'stringing', undefined, 'solar', 'Manage'],
    ['solar-settings', 'solar-unit', undefined, 'solar', 'Solar'],
    ['solar-settings', 'solar-unit', undefined, 'cad', 'Manage'],
    ['solar-settings', 'solar-unit', undefined, null, 'Manage'],
    ['solar-settings', 'solar-unit', { tab: 'view' }, 'solar', 'View'],
    ['solar-settings', 'solar-unit', { tab: 'view' }, 'cad', 'View'],
  ]) {
    const events = []
    const button = { visible: true, enabled: true }
    const page = {
      locator: (selector) => {
        assert.equal(selector, '.app')
        return { first: () => ({ getAttribute: async (name) => {
          assert.equal(name, 'data-surface')
          return surface
        } }) }
      },
      waitForResponse: async (predicate, options) => {
        assert.equal(options.timeout, 15_000)
        const response = (query) => ({ url: () => `http://walk/api/capabilities${query}`,
          request: () => ({ method: () => 'GET' }), ok: () => true })
        assert.equal(predicate(response('')), false)
        assert.equal(predicate(response('?drawing_id=other')), false)
        assert.equal(predicate(response('?drawing_id=private&drawing_version=2')), true)
        events.push('browser catalog')
        return response('?drawing_id=private&drawing_version=2')
      },
      goto: async (url) => {
        assert.equal(url, '/app?drawing=private&surface=solar')
        events.push('private drawing')
      },
      request: { get: async (path, options) => {
        assert.equal(options.timeout, 15_000)
        assert.equal(path, '/api/capabilities?drawing_id=private&drawing_version=2')
        return { ok: () => true, json: async () => ({ families: [{ family_id: familyId, label: 'Stringing', capabilities: [{ name, placement,
          availability: { entitled: true, engine_ready: true, input_ready: true, implemented: true } }] }] }) }
      } },
      getByRole: (role, options) => {
        if (role === 'tablist') return { getByRole: (_role, options) => ({ click: async () => events.push(options.name) }) }
        if (role === 'button') {
          assert.equal(options.name, 'More panels')
          return { isVisible: async () => false }
        }
        assert.equal(role, 'toolbar')
        assert.equal(options.name, 'Drafting tools')
        return { getByRole: (role, options) => {
          if (role === 'status') return { filter: () => ({ isVisible: async () => false }) }
          assert.equal(role, 'group')
          assert.equal(options.name, 'Stringing')
          return { getByRole: (role, options) => {
            assert.equal(role, 'button')
            assert.deepEqual(options, { name, exact: true })
            events.push('exact tool')
            return button
          } }
        } }
      },
    }
    const assertions = (value) => ({
      toBe: (expected) => assert.equal(value, expected),
      toBeVisible: async () => { events.push('visible'); assert.equal(value.visible, true) },
      toBeEnabled: async () => { events.push('enabled'); assert.equal(value.enabled, true) },
    })
    const probe = { kind: 'tool', sourceId: name, state: 'ready', assertion: { kind: 'opens', target: 'catalog-run-decision' } }
    const runtime = { page, drawingId: 'private', drawingVersion: 2, evidence: {}, recipeAssertions: assertions }
    await setupStep(probe, runtime, { kind: 'catalog-tool', name })
    assert.deepEqual(events, [tab, 'browser catalog', 'private drawing', tab, 'exact tool', 'visible', 'enabled'])
    assert.equal(runtime.ribbonTab, tab)
    assert.equal(runtime.evidence.browserCatalog.drawingId, 'private')
    button.enabled = false
    await assert.rejects(setupStep(probe, runtime, { kind: 'catalog-tool', name }), assert.AssertionError)
    button.enabled = true
    button.visible = false
    await assert.rejects(setupStep(probe, runtime, { kind: 'catalog-tool', name }), assert.AssertionError)
    button.visible = true
    page.waitForResponse = async () => { throw new Error('No browser drawing query') }
    await assert.rejects(setupStep(probe, runtime, { kind: 'catalog-tool', name }), /No browser drawing query/)
  }
})

test('catalog tools choose their first tab from the active surface without a readiness reload', async () => {
  for (const name of ['solar-unit-sync', 'solar-nec-conduit-fill']) {
    for (const [surface, familyId, placement, tab] of [
      ['solar', 'solar-unit', undefined, 'Solar'],
      ['solar', 'stringing', undefined, 'Manage'],
      ['cad', 'solar-unit', undefined, 'Manage'],
      [null, 'solar-unit', undefined, 'Manage'],
      ['solar', 'solar-unit', { tab: 'view' }, 'View'],
      ['cad', 'solar-unit', { tab: 'view' }, 'View'],
    ]) {
      const tabs = []
      const page = {
        locator: (selector) => {
          assert.equal(selector, '.app')
          return { first: () => ({ getAttribute: async (attribute) => {
            assert.equal(attribute, 'data-surface')
            return surface
          } }) }
        },
        request: { get: async (path, options) => {
          assert.equal(path, '/api/capabilities?drawing_id=private&drawing_version=head')
          assert.equal(options.timeout, 15_000)
          return { ok: () => true, json: async () => ({ families: [
            { family_id: familyId, label: 'Tools', capabilities: [{ name, placement }] },
          ] }) }
        } },
        getByRole: (role, options) => {
          if (role === 'tablist') {
            assert.equal(options.name, 'Ribbon')
            return { getByRole: (role, options) => {
              assert.equal(role, 'tab')
              return { click: async () => tabs.push(options.name) }
            } }
          }
          if (role === 'toolbar') return { getByRole: () => ({ filter: () => ({ isVisible: async () => false }) }) }
          assert.equal(role, 'button')
          assert.equal(options.name, 'More panels')
          return { isVisible: async () => false }
        },
        goto: () => assert.fail('this tool must keep the seated workspace'),
        waitForResponse: () => assert.fail('this tool does not require a browser catalog reload'),
      }
      const runtime = { page, drawingId: 'private', evidence: {} }
      await setupStep({ kind: 'tool', sourceId: name, state: 'ready' }, runtime, { kind: 'catalog-tool', name })
      assert.deepEqual(tabs, [tab])
      assert.equal(runtime.ribbonTab, tab)
      assert.equal(runtime.evidence.browserCatalog, undefined)
    }
  }
})

function fakePage() {
  const events = []
  let historyOpen = false
  let previewed = false
  const locator = (role, options = {}) => {
    const name = options.name
    return {
      getByRole: locator,
      filter: () => ({ isVisible: async () => false }),
      get visible() {
        if (role === 'dialog') return historyOpen
        if (name === 'More panels') return false
        if (role === 'text') return previewed && name.test('Viewing v1 of 2, read-only preview')
        return true
      },
      isVisible: async () => name !== 'More panels',
      click: async (bounds) => {
        assert.equal(bounds.timeout, 15_000)
        events.push({ role, name, bounds })
        if (name === 'history') historyOpen = true
        if (name instanceof RegExp) {
          assert.ok(name.test('v1'))
          assert.ok(!name.test('v2'))
          previewed = true
        }
        if (name === 'Close version history') historyOpen = false
      },
    }
  }
  let reads = 0
  const reply = (body) => ({ ok: () => true, json: async () => body })
  const page = {
    events, getByRole: locator, getByText: (name) => locator('text', { name }),
    reload: async (bounds) => { assert.equal(bounds.timeout, 60_000); events.push({ reload: true }) },
    request: {
      get: async (url, bounds) => {
        assert.equal(bounds.timeout, 15_000)
        assert.equal(url, '/api/drawings/private/versions')
        return reply(++reads === 1 ? { head: 1 } : { head: 2, versions: [{ v: 2 }, { v: 1 }] })
      },
      post: async (url, bounds) => {
        assert.equal(bounds.timeout, 15_000)
        assert.equal(url, '/api/drawings/private/versions/1/restore')
        return reply({ head: 2, new_version: { version: 2 }, restored_from: 1 })
      },
    },
  }
  return page
}

test('versionless probes attach unsupported evidence before requests, setup or activation', async () => {
  for (const kind of ['action', 'tool']) {
    const attachments = []
    const runtime = { evidence: { steps: [] }, page: {},
      runStep: async () => assert.fail('unreachable state must not run setup'),
      testInfo: { annotations: [], attach: async (name, attachment) => {
        attachments.push({ name, body: JSON.parse(attachment.body.toString()) })
      } } }
    const probe = { featureId: `${kind}:versionless`, kind, state: 'no-versioned-drawing', certify: 'local',
      setup: { steps: [{ kind: 'open-private-drawing' }] }, assertion: { assertionId: 'versionless/oracle' } }
    const started = Date.now()
    assert.deepEqual(await runProbe(probe, runtime), { unsupported: true, reason: VERSIONLESS_DRAWING_REASON })
    assert.ok(Date.now() - started < 5_000)
    assert.equal(runtime.evidence.result.result, 'unsupported_local')
    assert.equal(runtime.evidence.result.reason, VERSIONLESS_DRAWING_REASON)
    assert.equal(runtime.evidence.setupCompleted, undefined)
    assert.equal(runtime.evidence.oracleReached, undefined)
    assert.deepEqual(runtime.evidence.steps, [])
    assert.equal(runtime.evidence.cleanupCompleted, true)
    assert.deepEqual(attachments, [{ name: 'walk-result', body: runtime.evidence.result }])
    assert.deepEqual(runtime.testInfo.annotations, [{ type: 'unsupported_local', description: VERSIONLESS_DRAWING_REASON }])
  }
})

test('Solar panel selection declares only an observed calibration failure after setup starts', async () => {
  const attachments = []
  const runtime = { evidence: { steps: [] }, page: {},
    runStep: async (name, callback) => {
      assert.equal(name, 'Setup: select-entity')
      await callback()
    },
    testInfo: { annotations: [], attach: async (name, attachment) => {
      attachments.push({ name, body: JSON.parse(attachment.body.toString()) })
    } } }
  const probe = { featureId: 'action:solar-panels-move', kind: 'action', state: 'ready', certify: 'local',
    locator: { group: 'solar-panels' },
    setup: { steps: [{ kind: 'select-entity', type: 'LINE', editable: true }] },
    assertion: { assertionId: 'solar-panels-move/oracle' } }
  // Classification is deliberately recipe-, surface- and exact-message-specific.
  const recipe = probe.setup.steps[0]
  const error = new Error('The drawing needs two uncovered calibration points')
  assert.equal(solarCalibrationFailure(probe, recipe, error), true)
  assert.equal(solarCalibrationFailure({ ...probe, locator: { group: 'modify' } }, recipe, error), false)
  assert.equal(solarCalibrationFailure(probe, { ...recipe, viewerOnly: true }, error), false)
  // A missing page seam is an ordinary setup error, never an early Solar declaration.
  await assert.rejects(runProbe(probe, runtime))
  assert.equal(runtime.evidence.result, undefined)
  assert.equal(runtime.evidence.setupCompleted, undefined)
  assert.equal(runtime.evidence.oracleReached, undefined)
  assert.deepEqual(runtime.evidence.steps, [])
  assert.equal(runtime.evidence.cleanupCompleted, true)
  assert.deepEqual(attachments, [])
  assert.deepEqual(runtime.testInfo.annotations, [])
})

test('ready redo restores a saved version, undoes it and checks the real head before seating the workspace', async () => {
  const calls = []
  let reads = 0
  const reply = (body) => ({ ok: () => true, json: async () => body })
  const runtime = { drawingId: 'private', evidence: {}, page: { request: {
    get: async (url, bounds) => {
      calls.push(url)
      assert.equal(bounds.timeout, 15_000)
      // A concurrent write consumed the redo: successful mutation receipts
      // alone cannot establish readiness for the version-navigation oracle.
      return reply(++reads === 1 ? { head: 1 } : { head: 2, latest: 2, versions: [{ v: 1 }, { v: 2 }] })
    },
    post: async (url, bounds) => {
      calls.push(url)
      assert.equal(bounds.timeout, 15_000)
      return reply({ head: 2, new_version: { version: 2 } })
    },
  } } }
  await assert.rejects(setupStep({}, runtime, { kind: 'saved-version-history', redo: true }, expect))
  assert.deepEqual(calls, ['/api/drawings/private/versions', '/api/drawings/private/versions/1/restore',
    '/api/drawings/private/undo', '/api/drawings/private/versions'])
  assert.equal(runtime.evidence.savedVersionHistory, undefined)
})

test('read-only uses the registry History name, explicitly previews non-head and restores the tab', async () => {
  const page = fakePage()
  const runtime = { page, evidence: {}, drawingId: 'private', ribbonTab: 'Manage' }
  await previewVersion({}, runtime, expect, async () => {})
  assert.equal(page.events.find((event) => event.role === 'button' && event.name === 'history').name,
    accessibleName(ACTIONS.find((action) => action.id === 'history').label))
  assert.deepEqual(runtime.evidence.versionPreview, { head: 2, preview: 1, restoredFrom: 1 })
  assert.equal(runtime.drawingVersion, 1)
  assert.equal(runtime.ribbonTab, 'Manage')
  assert.equal(page.events.at(-1).name, 'Manage')
  assert.ok(page.events.find((event) => event.reload))
})

test('History failure is bounded and stops setup before preview', async () => {
  const page = fakePage()
  const original = page.getByRole
  page.getByRole = (role, options) => {
    const result = original(role, options)
    if (role === 'toolbar') result.getByRole = (_, button) => {
      assert.equal(button.name, 'history')
      return { click: async (bounds) => {
        assert.equal(bounds.timeout, 15_000)
        throw new Error('History click timed out')
      } }
    }
    return result
  }
  await assert.rejects(openHistory({}, { page, evidence: {} }, expect), /timed out/)
})

test('job setup requires accepted submission and visible pending UI, then restores the feature tab', async () => {
  const page = fakePage()
  page.request.get = async () => ({ ok: () => true, json: async () => ({ families: [{ capabilities: [{ name: 'count-by-layer' }] }] }) })
  page.route = async () => {}
  page.unroute = async () => {}
  let status = 202
  let pending = true
  page.waitForResponse = (predicate, bounds) => {
    assert.equal(bounds.timeout, 15_000)
    const response = { request: () => ({ method: () => 'POST' }), url: () => 'http://localhost/api/run',
      status: () => status, json: async () => ({ job_id: 'real-job' }) }
    assert.equal(predicate(response), true)
    return Promise.resolve(response)
  }
  page.locator = (selector) => {
    assert.equal(selector, '.strip-running')
    return { get visible() { return pending } }
  }
  const runtime = { page, evidence: {}, cleanup: [], ribbonTab: 'View', catalogPanelName: 'Original panel', workerFacts: {} }
  await startPendingRun({}, runtime, expect)
  assert.deepEqual(runtime.evidence.pendingRun, { tool: 'count-by-layer', jobId: 'real-job', status: 202, pendingVisible: true })
  assert.equal(runtime.ribbonTab, 'View')
  assert.equal(runtime.catalogPanelName, 'Original panel')
  await runtime.cleanup.pop()()
  status = 200
  await assert.rejects(startPendingRun({}, runtime, expect))
  assert.equal(runtime.catalogPanelName, 'Original panel')
  await runtime.cleanup.pop()()
  status = 202
  pending = false
  await assert.rejects(startPendingRun({}, runtime, expect))
  assert.equal(runtime.catalogPanelName, 'Original panel')
  await runtime.cleanup.pop()()
})

test('cleanup drains several outstanding routes exactly once before unroute, including pass-through arrivals', async () => {
  let handler
  let unroutes = 0
  const counts = [0, 0, 0, 0]
  let finish
  const slow = new Promise((resolve) => { finish = resolve })
  const page = {
    route: async (_, callback) => { handler = callback },
    unroute: async (_, callback) => {
      assert.equal(callback, handler)
      assert.deepEqual(counts, [1, 1, 1, 1])
      unroutes++
    },
  }
  const cleanup = await holdJobRoutes(page)
  const route = (index) => ({ continue: async () => {
    assert.equal(++counts[index], 1)
    if (index === 0) await slow
  } })
  const outstanding = [0, 1, 2].map((index) => handler(route(index)))
  assert.deepEqual(counts, [0, 0, 0, 0])
  const draining = cleanup()
  assert.equal(cleanup(), draining, 'cleanup is idempotent')
  await handler(route(3))
  assert.equal(unroutes, 0)
  finish()
  await draining
  await Promise.all(outstanding)
  assert.equal(unroutes, 1)
})

test('submission setup failure still drains routes through runProbe finally', async () => {
  let continued = 0
  let unroute = 0
  let outstanding
  const page = fakePage()
  page.request.get = async () => ({ ok: () => true, json: async () => ({ families: [{ capabilities: [{ name: 'count-by-layer' }] }] }) })
  page.route = async (_, handler) => { outstanding = handler({ continue: async () => { continued++ } }) }
  page.unroute = async () => { assert.equal(continued, 1); unroute++ }
  const original = page.getByRole
  page.getByRole = (role, options) => {
    if (options.name === 'Run count-by-layer') return { click: async (bounds) => {
      assert.equal(bounds.timeout, 15_000)
      throw new Error('submission failed')
    } }
    return original(role, options)
  }
  page.waitForResponse = (_, bounds) => {
    assert.equal(bounds.timeout, 15_000)
    return Promise.reject(new Error('response timed out'))
  }
  const probe = { kind: 'action', featureId: 'action:undo', state: 'job-running',
    setup: { steps: [{ kind: 'start-pending-run' }] }, assertion: { assertionId: 'undo/running' } }
  await assert.rejects(runProbe(probe, { page, evidence: { steps: [] }, testInfo: { annotations: [] },
    recipeAssertions: expect, runStep: async (_, callback) => callback() }), /submission failed/)
  await outstanding
  assert.equal(continued, 1)
  assert.equal(unroute, 1)
})

test('running Escape requires the detach message and removal of the running strip', async () => {
  const runtime = { evidence: { pendingRun: { jobId: 'pending-id' } }, testInfo: { project: { name: 'desktop' } }, page: {
    request: { get: async () => ({ ok: () => true, json: async () => ({ jobs: [{ job_id: 'pending-id' }] }) }) },
    getByRole: () => ({ isVisible: async () => false, visible: true }),
    locator: (selector) => {
      if (selector === '.strip-running') return { countValue: 0 }
      if (selector === 'aside.rail .rail-ledger') return { countValue: 1 }
      if (selector === 'aside.rail') return { getByText: () => ({ first: () => ({ visible: true }) }) }
      assert.equal(selector, '.toast[role="status"]')
      return { getByText: (text, options) => {
      assert.equal(text, 'Stopped following count-by-layer. It keeps running; find it in Jobs.')
      assert.equal(options.exact, true)
      return { visible: true }
      } }
    },
  } }
  await assertEffect({ assertion: { target: 'escape:running' } }, runtime, {}, {}, expect)
  assert.equal(runtime.evidence.pendingRun.detached, true)
  const getJobs = runtime.page.request.get
  runtime.page.request.get = async () => ({ ok: () => true, json: async () => ({ jobs: [] }) })
  await assert.rejects(assertEffect({ assertion: { target: 'escape:running' } }, runtime, {}, {}, expect))
  runtime.page.request.get = getJobs
  runtime.page.locator = () => ({ countValue: 1 })
  await assert.rejects(assertEffect({ assertion: { target: 'escape:running' } }, runtime, {}, {}, expect))
})

const oracleAssertions = (value) => ({ ...expect(value),
  toEqual: (expected) => assert.deepEqual(value, expected),
  toBeEnabled: async () => assert.equal(value.disabled, false),
  toHaveValue: async (expected) => assert.equal(await value.inputValue(), expected),
  toHaveText: async (expected) => assert.equal(await value.innerText(), expected),
  toContainText: async (expected) => assert.ok(value.text.includes(expected)),
  toHaveAccessibleName: async (expected) => assert.equal(value.name, expected),
})
oracleAssertions.poll = (read) => ({ toBe: async (expected) => assert.equal(await read(), expected) })

test('Jobs desktop setup expands and collapses the rail without querying phone panels', async () => {
  let open = false
  const page = { getByRole: (role, options) => {
    assert.equal(role, 'button')
    const collapse = options.name === 'Collapse the job monitor to a spine'
    if (!collapse) assert.ok(options.name.test('Expand the job monitor (2 live)'))
    return { isVisible: async () => collapse === open, get visible() { return collapse === open }, click: async () => { open = !open } }
  }, locator: (selector) => {
    assert.equal(selector, 'aside.rail .rail-ledger')
    return { get countValue() { return open ? 1 : 0 } }
  } }
  for (const desired of [false, true, true, false, false]) {
    await setJobRail(page, desired, false, expect)
    assert.equal(open, desired)
  }
})

test('available solve proposal does not wait on the settings-form browser catalog', async () => {
  assert.equal(solarBrowserCatalogRequired('solar-solve-proposal'), false)
  for (const name of ['solar-settings', 'solar-string-data', 'solar-autofill']) assert.equal(solarBrowserCatalogRequired(name), true)
  const record = { name: 'solar-solve-proposal', availability: { entitled: true, engine_ready: true, input_ready: true, implemented: true } }
  const runtime = { drawingId: 'private', evidence: {}, page: {
    locator: () => ({ first: () => ({ getAttribute: async () => null }) }),
    waitForResponse: () => assert.fail('available solve must not require the flag-gated browser fetch'),
    goto: () => assert.fail('available solve must keep the already seated workspace'),
    request: { get: async () => ({ ok: () => true, json: async () => ({ families: [{ label: 'Solar', capabilities: [record] }] }) }) },
    getByRole: (role) => {
      if (role === 'tablist') return { getByRole: () => ({ click: async () => {} }) }
      if (role === 'toolbar') return { getByRole: () => ({ filter: () => ({ isVisible: async () => false }) }) }
      return { isVisible: async () => false }
    },
  } }
  await setupStep({ kind: 'tool', sourceId: record.name, state: 'ready', assertion: { kind: 'opens', target: 'catalog-run-decision' } },
    runtime, { kind: 'catalog-tool', name: record.name }, oracleAssertions)
  assert.deepEqual(runtime.evidence.catalog.record, record)
  const button = { visible: true }
  runtime.page.getByRole = () => button
  runtime.evidence.responses = []
  runtime.catalogRunRequests = []
  await assertEffect({ sourceId: record.name, assertion: { target: 'catalog-run-decision', tool: record.name } }, runtime, {}, {}, oracleAssertions)
  runtime.catalogRunRequests.push('/api/run')
  await assert.rejects(assertEffect({ sourceId: record.name, assertion: { target: 'catalog-run-decision', tool: record.name } }, runtime, {}, {}, oracleAssertions))
  runtime.catalogRunRequests = []
  runtime.evidence.responses.push({ method: 'POST', url: 'http://localhost/api/run' })
  await assert.rejects(assertEffect({ sourceId: record.name, assertion: { target: 'catalog-run-decision', tool: record.name } }, runtime, {}, {}, oracleAssertions))
})

test('no-rung keyboard actions preserve the workspace, refuse dispatch and leave entry enabled', async () => {
  for (const [featureId, key] of [['action:bar-escape', 'Escape'], ['action:bar-retry', 'r']]) {
    let value = ''
    let state = { url: '/private', selection: [], surfaces: [] }
    let requestObserver
    const input = { disabled: false, inputValue: async () => value, fill: async (text) => { value = text } }
    const page = {
      on: (_, callback) => { requestObserver = callback }, off: () => {},
      evaluate: async (callback) => callback.toString().includes('requestAnimationFrame') ? undefined : structuredClone(state),
      getByRole: (role) => role === 'combobox' ? input : { getByRole: () => ({ first: () => ({ focus: async () => {} }) }) },
      keyboard: { press: async (pressed) => assert.equal(pressed, key) },
    }
    const probe = { featureId, kind: 'action', state: 'ready', locator: { trigger: 'keyboard', key }, assertion: { kind: 'disabled_with_reason' } }
    assert.equal(barNoRung(probe), true)
    assert.equal(barNoRung({ ...probe, state: 'job-running' }), false)
    const runtime = { page, evidence: {}, cleanup: [] }
    const before = await captureBarNoRung(runtime)
    await assertBarNoRung(probe, runtime, input, before, oracleAssertions)
    assert.equal(value, '')
    requestObserver({ method: () => 'POST', url: () => 'http://walk/api/run' })
    await assert.rejects(assertBarNoRung(probe, runtime, input, before, oracleAssertions))
    runtime.noRungDispatches = []
    state.selection = ['changed']
    await assert.rejects(assertBarNoRung(probe, runtime, input, before, oracleAssertions))
    state = before.state
    input.disabled = true
    await assert.rejects(assertBarNoRung(probe, runtime, input, before, oracleAssertions))
  }
})

test('geometry observer copies complete real replies and forwards worker traffic unchanged', () => {
  const original = globalThis.Worker
  const calls = []
  let listener
  globalThis.Worker = class {
    addEventListener(type, callback) { assert.equal(type, 'message'); listener = callback }
    postMessage(...args) { calls.push(args); return 'forwarded' }
  }
  try {
    installGeometryObserver()
    const worker = new Worker('/engine/worker-browser.js', { type: 'module' })
    const message = { type: 'edit', op: 'explode' }
    const transfer = []
    assert.equal(worker.postMessage(message, transfer), 'forwarded')
    assert.equal(calls[0][0], message)
    assert.equal(calls[0][1], transfer)
    const entities = [{ id: '1', type: 'LINE', vertices: [[0, 0], [10, 0]] }]
    listener({ data: { type: 'documentLoaded', documentId: 'head.dxf', entities } })
    entities[0].vertices[0][0] = 99
    assert.equal(globalThis.__walkGeometry.geometry.entities[0].vertices[0][0], 0)
    assert.deepEqual(globalThis.__walkGeometry.dispatches, [{ type: 'edit', op: 'explode' }])
    listener({ data: { type: 'error', entities: [] } })
    assert.equal(globalThis.__walkGeometry.geometry.documentId, 'head.dxf')
  } finally { globalThis.Worker = original; delete globalThis.__walkGeometry }
})

function refusalRuntime() {
  const state = { geometry: { documentId: 'private-v1.dxf', entities: [{ id: '1', type: 'DIMENSION', vertices: [[0, 0]] }] },
    selection: 'DIMENSION #1', history: [{ name: 'Undo edit (unavailable: nothing to undo)', disabled: true }],
    clipboard: { name: 'paste (unavailable: nothing on the clipboard yet)', title: 'empty', disabled: true },
    versions: { head: 1, versions: [{ v: 1 }] }, dispatches: [], status: 'expected refusal' }
  const node = { innerText: async () => state.selection, getByRole: () => ({ innerText: async () => state.status }) }
  const runtime = { drawingId: 'private', evidence: {}, page: {
    evaluate: async (callback) => structuredClone(callback.toString().includes('dispatches') ? state.dispatches : state.geometry),
    getByTestId: () => node,
    getByRole: (role) => ({ getByRole: () => role === 'toolbar'
      ? { evaluateAll: async () => structuredClone(state.history) }
      : { evaluate: async () => structuredClone(state.clipboard) } }),
    request: { get: async () => ({ ok: () => true, json: async () => structuredClone(state.versions) }) },
  } }
  return { runtime, state }
}

test('engine refusal requires exact text and preserves geometry, selection, history, clipboard and dispatch', async () => {
  for (const field of ['status', 'geometry', 'selection', 'history', 'clipboard', 'versions', 'dispatches']) {
    const { runtime, state } = refusalRuntime()
    const before = await captureEngineRefusal(runtime)
    const probe = { assertion: { refusal: 'expected refusal' } }
    await assertEngineRefusal(probe, runtime, before, oracleAssertions)
    if (field === 'status' || field === 'selection') state[field] = 'unexpected'
    else if (field === 'geometry') state.geometry.entities[0].vertices[0][0] = 100
    else if (field === 'history') state.history[0].disabled = false
    else if (field === 'clipboard') state.clipboard.name += 'changed'
    else if (field === 'versions') state.versions.head = 2
    else state.dispatches.push({ type: 'edit' })
    await assert.rejects(assertEngineRefusal(probe, runtime, before, oracleAssertions))
  }
})

test('version navigation requires the adjacent server head and seated identity even with unchanged geometry', async () => {
  const versions = { head: 4, versions: [{ v: 9 }, { v: 1 }, { v: 4 }] }
  assert.equal(expectedVersionHead({ sourceId: 'undo' }, versions), 1)
  assert.equal(expectedVersionHead({ sourceId: 'redo' }, versions), 9)
  assert.throws(() => expectedVersionHead({ sourceId: 'redo' }, { head: 9, versions: versions.versions }), /No redo/)
  const before = { versions, count: 1, geometry: { entities: [{ id: '1', type: 'LINE', vertices: [[0, 0], [10, 0]] }] } }
  for (const sourceId of ['undo', 'redo']) {
    const head = sourceId === 'undo' ? 1 : 9
    let seated = head
    let observedHead = head
    let entities = structuredClone(before.geometry.entities)
    const runtime = { drawingId: 'private', evidence: {}, page: {
      request: { get: async () => ({ ok: () => true, json: async () => ({ head: observedHead }) }) },
      locator: () => ({ get text() { return `private-v${seated}.dxf` } }),
      getByTestId: () => ({ innerText: async () => '1' }),
      evaluate: async () => ({ documentId: `private-v${seated}.dxf`, entities }),
    } }
    await assertVersionTransition({ sourceId }, runtime, before, oracleAssertions)
    seated = 4
    await assert.rejects(assertVersionTransition({ sourceId }, runtime, before, oracleAssertions))
    seated = head; observedHead = 4
    await assert.rejects(assertVersionTransition({ sourceId }, runtime, before, oracleAssertions))
    observedHead = head; entities = [{ id: '1', type: 'LINE', vertices: [[0, 0], [99, 0]] }]
    await assert.rejects(assertVersionTransition({ sourceId }, runtime, before, oracleAssertions))
  }
})

test('zoom-out baseline zooms past saturation and refuses a viewport that never becomes measurable', async () => {
  let width = 6576
  let clicks = 0
  let stuck = false
  const page = { getByRole: (role) => role === 'button'
    ? { locator: () => ({ evaluate: async () => ({ width: 100, height: 100 }) }) }
    : { getByRole: () => ({ click: async () => { clicks++; if (!stuck) width /= 2 } }) } }
  const bounds = async () => ({ x: 0, y: 0, width, height: width })
  const baseline = await establishZoomBaseline(page, bounds)
  assert.ok(clicks > 1)
  assert.ok(baseline.viewport.width < 40)
  assert.equal(baseline.unsaturated, true)
  width = 6576; clicks = 0; stuck = true
  await assert.rejects(establishZoomBaseline(page, bounds), /unsaturated/)
  assert.equal(clicks, 30)
})

test('copy verifies a paste at the known base and detects corrupt copied or untouched geometry', async () => {
  for (const fault of [null, 'copy', 'untouched']) {
    const original = { id: '1', type: 'LINE', layer: 'Walk', vertices: [[111, 190, 0], [333, 190, 0]] }
    const before = { count: 1, geometry: { documentId: 'private-v1.dxf', entities: [original] } }
    let geometry = structuredClone(before.geometry)
    let count = 1
    const page = {
      evaluate: async () => structuredClone(geometry),
      getByRole: () => ({ getByRole: () => ({ click: async () => {} }) }),
      getByLabel: (_, options) => ({ fill: async (value) => { assert.equal(options.exact, true); assert.equal(value, '400,100') } }),
      getByTestId: (id) => id === 'cockpit-prompt' ? { name: 'PASTE command' }
        : id === 'cad-edit-entity-count' ? { innerText: async () => String(count) }
          : { click: async () => {
            count = 2
            geometry.entities.push({ id: '2', type: 'LINE', layer: 'Walk', vertices: [[400, 100, 0], [fault === 'copy' ? 623 : 622, 100, 0]] })
            if (fault === 'untouched') geometry.entities[0].vertices[0][0]++
          } },
    }
    const runtime = { page, evidence: {} }
    if (fault) await assert.rejects(assertCopiedGeometry({}, runtime, before, oracleAssertions))
    else {
      await assertCopiedGeometry({}, runtime, before, oracleAssertions)
      assert.deepEqual(runtime.evidence.clipboardPaste.base, [400, 100])
    }
  }
})

test('Explode checks exact segment geometry and preserves every other entity', async () => {
  const source = { id: String(0xA200), type: 'LWPOLYLINE', layer: 'Walk', vertices: [[111, 200, 0], [222, 200, 0], [222, 210, 0]] }
  const other = { id: '1', index: 1, type: 'LINE', layer: 'Walk', vertices: [[0, 0, 0], [10, 0, 0]] }
  const before = { count: 2, geometry: { entities: [other, source] } }
  const parts = [
    { id: '2', type: 'LINE', layer: 'Walk', vertices: [[111, 200, 0], [222, 200, 0]] },
    { id: '3', type: 'LINE', layer: 'Walk', vertices: [[222, 200, 0], [222, 210, 0]] },
  ]
  for (const fault of [null, 'segments', 'untouched', 'id', 'type', 'layer', 'source-retained']) {
    const entities = structuredClone([other, ...parts])
    entities[0].index = 0
    if (fault === 'segments') entities[2].vertices[1][1]++
    if (fault === 'untouched') entities[0].vertices[0][0]++
    if (fault === 'id') entities[0].id = 'changed'
    if (fault === 'type') entities[0].type = 'LWPOLYLINE'
    if (fault === 'layer') entities[0].layer = 'changed'
    if (fault === 'source-retained') entities[1] = source
    const runtime = { page: { getByTestId: () => ({ innerText: async () => '3' }), evaluate: async () => ({ entities }) } }
    const check = () => assertEffect({ assertion: { target: 'engine:explode' } }, runtime, {}, before, oracleAssertions)
    if (fault) await assert.rejects(check())
    else await check()
  }
})
