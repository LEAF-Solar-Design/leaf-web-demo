import test from 'node:test'
import assert from 'node:assert/strict'
import { ACTIONS, accessibleName } from '../../src/lib/actionRegistry.js'
import { holdJobRoutes, openHistory, previewVersion, startPendingRun, runProbe, assertEffect, setupStep, requireNoDrawing, VERSIONLESS_DRAWING_REASON, solarCalibrationFailure } from './fixtures.mjs'

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
  for (const name of ['solar-settings', 'solar-string-data']) {
    const events = []
    const button = { visible: true, enabled: true }
    const page = {
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
      request: { get: async (path) => {
        assert.equal(path, '/api/capabilities?drawing_id=private&drawing_version=2')
        return { ok: () => true, json: async () => ({ families: [{ label: 'Stringing', capabilities: [{ name,
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
    assert.deepEqual(events, ['Manage', 'browser catalog', 'private drawing', 'Manage', 'exact tool', 'visible', 'enabled'])
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

function fakePage() {
  const events = []
  let historyOpen = false
  let previewed = false
  const locator = (role, options = {}) => {
    const name = options.name
    return {
      getByRole: locator,
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
  const runtime = { evidence: { pendingRun: {} }, page: {
    getByText: (text, options) => {
      assert.equal(text, 'Stopped following count-by-layer. It keeps running; find it in Jobs.')
      assert.equal(options.exact, true)
      return { visible: true }
    },
    locator: (selector) => { assert.equal(selector, '.strip-running'); return { countValue: 0 } },
  } }
  await assertEffect({ assertion: { target: 'escape:running' } }, runtime, {}, {}, expect)
  assert.equal(runtime.evidence.pendingRun.detached, true)
  runtime.page.locator = () => ({ countValue: 1 })
  await assert.rejects(assertEffect({ assertion: { target: 'escape:running' } }, runtime, {}, {}, expect))
})
