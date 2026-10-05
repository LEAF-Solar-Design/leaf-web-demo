import test from 'node:test'
import assert from 'node:assert/strict'
import { ACTIONS, accessibleName } from '../../src/lib/actionRegistry.js'
import { holdJobRoutes, openHistory, previewVersion, startPendingRun, runProbe, assertEffect, setupStep, requireNoDrawing, VERSIONLESS_DRAWING_REASON, solarCalibrationFailure,
  assertEngineMode, setJobRail, solarBrowserCatalogRequired, barNoRung, captureBarNoRung, assertBarNoRung, installGeometryObserver,
  captureEngineRefusal, assertEngineRefusal, assertCopiedGeometry, expectedVersionHead, assertVersionTransition, establishZoomBaseline } from './fixtures.mjs'
import { solarDocumentProbe, authorAvailability, disclosureEvidence, phoneRailAvailability, completeSolarReadiness, UnsupportedLocalError } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

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
    waitForResponse: () => assert.fail('available solve must not require the flag-gated browser fetch'),
    goto: () => assert.fail('available solve must keep the already seated workspace'),
    request: { get: async () => ({ ok: () => true, json: async () => ({ families: [{ label: 'Solar', capabilities: [record] }] }) }) },
    getByRole: (role) => {
      if (role === 'tablist') return { getByRole: () => ({ click: async () => {} }) }
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
  const other = { id: '1', type: 'LINE', vertices: [[0, 0, 0], [10, 0, 0]] }
  const before = { count: 2, geometry: { entities: [other, source] } }
  const parts = [
    { id: '2', type: 'LINE', layer: 'Walk', vertices: [[111, 200, 0], [222, 200, 0]] },
    { id: '3', type: 'LINE', layer: 'Walk', vertices: [[222, 200, 0], [222, 210, 0]] },
  ]
  for (const fault of [null, 'segments', 'untouched', 'source-retained']) {
    const entities = structuredClone([other, ...parts])
    if (fault === 'segments') entities[2].vertices[1][1]++
    if (fault === 'untouched') entities[0].vertices[0][0]++
    if (fault === 'source-retained') entities[1] = source
    const runtime = { page: { getByTestId: () => ({ innerText: async () => '3' }), evaluate: async () => ({ entities }) } }
    const check = () => assertEffect({ assertion: { target: 'engine:explode' } }, runtime, {}, before, oracleAssertions)
    if (fault) await assert.rejects(check())
    else await check()
  }
})
