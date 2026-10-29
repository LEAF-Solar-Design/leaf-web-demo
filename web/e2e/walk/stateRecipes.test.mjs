import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { EventEmitter } from 'node:events'
import { ACTIONS, accessibleName } from '../../src/lib/actionRegistry.js'
import { holdJobRoutes, openHistory, previewVersion, startPendingRun, runProbe, assertEffect, setupStep, requireNoDrawing, VERSIONLESS_DRAWING_REASON, solarCalibrationFailure,
  assertEngineMode, setJobRail, solarBrowserCatalogRequired, barNoRung, captureBarNoRung, assertBarNoRung, installGeometryObserver,
  captureEngineRefusal, assertEngineRefusal, assertCopiedGeometry, expectedVersionHead, assertVersionTransition, establishZoomBaseline } from './fixtures.mjs'
import { solarDocumentProbe, authorAvailability, disclosureEvidence, phoneRailAvailability, completeSolarReadiness, UnsupportedLocalError, recoverFailedCatalog } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'
import { captureMultipleSelection, assertMultipleSelectionDelete } from './fixtures.mjs'
import { seedSolarChain } from './fixtures.mjs'
import { solarChainSteps } from './probes.mjs'
import { projectSolarResponse, projectSolarStep, switchProjectSolarForms, assertProjectSolarDrawing, observeProjectSolarResponses, setupRequestHeaders, observeProbeRequests } from './fixtures.mjs'
import { inspectJob, inspectContext } from '../local/solarProjectWalk.mjs'
import { expect as walkExpect, selectProjectSolarVersion, openProjectSolarTool,
  takeProjectSolarCheckout, publishProjectSolar } from './fixtures.mjs'
import { canonicalProjectVersion, proposalCatalogState, proposalFormValues, refusalCode,
  stagingAction, stagingRead, stagingContext, stagingRun, reviewStagingProposal } from '../walk-staging/projectSolarSteps.mjs'
import { monitorStagingSolar, withStagingProject, stagingNetworkPath, stagingErrorCode,
  observeStagingNetwork, stagingNetworkFailure, stagingEvidenceStep,
  appendStagingRequest, appendStagingConsole } from '../walk-staging/projectSolarSteps.mjs'
import { redactSolarDiagnostic, projectSolarRunDiagnostic, walkClick, walkFill, walkSelect, walkEnabled,
  enterWorkspaceIfChooser, enterWorkspaceEnvironment, reloadAndReenter, openSolarCadProfile, STUDIO_RAIL_FEATURES, revealStudioRail, prepareStudioRail } from './fixtures.mjs'
import { stagingDelivery, observeDeliveryHealth, createLocalAncestry, deliveryRepo, decideStagingDelivery } from './stagingDelivery.mjs'
import { absenceNamePattern, resolveSurfaceControl, assertSurfaceState, setPhonePanel, readPhonePanel, assertPhonePanel } from './fixtures.mjs'
import { setStudioDrawer, studioDrawerLocator, assertStudioDrawer, studioDrawerProbeLocator,
  setToolRail, railExpandLauncher, assertRailExpandFamily } from './fixtures.mjs'
import { ACCESSIBILITY_DIALOG_SCOPE, accessibilityDialogTargets, mergeAccessibilityMeasurements, addPersonaFragment,
  prepareAccessibilityCapture, followAccessibilityCapture } from './fixtures.mjs'
import { accessibility, mobileTouch } from './lenses/index.mjs'

function accessibilityWalkFixture({ tabOutside = false, tabThrows = false, modal = true } = {}) {
  const calls = [], cleanup = [], child = {}, outside = {}
  const opener = { isConnected: true, disposed: 0, async dispose() { this.disposed++ } }
  const root = { isConnected: true, visible: true, disposed: 0,
    contains: node => node === child,
    getAttribute: name => { assert.equal(name, 'aria-modal'); return String(modal) },
    async isVisible() { return this.visible },
    async dispose() { this.disposed++ } }
  const document = { activeElement: opener }
  const locatorFor = handle => ({ count: async () => 1, isVisible: async () => handle.visible !== false,
    elementHandle: async () => handle,
    ariaSnapshot: async () => '- dialog "Linked services":\n  - button "Close"' })
  const locator = locatorFor(opener), dialog = locatorFor(root)
  const page = {
    getByRole(role, options) {
      assert.equal(role, 'dialog')
      assert.deepEqual(options, { name: 'Linked services', exact: true })
      return dialog
    },
    evaluate: async (callback, argument) => {
      const previous = Object.getOwnPropertyDescriptor(globalThis, 'document')
      Object.defineProperty(globalThis, 'document', { value: document, configurable: true })
      try { return callback(argument) } finally {
        if (previous) Object.defineProperty(globalThis, 'document', previous)
        else delete globalThis.document
      }
    },
    keyboard: { press: async key => {
      calls.push(key)
      if (key === 'Tab') {
        if (tabThrows) throw new Error('keyboard unavailable')
        document.activeElement = tabOutside ? outside : child
      } else {
        assert.equal(key, 'Escape')
        root.visible = false
        document.activeElement = opener
      }
    } },
  }
  const probe = { featureId: 'control:linked-services', state: 'ready' }
  const mobile = { reference: 'mobile-touch:control:linked-services[ready]@phone',
    input: { touch: true, coarsePointer: true }, viewport: { width: 390, height: 844 },
    control: { reachable: true, clipped: false, rect: { x: 10, y: 20, width: 44, height: 44 } } }
  const evidence = { schema: 'leaf.walk-evidence.v1', featureId: probe.featureId,
    state: probe.state, viewport: 'desktop',
    result: { result: 'passed', featureId: probe.featureId, state: probe.state } }
  addPersonaFragment(evidence, { persona: 'mobile-touch', measurements: structuredClone(mobile),
    bundleReference: `walk:${probe.featureId}[${probe.state}]@desktop` })
  return { page, probe, locator, evidence, cleanup, calls, mobile, root, opener }
}

test('dialog follow-on feeds the real accessibility lens and preserves mobile measurements', async () => {
  const fake = accessibilityWalkFixture()
  const capture = await prepareAccessibilityCapture({ ...fake, viewport: 'desktop' })
  await followAccessibilityCapture({ ...fake, capture })
  assert.equal(accessibility.evaluate(fake.evidence).status, 'pass')
  assert.equal(fake.evidence.measurements.reference, 'walk:control:linked-services[ready]@desktop')
  for (const [key, value] of Object.entries(fake.mobile)) {
    if (key !== 'reference') assert.deepEqual(fake.evidence.measurements[key], value)
  }
  assert.deepEqual(fake.evidence.measurementSources, [
    { persona: 'mobile-touch', reference: fake.mobile.reference, missing: false },
    { persona: 'accessibility', reference: 'accessibility:control:linked-services[ready]@desktop', missing: false },
  ])
  assert.deepEqual(fake.calls, ['Tab', 'Escape'])
  for (const dispose of fake.cleanup) await dispose()
  assert.ok(fake.root.disposed > 0)
  assert.ok(fake.opener.disposed > 0)
  assert.deepEqual(mergeAccessibilityMeasurements({ focus: { prior: 'retained' }, other: 7 },
    { focus: { observed: false, absent: undefined }, overlay: { modal: false } }),
  { focus: { prior: 'retained', observed: false }, other: 7, overlay: { modal: false } })
})

test('mobile-touch and accessibility fragments preserve both passing persona verdicts', async () => {
  const fake = accessibilityWalkFixture()
  fake.evidence.viewport = 'phone'
  const capture = await prepareAccessibilityCapture({ ...fake, viewport: 'desktop' })
  await followAccessibilityCapture({ ...fake, capture })
  assert.equal(fake.evidence.measurements.reference, 'walk:control:linked-services[ready]@phone')
  assert.equal(mobileTouch.evaluate(fake.evidence).status, 'pass')
  assert.equal(accessibility.evaluate(fake.evidence).status, 'pass')
  assert.deepEqual(fake.evidence.measurementConflicts, [])
  for (const dispose of fake.cleanup) await dispose()
})

test('persona fragments remove disagreeing fields and replace earlier observations for one persona', () => {
  const evidence = {}, bundleReference = 'walk:control:linked-services[ready]@phone'
  const add = (persona, measurements) => addPersonaFragment(evidence, { persona, measurements, bundleReference })
  add('mobile-touch', { reference: 'mobile-touch:probe[ready]@phone', control: { reachable: true, clipped: false } })
  add('accessibility', { reference: 'accessibility:probe[ready]@phone', control: { reachable: false } })
  assert.equal(Object.hasOwn(evidence.measurements.control, 'reachable'), false)
  assert.equal(evidence.measurements.control.clipped, false)
  assert.deepEqual(evidence.measurementConflicts, ['control.reachable'])
  add('accessibility', { reference: 'accessibility:probe[ready]@phone', control: { reachable: true } })
  assert.equal(evidence.personaFragments.length, 2)
  assert.equal(evidence.measurements.control.reachable, true)
  assert.deepEqual(evidence.measurementConflicts, [])
})

test('a credential-shaped fragment is dropped without losing the other persona or exposing values', () => {
  const evidence = {}, bundleReference = 'walk:control:linked-services[ready]@phone'
  const measurements = { reference: 'mobile-touch:probe[ready]@phone', control: { reachable: true } }
  addPersonaFragment(evidence, { persona: 'mobile-touch', measurements, bundleReference })
  const before = structuredClone(evidence.measurements)
  assert.doesNotThrow(() => addPersonaFragment(evidence, { persona: 'accessibility', bundleReference,
    measurements: { reference: 'accessibility:probe[ready]@phone', overlay: { name: ['Bea', 'rer synthetic-private-value'].join('') } } }))
  assert.deepEqual(evidence.measurements, before)
  assert.deepEqual(evidence.personaFragments, [{ persona: 'mobile-touch', measurements }])
  assert.deepEqual(evidence.measurementSources,
    [{ persona: 'mobile-touch', reference: measurements.reference, missing: false }])
  assert.deepEqual(evidence.measurementDiagnostics, [{ message: 'Optional persona measurement fragment rejected' }])
  assert.equal(JSON.stringify(evidence).includes('synthetic-private-value'), false)
})

test('a credential-shaped replacement preserves the previous fragment and its source', () => {
  const evidence = {}, bundleReference = 'walk:control:linked-services[ready]@phone'
  const measurements = { reference: 'mobile-touch:probe[ready]@phone', control: { reachable: true, clipped: false } }
  addPersonaFragment(evidence, { persona: 'mobile-touch', measurements, bundleReference })
  const fragments = evidence.personaFragments
  const before = structuredClone(evidence)
  assert.doesNotThrow(() => addPersonaFragment(evidence, { persona: 'mobile-touch', bundleReference,
    measurements: { reference: 'mobile-touch:replacement[ready]@phone', control: { name: ['Bea', 'rer synthetic-private-value'].join('') } } }))
  assert.equal(evidence.personaFragments, fragments)
  assert.deepEqual(evidence.personaFragments, before.personaFragments)
  assert.deepEqual(evidence.measurements, before.measurements)
  assert.deepEqual(evidence.measurementSources, before.measurementSources)
  assert.deepEqual(evidence.measurementConflicts, before.measurementConflicts)
  assert.deepEqual(evidence.measurementDiagnostics, [{ message: 'Optional persona measurement fragment rejected' }])
  assert.equal(JSON.stringify(evidence).includes('synthetic-private-value'), false)
})

test('a valid mobile-touch replacement updates the fragment, measurements, and source', () => {
  const evidence = {}, bundleReference = 'walk:control:linked-services[ready]@phone'
  addPersonaFragment(evidence, { persona: 'mobile-touch', bundleReference,
    measurements: { reference: 'mobile-touch:probe[ready]@phone', control: { reachable: true, clipped: false } } })
  const measurements = { reference: 'mobile-touch:replacement[ready]@phone', control: { reachable: false } }
  assert.doesNotThrow(() => addPersonaFragment(evidence, { persona: 'mobile-touch', measurements, bundleReference }))
  assert.deepEqual(evidence.personaFragments, [{ persona: 'mobile-touch', measurements }])
  assert.deepEqual(evidence.measurements, { reference: bundleReference, control: { reachable: false } })
  assert.deepEqual(evidence.measurementSources,
    [{ persona: 'mobile-touch', reference: measurements.reference, missing: false }])
  assert.deepEqual(evidence.measurementConflicts, [])
  assert.equal(evidence.measurementDiagnostics, undefined)
})

test('a measured Tab leaving the dialog yields a real accessibility finding', async () => {
  const fake = accessibilityWalkFixture({ tabOutside: true })
  const capture = await prepareAccessibilityCapture({ ...fake, viewport: 'desktop' })
  await followAccessibilityCapture({ ...fake, capture })
  assert.equal(fake.evidence.measurements.focus.tabRemainsInside, false)
  assert.equal(accessibility.evaluate(fake.evidence).status, 'finding')
  for (const dispose of fake.cleanup) await dispose()
})

test('a failed follow-on retains only observed fields, disposes, and preserves the functional verdict', async () => {
  const fake = accessibilityWalkFixture({ tabThrows: true })
  const verdict = structuredClone(fake.evidence.result)
  const capture = await prepareAccessibilityCapture({ ...fake, viewport: 'desktop' })
  await followAccessibilityCapture({ ...fake, capture })
  assert.equal(fake.evidence.measurements.overlay.role, 'dialog')
  for (const key of ['tabRemainsInside', 'escapeClosed', 'returnTarget', 'actualReturnTarget']) {
    assert.equal(Object.hasOwn(fake.evidence.measurements.focus, key), false)
  }
  assert.equal(accessibility.evaluate(fake.evidence).status, 'unknown')
  assert.deepEqual(fake.evidence.result, verdict)
  assert.ok(fake.evidence.accessibilityDiagnostics.some(item => item.phase === 'tab'))
  assert.deepEqual(fake.calls, ['Tab'])
  for (const dispose of fake.cleanup) await dispose()
  assert.ok(fake.root.disposed > 0)
  assert.ok(fake.opener.disposed > 0)
})

test('only the declared desktop dialog states can create an accessibility capture', async () => {
  assert.deepEqual(Object.keys(ACCESSIBILITY_DIALOG_SCOPE), ['control:linked-services', 'control:claude-accounts',
    'control:session-details', 'control:version-history', 'action:bar-shortcuts'])
  for (const [featureId, name, states] of [
    ['control:linked-services', 'Linked services', ['ready', 'failed-load']],
    ['control:claude-accounts', 'Claude accounts', ['ready', 'failed-load']],
    ['control:session-details', 'Session · provenance', ['ready', 'failed-load']],
    ['control:version-history', 'Version history', ['ready']],
  ]) {
    assert.deepEqual(ACCESSIBILITY_DIALOG_SCOPE[featureId].states, states)
    for (const state of states) {
      const locator = {}, dialog = {}
      const targets = await accessibilityDialogTargets({ viewport: 'desktop', probe: { featureId, state }, locator,
        page: { getByRole: (role, options) => {
          assert.deepEqual([role, options], ['dialog', { name, exact: true }])
          return dialog
        } } })
      assert.deepEqual(targets, { openTarget: { key: featureId, locator },
        returnTarget: { key: featureId, locator }, dialog })
    }
  }
  for (const [viewport, featureId, state] of [
    ['desktop', 'control:cost-panel', 'ready'], ['desktop', 'control:version-history', 'failed-load'],
    ['desktop', 'action:bar-shortcuts', 'failed-load'],
    ...Object.keys(ACCESSIBILITY_DIALOG_SCOPE).flatMap(id => ['ready', 'failed-load'].map(state => ['phone', id, state])),
  ]) {
    const fake = accessibilityWalkFixture()
    const probe = { featureId, state }
    assert.equal(await accessibilityDialogTargets({ ...fake, viewport, probe }), null)
    assert.equal(await prepareAccessibilityCapture({ ...fake, viewport, probe,
      createCapture: () => assert.fail('out-of-scope probe created capture') }), null)
    assert.deepEqual(fake.cleanup, [])
    assert.equal(Object.hasOwn(fake.evidence.measurements, 'overlay'), false)
  }
})

test('shortcuts declares the persistent control activation focuses without changing observed non-modal semantics', async () => {
  for (const visible of [true, false]) {
    const project = { isVisible: async () => visible }, tab = {}
    const page = { getByRole: (role, options) => {
      if (role === 'navigation') {
        assert.equal(options.name, 'Workspace controls')
        return { getByRole: () => project }
      }
      if (role === 'tablist') return { getByRole: () => ({ first: () => tab }) }
      assert.deepEqual([role, options], ['dialog', { name: 'Keyboard shortcuts', exact: true }])
      return { dialog: true }
    } }
    const targets = await accessibilityDialogTargets({ page, viewport: 'desktop',
      probe: { featureId: 'action:bar-shortcuts', state: 'ready' }, locator: {} })
    assert.equal(targets.openTarget.locator, visible ? project : tab)
    assert.deepEqual(targets.returnTarget, targets.openTarget)
  }
  const fake = accessibilityWalkFixture({ modal: false })
  const capture = await prepareAccessibilityCapture({ ...fake, viewport: 'desktop' })
  await followAccessibilityCapture({ ...fake, capture })
  assert.equal(fake.evidence.measurements.overlay.modal, false)
  assert.equal(accessibility.evaluate(fake.evidence).status, 'finding')
  for (const dispose of fake.cleanup) await dispose()
})

const stagingBase = 'https://platform-staging.leafdesign.ai'
const stagingIds = [1, 2, 3, 4, 5, 6].map(id => `00000000-0000-4000-8000-${String(id).padStart(12, '0')}`)
const deliveryExpected = 'a'.repeat(40), deliveryOld = 'b'.repeat(40), deliveryWeb = 'c'.repeat(40)
const deliveryEnv = { LEAF_E2E_EXPECTED_SHA: deliveryExpected }
function deliveryResponse(path, body, status = 200, extra = {}) {
  const bytes = Buffer.from(typeof body === 'string' ? body : JSON.stringify(body))
  let read = false
  return { url: `${stagingBase}${path}`, status, redirected: false,
    body: { getReader: () => ({ read: async () => {
      if (read) return { done: true }
      read = true
      return { done: false, value: bytes }
    }, cancel: async () => {} }) }, ...extra }
}
function deliveryFetch(app = { source_sha: deliveryOld }, web = { source_sha: deliveryWeb }) {
  return async url => {
    const path = new URL(url).pathname
    return deliveryResponse(path, path === '/api/health' ? app : web)
  }
}
const deliveryOptions = (extra = {}) => ({ env: deliveryEnv, fetchImpl: deliveryFetch(), ancestry: async () => 'ancestor', ...extra })

test('staging delivery reads both public identities without credentials', async () => {
  const calls = []
  const evidence = await stagingDelivery(stagingBase, deliveryOptions({ fetchImpl: async (url, options) => {
    calls.push(url)
    assert.deepEqual(Object.keys(options).sort(), ['cache', 'credentials', 'method', 'redirect', 'signal'])
    assert.equal(options.method, 'GET')
    assert.equal(options.credentials, 'omit')
    assert.equal(options.redirect, 'error')
    assert.equal(options.cache, 'no-store')
    assert.equal(options.signal.aborted, false)
    return deliveryFetch()(url)
  } }))
  assert.deepEqual(calls, [`${stagingBase}/api/health`, `${stagingBase}/health.json`])
  assert.equal(evidence.expected_sha, deliveryExpected)
  assert.equal(evidence.app.source_sha, deliveryOld)
  assert.equal(evidence.web.source_sha, deliveryWeb)
  assert.equal(evidence.schema, 'leaf.staging-delivery.v1')
  assert.match(evidence.checkedAt, /Z$/)
})

test('staging delivery skips only proven older components', async () => {
  for (const app of ['equal', 'ancestor', 'not_ancestor', 'unknown', 'absent', 'invalid']) {
    for (const web of ['equal', 'ancestor', 'not_ancestor', 'unknown', 'absent', 'invalid']) {
      const source = (relation, old) => relation === 'equal' ? deliveryExpected
        : relation === 'absent' ? undefined : relation === 'invalid' ? 'private-short-sha' : old
      const result = await stagingDelivery(stagingBase, deliveryOptions({
        fetchImpl: deliveryFetch({ source_sha: source(app, deliveryOld) }, { source_sha: source(web, deliveryWeb) }),
        ancestry: async served => served === deliveryOld ? app : web,
      }))
      const eligible = [app, web].every(value => ['equal', 'ancestor'].includes(value))
      assert.equal(result.decision === 'stale', eligible && (app === 'ancestor' || web === 'ancestor'), `${app}/${web}`)
      if (app === 'equal' && web === 'equal') assert.equal(result.decision, 'current')
      assert.doesNotMatch(JSON.stringify(result), /private-short-sha/)
    }
  }
  assert.equal(decideStagingDelivery({ relation: 'equal' }, { relation: 'not_ancestor' }), 'mismatch')
})

test('staging delivery does not mistake image adoption for a missing release', async () => {
  const rows = [
    [{ source_identity: 'adopted', release_source_sha: deliveryExpected }, false],
    [{ source_identity: 'build_unknown', release_source_sha: deliveryExpected }, false],
    [{ source_identity: 'built', release_source_sha: deliveryExpected }, false],
    [{ source_identity: 'built', release_source_sha: deliveryOld }, true],
    [{ source_identity: 'unattested', release_source_sha: null }, true],
    [{}, true], [{ source_identity: 'private-invalid', release_source_sha: 'private-invalid' }, false],
    [{ source_identity: 'unattested', release_source_sha: 'private-invalid' }, false],
    [{ source_identity: 'built' }, false], [{ release_source_sha: deliveryOld }, false],
  ]
  for (const [metadata, eligible] of rows) {
    const result = await stagingDelivery(stagingBase, deliveryOptions({
      fetchImpl: deliveryFetch({ source_sha: deliveryOld, ...metadata }),
    }))
    assert.equal(result.decision, eligible ? 'stale' : 'unverified', JSON.stringify(metadata))
    assert.equal(result.app.source_sha, deliveryOld)
    assert.doesNotMatch(JSON.stringify(result), /private-invalid/)
  }
})

test('staging delivery local ancestry is offline and bounded', async () => {
  for (const [exit, shallow, wanted] of [[0, 'true', 'ancestor'], [1, 'false', 'not_ancestor'],
    [1, 'true', 'unknown'], [2, 'false', 'unknown'], ['ETIMEDOUT', 'false', 'unknown'], ['ENOENT', 'false', 'unknown']]) {
    const calls = []
    const compare = createLocalAncestry({ now: () => 100, run: async (args, options) => {
      calls.push(args)
      assert.equal(options.cwd, deliveryRepo)
      assert.equal(options.shell, false)
      assert.equal(options.timeout, 2000)
      assert.equal(options.maxBuffer, 4096)
      assert.equal(options.env.GIT_NO_LAZY_FETCH, '1')
      assert.equal(options.env.GIT_NO_REPLACE_OBJECTS, '1')
      return { code: args[0] === 'merge-base' ? exit : 0, stdout: shallow }
    } })
    assert.equal(await compare(deliveryOld, deliveryExpected), wanted)
    assert.deepEqual(calls.slice(0, 3), [['cat-file', '-e', `${deliveryOld}^{commit}`],
      ['cat-file', '-e', `${deliveryExpected}^{commit}`], ['merge-base', '--is-ancestor', deliveryOld, deliveryExpected]])
    assert.deepEqual(calls.slice(3), exit === 1 ? [['rev-parse', '--is-shallow-repository']] : [])
  }
  let clock = 0, calls = 0
  const bounded = createLocalAncestry({ now: () => clock, run: async (args, options) => {
    assert.equal(options.timeout, calls === 0 ? 2000 : 800)
    calls++
    clock += 1200
    return { code: 0 }
  } })
  assert.equal(await bounded(deliveryOld, deliveryExpected), 'unknown')
  assert.equal(await bounded(deliveryWeb, deliveryExpected), 'unknown')
  assert.equal(calls, 2)
  for (const run of [async () => ({ code: 128 }), async () => { throw new Error('missing executable private') }]) {
    assert.equal(await createLocalAncestry({ run })(deliveryOld, deliveryExpected), 'unknown')
  }
  const inert = createLocalAncestry({ run: () => assert.fail('equal/invalid needs no Git') })
  assert.equal(await inert(deliveryExpected, deliveryExpected), 'equal')
  assert.equal(await inert('invalid', deliveryExpected), 'unknown')
})

test('staging delivery validates expected input without changing optional mode', async () => {
  for (const env of [{}, { LEAF_E2E_EXPECTED_SHA: '' }]) {
    const page = new EventEmitter(), runtime = { page, stack: { baseURL: stagingBase }, evidence: { projectSolar: {} } }
    let actions = 0
    assert.equal(await stagingEvidenceStep(runtime, { title: 'optional' }, async () => { actions++; return 7 }, async () => {},
      { env, fetchImpl: () => assert.fail('optional fetch'), ancestry: () => assert.fail('optional Git') }), 7)
    assert.equal(actions, 1)
    assert.equal(runtime.evidence.projectSolar.delivery.decision, 'not_configured')
  }
  for (const invalid of ['bad-private-value', 'A'.repeat(40), 'a'.repeat(39), null]) {
    await assert.rejects(stagingDelivery(stagingBase, deliveryOptions({ env: { LEAF_E2E_EXPECTED_SHA: invalid },
      fetchImpl: () => assert.fail('invalid input I/O') })), /PROD_CONFIG_INVALID LEAF_E2E_EXPECTED_SHA/)
  }
  const result = await stagingDelivery(stagingBase, deliveryOptions({
    fetchImpl: deliveryFetch({ source_sha: 'private-invalid' }, { source_sha: 'private-invalid' }),
    ancestry: () => assert.fail('invalid deployed input Git'),
  }))
  assert.equal(result.app.source_sha, null)
  assert.equal(result.web.source_sha, null)
  assert.doesNotMatch(JSON.stringify(result), /private-invalid/)
})

test('staging delivery preflight persists a real skip before browser work', async () => {
  for (const title of ['project-solar-open', 'project-solar-form-switch', 'project-solar-published', 'project-solar-proposal-review']) {
    const skip = new Error('Test is skipped: infra_stale_delivery')
    const page = { on: () => assert.fail('no monitor installation'), addInitScript: () => assert.fail('no init'),
      goto: () => assert.fail('no navigation'), evaluate: () => assert.fail('no project reads or writes') }
    let persisted
    const testInfo = { title, annotations: [], skip: (condition, reason) => {
      assert.equal(condition, true)
      assert.equal(reason, 'infra_stale_delivery')
      assert.equal(persisted.projectSolar.delivery.decision, 'stale')
      throw skip
    } }
    await assert.rejects(withStagingProject(page, stagingBase, stagingIds[1], '', testInfo,
      () => assert.fail('no action/checkout'), { delivery: deliveryOptions(),
        persist: async evidence => { persisted = structuredClone(evidence) } }), error => error === skip)
    const delivery = persisted.projectSolar.delivery
    assert.equal(delivery.expected_sha, deliveryExpected)
    assert.equal(delivery.app.source_sha, deliveryOld)
    assert.equal(delivery.web.source_sha, deliveryWeb)
    assert.equal(persisted.projectSolar.skipReason, 'infra_stale_delivery')
    assert.equal(persisted.projectSolar.classification.reasonCode, 'infra_stale_delivery')
    assert.deepEqual(testInfo.annotations, [{ type: 'infra_stale_delivery', description: JSON.stringify(delivery) }])
  }
})

test('staging delivery cannot hide UI or security failures', async () => {
  const stale = await stagingDelivery(stagingBase, deliveryOptions())
  assert.equal(classifyStagingOutcome({ error: new Error('UI barrier'), delivery: stale }).status, 'failed')
  assert.equal(classifyStagingOutcome({ error: new Error('UI barrier'), skipped: 'infra_stale_delivery', delivery: stale }).status, 'failed')
  assert.deepEqual(classifyStagingOutcome({ skipped: 'named', delivery: stale }),
    { status: 'skipped', barrierFailure: null, observations: [] })
  for (const base of ['https://app.leafdesign.ai', 'https://platform-staging.leafdesign.ai:444', 'https://user:password@platform-staging.leafdesign.ai']) {
    await assert.rejects(withStagingProject({}, base, stagingIds[1], '', {}, () => assert.fail('unsafe action'),
      { delivery: deliveryOptions({ fetchImpl: () => assert.fail('unsafe fetch') }) }))
  }
  await assert.rejects(withStagingProject({}, stagingBase, stagingIds[1], 'invalid-token', {}, () => assert.fail('identity action'),
    { delivery: deliveryOptions({ fetchImpl: () => assert.fail('identity fetch') }) }))
  await assert.rejects(stagingDelivery(stagingBase, deliveryOptions({ fetchImpl: async () =>
    deliveryResponse('/api/health', {}, 200, { url: 'https://foreign.invalid/api/health' }) })), /origin mismatch/)
  const currentOptions = deliveryOptions({ fetchImpl: deliveryFetch({ source_sha: deliveryExpected }, { source_sha: deliveryExpected }),
    ancestry: () => assert.fail('equal needs no Git') })
  for (const fail of [false, true]) {
    const page = new EventEmitter(), runtime = { page, stack: { baseURL: stagingBase }, evidence: { projectSolar: {} } }
    const failure = new Error('UI barrier')
    const run = () => stagingEvidenceStep(runtime, { title: 'current' }, async () => {
      emitStagingEvidenceResponse(page, '/api/tenant/claude-grant', 502)
      if (fail) throw failure
      return 9
    }, async () => {}, currentOptions)
    if (fail) await assert.rejects(run(), error => error === failure)
    else assert.equal(await run(), 9)
    assert.equal(runtime.evidence.projectSolar.delivery.decision, 'current')
    assert.equal(runtime.evidence.projectSolar.classification.status, fail ? 'failed' : 'passed')
    assert.equal(runtime.evidence.projectSolar.classification.observations[0].status, 502)
  }
})

test('staging delivery unavailable health stays diagnostic', async () => {
  for (const factory of [
    path => deliveryResponse(path, 'private-body', 404), path => deliveryResponse(path, 'private-body', 502),
    path => deliveryResponse(path, 'private-invalid-json'), path => deliveryResponse(path, { source_sha: 'unknown' }),
    path => deliveryResponse(path, { source_sha: 'abc' }), path => deliveryResponse(path, 'x'.repeat(4097)),
    path => deliveryResponse(path, {}, 302),
    () => { throw new Error('private-redirect-header') },
    path => deliveryResponse(path, {}, 200, { body: { getReader: () => ({ read: () => new Promise(() => {}), cancel: async () => {} }) } }),
    () => new Promise(() => {}),
  ]) {
    let calls = 0
    const evidence = await stagingDelivery(stagingBase, deliveryOptions({ timeoutMs: 1, fetchImpl: async url => {
      calls++
      return factory(new URL(url).pathname)
    }, ancestry: () => assert.fail('unavailable cannot use Git') }))
    assert.equal(calls, 2)
    assert.equal(evidence.decision, 'unverified')
    assert.doesNotMatch(JSON.stringify(evidence), /private-|xxxx|redirect-header/)
    assert.equal(evidence.app.source_sha, null)
    assert.equal(evidence.web.source_sha, null)
  }
  const result = await observeDeliveryHealth(stagingBase, '/health.json', { timeoutMs: 1,
    fetchImpl: () => new Promise(() => {}) })
  assert.deepEqual(result, { source_sha: null, observation: 'unavailable', http_status: null })
})

test('staging delivery evidence IO cannot change the verdict', async () => {
  const badDisk = async () => { throw new Error('disk unavailable') }
  const sentinel = new Error('Test is skipped: infra_stale_delivery')
  await assert.rejects(withStagingProject({ on: () => assert.fail('no monitor') }, stagingBase, stagingIds[1], '',
    { title: 'skip-io', annotations: [], skip: () => { throw sentinel } }, () => assert.fail('no action'),
    { persist: badDisk, delivery: deliveryOptions() }), error => error === sentinel)
  for (const fail of [false, true]) {
    const page = new EventEmitter(), runtime = { page, stack: { baseURL: stagingBase }, evidence: { projectSolar: {} } }
    const original = new Error('real failure')
    const run = () => stagingEvidenceStep(runtime, { title: 'io' }, async () => { if (fail) throw original; return 3 },
      badDisk, deliveryOptions({ env: {} }))
    if (fail) await assert.rejects(run(), error => error === original)
    else assert.equal(await run(), 3)
    assert.equal(runtime.evidence.projectSolar.classification.status, fail ? 'failed' : 'passed')
    assert.equal(page.listenerCount('response'), 0)
    assert.equal(page.listenerCount('console'), 0)
  }
})

test('walk form helpers pass default and explicit timeouts and preserve enabled booleans', async () => {
  for (const [options, timeout] of [[undefined, 15_000], [{ timeout: 7_000 }, 7_000]]) {
    for (const [helper, method] of [[walkFill, 'fill'], [walkSelect, 'selectOption']]) {
      let called = false
      await helper({ [method]: async (value, actual) => {
        assert.equal(value, '0')
        assert.deepEqual(actual, { timeout })
        called = true
      }, evaluate: () => assert.fail('successful actions need no probe') }, '0', 'Control', options)
      assert.equal(called, true)
    }
    for (const enabled of [true, false]) {
      assert.equal(await walkEnabled({ isEnabled: async actual => {
        assert.deepEqual(actual, { timeout })
        return enabled
      } }, 'Run solve', options), enabled)
    }
  }
})

test('walk form helpers name rejected controls, redact secrets and retain the original cause', async () => {
  const token = 'eyJabc.def.ghi', capability = 'lco1.secret'
  for (const [helper, method] of [[walkFill, 'fill'], [walkSelect, 'selectOption'], [walkEnabled, 'isEnabled']]) {
    const original = new Error(`missing control ${token} ${capability}\r\nprivate call log`)
    const locator = { [method]: async () => { throw original }, evaluate: async () => ['0'] }
    const label = `Control ${token} ${capability}`
    const action = helper === walkEnabled ? helper(locator, label) : helper(locator, '0', label)
    await assert.rejects(action, error => {
      assert.equal(error.cause, original)
      assert.equal(error.message.split('\n')[0], 'Control [redacted] [redacted]: missing control [redacted] [redacted]')
      for (const secret of [token, capability, 'private call log']) assert.equal(error.message.includes(secret), false)
      return true
    })
  }
})

test('walkSelect reports current option values with a bounded, redacted probe or unavailable', async () => {
  const original = new Error('option missing\ncall log')
  const token = 'eyJabc.def.ghi', capability = 'lco1.secret'
  for (const unavailable of [false, true]) {
    await assert.rejects(walkSelect({ selectOption: async () => { throw original },
      evaluate: async (fn, argument, options) => {
        assert.equal(argument, undefined)
        assert.deepEqual(options, { timeout: 2_000 })
        if (unavailable) throw new Error('probe detached')
        return fn({ options: [{ value: '0' }, { value: '1' }, { value: token }, { value: capability }] })
      },
    }, '2', 'Phase'), error => {
      assert.equal(error.cause, original)
      assert.equal(error.message, `Phase: option missing\nSelect options: ${unavailable
        ? '[unavailable]' : '["0","1","[redacted]","[redacted]"]'}`)
      return true
    })
  }
})

test('proposal review uses named bounded helpers for every fill, select and enabled check', () => {
  const body = reviewStagingProposal.toString()
  assert.doesNotMatch(body, /\.(?:fill|selectOption|isEnabled)\s*\(/)
  for (const call of ["walkEnabled(frame, 'Frame')", "walkEnabled(start, 'Run solve')",
    "walkFill(form.getByLabel('Grant ref', { exact: true }), process.env.LEAF_WALK_STAGING_GRANT_REF || 'staging-solver', 'Grant ref')",
    "walkFill(form.getByLabel('Request', { exact: true }), JSON.stringify(values.request), 'Request')",
    "walkFill(frame, values.frame, 'Frame')",
    "walkSelect(form.getByRole('combobox', { name: 'Phase', exact: true }), '0', 'Phase')"]) {
    assert.ok(body.includes(call), `missing helper call: ${call}`)
  }
})

test('walkClick resolves with default and explicit click timeouts', async () => {
  for (const [options, timeout] of [[undefined, 15_000], [{ timeout: 7_000 }, 7_000]]) {
    let clicked = false
    await walkClick({ click: async actual => {
      assert.deepEqual(actual, { timeout })
      clicked = true
    }, evaluate: async () => assert.fail('successful clicks need no probe') }, 'Open Solar', options)
    assert.equal(clicked, true)
  }
})

test('walkClick reports redacted geometry and the original first-line reason with its cause', async () => {
  const token = 'eyJabc.def.ghi', capability = 'lco1.secret'
  const original = new Error(`outside viewport ${token} ${capability}\nprivate second line`)
  const overlay = { tagName: 'DIV', id: token, classList: ['overlay', 'blocking', capability, 'omitted'] }
  const node = {
    getBoundingClientRect: () => ({ x: -100.4, y: 20.7, width: 40.2, height: 10.8 }),
    contains: hit => hit === overlay,
    ownerDocument: {
      defaultView: { innerWidth: 800, innerHeight: 600,
        getComputedStyle: target => {
          assert.equal(target, node)
          return { position: 'fixed', top: '20px', left: '-100px', zIndex: '9' }
        } },
    },
  }
  for (const [hit, contains, hitIsTarget] of [[overlay, true, true], [overlay, false, false],
    [node, false, true], [null, false, false]]) {
    node.tagName = 'BUTTON'
    node.id = ''
    node.classList = []
    node.contains = () => contains
    node.ownerDocument.elementFromPoint = (x, y) => {
      assert.equal(x, -100.4 + 40.2 / 2)
      assert.equal(y, 20.7 + 10.8 / 2)
      return hit
    }
    await assert.rejects(walkClick({
      click: async () => { throw original },
      evaluate: async (fn, argument, options) => {
        assert.equal(argument, undefined)
        assert.deepEqual(options, { timeout: 2_000 })
        return fn(node)
      },
    }, `Open Solar ${token} ${capability}`), error => {
      assert.equal(error.cause, original)
      assert.ok(error.message.startsWith('Open Solar [redacted] [redacted]: outside viewport [redacted] [redacted]\n'))
      for (const secret of [token, capability, 'private second line', 'omitted']) assert.equal(error.message.includes(secret), false)
      const probe = JSON.parse(error.message.split('\nClick target: ')[1])
      assert.deepEqual(probe.box, { x: -100, y: 21, width: 40, height: 11 })
      assert.deepEqual(probe.viewport, { width: 800, height: 600 })
      assert.equal(probe.hit, hit === overlay ? 'div#[redacted].overlay.blocking.[redacted]' : hit ? 'button' : null)
      assert.equal(probe.hitIsTarget, hitIsTarget)
      assert.deepEqual(probe.position, { position: 'fixed', top: '20px', left: '-100px', zIndex: '9' })
      return true
    })
  }
})

test('walkClick keeps the full probe JSON while limiting the original reason to 300 characters', async () => {
  const probe = { box: {}, viewport: {}, hit: 'x'.repeat(500), hitIsTarget: false, position: {} }
  await assert.rejects(walkClick({ click: async () => { throw new Error('r'.repeat(400)) },
    evaluate: async () => probe }, 'Run solve'), error => {
    assert.equal(error.message, `Run solve: ${'r'.repeat(300)}\nClick target: ${JSON.stringify(probe)}`)
    return true
  })
})

test('walkClick preserves the click failure when its geometry probe is unavailable', async () => {
  const original = new Error('target detached\ncall log')
  await assert.rejects(walkClick({ click: async () => { throw original },
    evaluate: async () => { throw new Error('probe detached') } }, 'Open versions'), error => {
    assert.equal(error.message, 'Open versions: target detached\nClick target: [unavailable]')
    assert.equal(error.cause, original)
    return true
  })
})

function exactLabelSelectLines(source) {
  // Keep strings atomic and omit comments so fixture text cannot flag itself.
  const tokenPattern = new RegExp(String.raw`\/\/[^\n]*|\/\*[\s\S]*?\*\/|'(?:\\[\s\S]|[^'\\])*'|"(?:\\[\s\S]|[^"\\])*"|\x60(?:\\[\s\S]|[^\x60\\])*\x60|[A-Za-z_$][\w$]*|[^\s]`, 'g')
  const tokens = [...source.matchAll(tokenPattern)]
    .filter(([token]) => !token.startsWith('//') && !token.startsWith('/*'))
  const value = index => tokens[index]?.[0]
  const callEnd = start => {
    let depth = 0
    for (let index = start; index < tokens.length; index++) {
      if (value(index) === '(') depth++
      if (value(index) === ')' && --depth === 0) return index
    }
    return tokens.length
  }
  const lines = []
  for (let index = 0; index < tokens.length; index++) {
    if (value(index) !== 'getByLabel' || value(index + 1) !== '(') continue
    const end = callEnd(index + 1)
    const exact = tokens.slice(index + 2, end).some(([token], offset) =>
      token === 'exact' && value(index + 3 + offset) === ':' && value(index + 4 + offset) === 'true')
    if (!exact) continue
    for (let next = end + 1; value(next) === '.' && value(next + 2) === '(';) {
      if (value(next + 1) === 'selectOption') {
        lines.push(source.slice(0, tokens[index].index).split('\n').length)
        break
      }
      next = callEnd(next + 2) + 1
    }
  }
  return lines
}

test('select locator guard flags exact labels, including multiline calls and chained locators', () => {
  assert.deepEqual(exactLabelSelectLines(`await settings.getByLabel('Drawing units', { exact: true }).selectOption('m')`), [1])
  assert.deepEqual(exactLabelSelectLines(`await form
    .getByLabel('Phase', {
      exact: true,
    }).first().selectOption('0')`), [2])
})

test('select locator guard accepts combobox roles and separate statements', () => {
  assert.deepEqual(exactLabelSelectLines(`await settings.getByRole('combobox', { name: 'Drawing units', exact: true }).selectOption('m')`), [])
  assert.deepEqual(exactLabelSelectLines(`await form.getByLabel('Request', { exact: true }).fill('{}')
    await form.getByRole('combobox', { name: 'Phase', exact: true }).selectOption('0')`), [])
})

test('all e2e select controls avoid exact label locators', () => {
  const violations = []
  const visit = (directory, relative = 'web/e2e') => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = `${relative}/${entry.name}`
      const url = new URL(entry.name + (entry.isDirectory() ? '/' : ''), directory)
      if (entry.isDirectory()) visit(url, path)
      else if (entry.isFile() && entry.name.endsWith('.mjs')) {
        for (const line of exactLabelSelectLines(readFileSync(url, 'utf8'))) violations.push(`${path}:${line}`)
      }
    }
  }
  visit(new URL('../', import.meta.url))
  assert.deepEqual(violations, [], `Use getByRole('combobox', { name, exact: true }) for select controls:\n${violations.join('\n')}`)
})

function fakeAuth(value) {
  const headers = {}
  headers.Authorization = `Bearer ${value}`
  return headers
}

const stagingBinding = { organization_id: stagingIds[0], project_id: stagingIds[1],
  drawing_id: stagingIds[2], input_version_id: stagingIds[3] }
const stagingResponse = (path, body, status = 200, method = 'GET') => ({
  url: () => `${stagingBase}${path}`, status: () => status, ok: () => status < 400,
  json: async () => body, request: () => ({ method: () => method, headers: () => ({ authorization: 'Bearer unit-only' }) }),
})
const stagingGraph = () => ({ rev: 1, panels: [], strings: [], inverters: [], routes: [], schedules: [],
  settings: { panels_in_sequence: 1 } })
const stagingContextBody = (binding = stagingBinding, graph = stagingGraph()) => ({
  context: { ...binding, is_head: true, head_version_id: binding.input_version_id, intake_sha256: 'a'.repeat(64) },
  intake: { solar_design_graph: graph, solar_design_graph_sha256: 'b'.repeat(64) },
})

test('staging network paths cover runtime APIs and retain only query keys', () => {
  for (const path of ['/api/projects', '/api/projects/p/drawing-versions/v/context', '/api/capabilities',
    '/api/tools/catalog', '/api/session', '/api/drawings/d', '/api/campaigns', '/api/jobs/j', '/api/run']) {
    assert.equal(stagingNetworkPath(`${stagingBase}${path}?drawing_id=private&token=secret&token=other&empty=`),
      `${path}?drawing_id&token&token&empty`)
  }
  for (const path of ['/app', '/assets/api/jobs']) {
    assert.equal(stagingNetworkPath(`${stagingBase}${path}?secret=value`), null)
  }
})

test('staging network reads only public error codes from response bodies', async () => {
  for (const [body, expected] of [
    [{ error_code: 'SCOPE_PENDING', private: 'secret' }, 'SCOPE_PENDING'],
    [{ error: { error_code: 'context_missing', detail: 'secret' } }, 'context_missing'],
    [{ error_code: 'not a public code', error: { error_code: 'fallback_code' } }, 'fallback_code'],
    [{ detail: 'secret' }, null],
  ]) assert.equal(await stagingErrorCode(stagingResponse('/api/session', body, 403)), expected)
  assert.equal(await stagingErrorCode(stagingResponse('/api/run', { error_code: 'RUN_PENDING' }, 202)), 'RUN_PENDING')
  assert.equal(await stagingErrorCode({ status: () => 500, json: async () => { throw new Error('not JSON') } }), null)
})

test('staging network retains the last 40 events, failures and timings without private content', async () => {
  const page = new EventEmitter(), records = []
  const finish = observeStagingNetwork(page, records)
  const request = path => ({ url: () => `${stagingBase}${path}`, method: () => 'GET',
    timing: () => ({ startTime: 1, responseEnd: 12.6 }), failure: () => ({ errorText: 'net::ERR_FAILED' }) })
  page.emit('requestfailed', request('/api/projects/p/drawing-versions/early/context'))
  page.emit('requestfailed', request('/api/capabilities'))
  for (let index = 0; index < 205; index++) {
    const response = stagingResponse(`/api/jobs/${index}?token=secret`, { error_code: 'JOB_FAILED', detail: 'secret' }, 500)
    response.request = () => request(`/api/jobs/${index}?token=secret`)
    response.finished = async () => null
    page.emit('response', response)
  }
  assert.equal(records.length, 40)
  assert.equal(stagingNetworkFailure(records).includes('/early/context'), false)
  page.emit('requestfailed', request('/api/projects/p/drawing-versions/v/context?drawing_id=secret'))
  page.emit('requestfailed', request('/api/capabilities?tenant=secret'))
  page.emit('requestfailed', request('/api/run?token=value'))
  await finish()
  assert.equal(records.length, 40)
  assert.equal(records[0].path, '/api/jobs/168?token')
  assert.deepEqual(records[0], { method: 'GET', path: '/api/jobs/168?token', duration_ms: 13,
    status: 500, error_code: 'JOB_FAILED' })
  assert.equal(records.at(-1).status, 'failed')
  assert.equal(records.at(-1).path, '/api/run?token')
  assert.equal(records.at(-1).duration_ms, 13)
  assert.equal(JSON.stringify(records).includes('secret'), false)
  const diagnostic = stagingNetworkFailure(records)
  assert.match(diagnostic, /drawing-versions\/v\/context\?drawing_id/)
  assert.match(diagnostic, /capabilities\?tenant/)
  assert.equal(page.listenerCount('response'), 0)
  assert.equal(page.listenerCount('requestfailed'), 0)
})

test('staging network durations reject invalid starts and out-of-range elapsed times', async () => {
  for (const [startTime, responseEnd, expected] of [
    [0, -1, null], [-1, -1, null], [NaN, -1, null], [Infinity, -1, null],
    [0, 12.6, null], [-1, 12.6, null],
    [1, -1, null], [Date.now() + 60000, -1, null],
    [1, 600000.1, null], [1, Infinity, null],
    [1, 0, 0], [1, 12.6, 13], [1, 600000, 600000],
  ]) {
    const page = new EventEmitter(), records = []
    const finish = observeStagingNetwork(page, records)
    page.emit('requestfailed', {
      url: () => `${stagingBase}/api/jobs/j`, method: () => 'GET',
      timing: () => ({ startTime, responseEnd }), failure: () => ({ errorText: 'net::ERR_ABORTED' }),
    })
    await finish()
    assert.equal(records.length, 1)
    assert.equal(records[0].duration_ms, expected, `startTime=${startTime}, responseEnd=${responseEnd}`)
  }
})

test('staging network observer errors never reject the diagnostic drain', async () => {
  const page = new EventEmitter(), records = []
  const finish = observeStagingNetwork(page, records)
  page.emit('requestfailed', { url: () => 'invalid URL' })
  page.emit('response', { request: () => { throw new Error('unavailable request') } })
  await finish()
  assert.deepEqual(records, [])
  assert.equal(stagingNetworkFailure(records), 'Staging network: []\nStaging console: []')
})

test('pure staging diagnostics bound ordered rings and exclude headers and body fields', () => {
  const requests = [], messages = []
  const AUTH_HEADER = 'author' + 'ization'
  const token = 'eyJhbGciOiJIUzI1NiJ9.payload.signature', capability = 'lco1.checkout-secret'
  for (let index = 0; index < 45; index++) appendStagingRequest(requests, {
    method: 'POST', path: `/api/run/${index}/${token}/${capability}?token=private&drawing_id=private`,
    status: 403, duration_ms: index, error_code: 'RUN_REFUSED',
    headers: { [AUTH_HEADER]: 'header-secret' }, body: { detail: 'body-secret' },
  })
  assert.equal(requests.length, 40)
  assert.deepEqual(requests.map(row => row.duration_ms), Array.from({ length: 40 }, (_, index) => index + 5))
  assert.equal(requests[0].path, '/api/run/5/[redacted]/[redacted]?token&drawing_id')
  for (let index = 0; index < 25; index++) appendStagingConsole(messages, {
    level: index % 2 ? 'warning' : 'error', text: `${index}: ${token} ${capability} ${'x'.repeat(400)}`,
    headers: { [AUTH_HEADER]: 'header-secret' },
  })
  appendStagingConsole(messages, { level: 'log', text: 'ignored' })
  assert.equal(messages.length, 20)
  assert.ok(messages[0].text.startsWith('5: [redacted] [redacted]'))
  assert.ok(messages.every(row => row.text.length === 300))
  const output = stagingNetworkFailure(requests, messages)
  for (const secret of [token, capability, 'private', 'header-secret', 'body-secret', 'ignored']) {
    assert.equal(output.includes(secret), false)
  }
  assert.equal(redactSolarDiagnostic(' a '.repeat(200)).length, 300)
  assert.equal(redactSolarDiagnostic({ headers: { [AUTH_HEADER]: 'header-secret' } }), '[unavailable]')
})

test('staging requests stay in initiation order when responses finish out of order', async () => {
  const page = new EventEmitter(), records = []
  const finish = observeStagingNetwork(page, records)
  const requests = [1, 2].map(index => ({ url: () => `${stagingBase}/api/run/${index}`,
    method: () => 'POST', timing: () => ({ startTime: 1, responseEnd: index }) }))
  for (const request of requests) page.emit('request', request)
  for (const request of [...requests].reverse()) page.emit('response', {
    request: () => request, status: () => 202, json: async () => ({}), finished: async () => null,
  })
  await finish()
  assert.deepEqual(records.map(row => row.path), ['/api/run/1', '/api/run/2'])
  assert.equal(page.listenerCount('request'), 0)
})

test('Solar failure snapshot reads form, checkout and every alert before cleanup and tolerates unreadable locators', async () => {
  let released = false
  const text = value => ({ textContent: async () => { assert.equal(released, false); return value } })
  const settings = {
    getByTestId: id => text(`${id} eyJabc.def.ghi lco1.secret`),
    getAttribute: async (name, options) => {
      assert.deepEqual(options, { timeout: 2_000 })
      return { 'data-mode': 'edit', 'data-submit-seq': ' 2 ',
        'data-submit-exit': ' superseded_context eyJabc.def.ghi lco1.secret ' }[name]
    },
    getByRole: role => {
      assert.equal(role, 'alert')
      return { count: async () => 2, nth: index => text(`${index}: ${'a'.repeat(400)}`) }
    },
  }
  const page = { getByRole: () => ({ getByRole: role => role === 'status' ? text('Checkout held')
    : { isEnabled: async () => { assert.equal(released, false); return true } } }) }
  const output = await projectSolarRunDiagnostic(page, settings, { isEnabled: async () => true })
  released = true
  const fields = JSON.parse(output.slice(output.indexOf('{')))
  assert.equal(fields['data-mode'], 'edit')
  assert.equal(fields['data-submit-seq'], '2')
  assert.equal(fields['data-submit-exit'], 'superseded_context [redacted] [redacted]')
  assert.equal(fields.startEnabled, 'true')
  assert.equal(fields.checkoutStatus, 'Checkout held')
  assert.equal(fields.releaseEnabled, 'true')
  assert.equal(fields['solar-settings-reason'], 'solar-settings-reason [redacted] [redacted]')
  assert.equal(fields['solar-settings-run'], 'solar-settings-run [redacted] [redacted]')
  assert.deepEqual(fields.alerts.map(value => value.length), [300, 300])
  const unavailable = await projectSolarRunDiagnostic({}, {}, {})
  assert.equal(unavailable.includes('[unavailable]'), true)
  const unreadable = await projectSolarRunDiagnostic({}, {
    getAttribute: async () => { throw new Error('detached') } }, {})
  const unreadableFields = JSON.parse(unreadable.slice(unreadable.indexOf('{')))
  assert.equal(unreadableFields['data-submit-seq'], '[unavailable]')
  assert.equal(unreadableFields['data-submit-exit'], '[unavailable]')
})

test('staging step persists evidence on pass and failure and preserves verdicts if evidence IO fails', async () => {
  for (const fail of [false, true]) {
    const page = new EventEmitter(), runtime = { page, evidence: { projectSolar: { steps: [] } } }
    const failure = new Error('workspace hidden')
    let persisted
    const run = () => stagingEvidenceStep(runtime, { title: 'project-solar-open' }, async () => {
      const response = stagingResponse('/api/projects/p/drawing-versions/v/context?drawing_id=private',
        { error: { error_code: 'SCOPE_PENDING' }, private: 'secret' }, 409)
      response.request = () => ({ url: response.url, method: () => 'GET',
        timing: () => ({ startTime: 1, responseEnd: 10 }) })
      response.finished = async () => null
      page.emit('response', response)
      page.emit('console', { type: () => 'warning', text: () => 'run blocked lco1.secret' })
      page.emit('requestfailed', { url: () => `${stagingBase}/api/capabilities`, method: () => 'GET',
        failure: () => ({ errorText: 'net::ERR_FAILED' }), timing: () => ({ startTime: 1, responseEnd: 20 }) })
      if (fail) throw failure
      return 'passed'
    }, async (evidence, title) => {
      persisted = { evidence, title }
      throw new Error('disk unavailable')
    })
    if (fail) await assert.rejects(run(), error => {
      assert.equal(error, failure)
      assert.match(error.message, /workspace hidden\nStaging network:/)
      assert.match(error.message, /SCOPE_PENDING/)
      assert.match(error.message, /capabilities/)
      assert.match(error.message, /Staging console:.*run blocked \[redacted\]/)
      return true
    })
    else assert.equal(await run(), 'passed')
    assert.equal(persisted.title, 'project-solar-open')
    assert.equal(persisted.evidence.projectSolar.network.length, 2)
    assert.equal(persisted.evidence.projectSolar.network[0].error_code, 'SCOPE_PENDING')
    assert.equal(page.listenerCount('console'), 0)
  }
})

test('staging fixture selects the newest canonical version and rejects ambiguous or foreign bindings', () => {
  const row = { org_id: stagingIds[0], project_id: stagingIds[1], drawing_id: stagingIds[2], version_id: stagingIds[3], seq: 1 }
  const workspace = { project: { project_id: stagingIds[1], org_id: stagingIds[0] },
    drawing_versions: [row, { ...row, seq: 2, version_id: stagingIds[5] }] }
  assert.deepEqual(canonicalProjectVersion(workspace, stagingIds[1]), { ...stagingBinding, input_version_id: stagingIds[5] })
  for (const changed of [
    { ...workspace, project: { ...workspace.project, project_id: stagingIds[0] } },
    { ...workspace, drawing_versions: [] },
    { ...workspace, drawing_versions: [row, { ...row, drawing_id: stagingIds[4] }] },
    { ...workspace, drawing_versions: [{ ...row, org_id: stagingIds[4] }] },
    { ...workspace, drawing_versions: [{ ...row, version_id: 3 }] },
  ]) assert.throws(() => canonicalProjectVersion(changed, stagingIds[1]))
})

test('staging refusal annotations retain public catalog reasons and omit private fields', () => {
  const secret = 'private'
  const privateReason = fakeAuth(secret).Authorization
  const tool = { name: 'solar-solve-proposal', params: { grant_ref: 'private' }, availability: {
    entitled: false, runnable: false, admission: { status_code: 403, error: { reason_code: 'ENTITLEMENT_REQUIRED' } },
    refusal_reasons: ['cloud_offline', privateReason], authorization: secret,
  } }
  const expected = [{ name: tool.name, entitled: false, runnable: false, admission_status: 403,
    reason_code: 'ENTITLEMENT_REQUIRED', refusal_reasons: ['cloud_offline'] }]
  assert.deepEqual(proposalCatalogState({ families: [{ capabilities: [tool] }] }), expected)
  assert.deepEqual(proposalCatalogState({ tools: [tool] }), expected)
  assert.deepEqual(proposalCatalogState({}), [])
  for (const body of [{ error: { reason_code: 'CLOUD_OFFLINE' } }, { detail: { error: { reason_code: 'CLOUD_OFFLINE' } } },
    { detail: { reason_code: 'CLOUD_OFFLINE' } }, { reason_code: 'CLOUD_OFFLINE' }, { reason: 'CLOUD_OFFLINE' }]) {
    assert.equal(refusalCode(body), 'CLOUD_OFFLINE')
  }
  assert.equal(refusalCode({ error: { reason_code: privateReason } }, 'run_http_503'), 'run_http_503')
})

test('staging Solve request uses the canonical frame cells and panel coordinates', () => {
  const graph = { settings: { panels_in_sequence: 2 }, panels: [
    { id: 'panel-1', centre: [5, 8], angle: 90 }, { id: 'panel-2', centre: [6, 8], angle: 90 },
    { id: 'panel-3', centre: [7, 8], angle: 90 },
  ], frames: [{ id: 'frame-1', matrix: [[{ panel_ref: 'panel-1' }, { panel_ref: null, x: 1, y: 2 }],
    [{ panel_ref: 'panel-2' }, { panel_ref: 'panel-3' }]] }] }
  const values = proposalFormValues(graph)
  assert.equal(values.frame, 'frame-1')
  assert.deepEqual(values.request.grid.Sequences, [[2, 1], [1, 1]])
  assert.deepEqual(values.request.grid.Rows[0].Panels[0], { Code: 1, Id: 'panel-1', Seq: 0,
    InverterId: -1, StringInputNumber: 0, X: 5, Y: 8, Angle: 90 })
  assert.deepEqual(values.request.grid.Rows[0].Panels[1], { Code: 0, Id: '', Seq: 0,
    InverterId: -1, StringInputNumber: 0, X: 1, Y: 2, Angle: 0 })
  assert.equal(proposalFormValues(null).frame, 'walk-staging-frame')
})

test('staging action arms its listener before clicking and refuses an unapproved page or base', async () => {
  const events = [], response = stagingResponse('/api/run', {}, 202, 'POST')
  const page = { url: () => stagingBase, waitForResponse: async predicate => {
    events.push('listen')
    assert.equal(predicate(response), true)
    assert.equal(predicate(stagingResponse('/api/run', {}, 200, 'GET')), false)
    return response
  } }
  assert.equal(await stagingAction(page, stagingBase, 'POST', '/api/run', () => events.push('click')), response)
  assert.deepEqual(events, ['listen', 'click'])
  for (const [browser, base] of [[{ ...page, url: () => 'https://example.com' }, stagingBase], [page, 'https://app.leafdesign.ai']]) {
    await assert.rejects(stagingAction(browser, base, 'POST', '/api/run', () => assert.fail('must not click')))
  }
})

test('staging monitor drains every bearer response and rejects request leakage or off-origin navigation', async () => {
  for (const fault of ['none', 'request', 'response', 'navigation']) {
    const page = new EventEmitter(), solar = { traffic: { runCount: 0 }, catalog: [] }
    page.url = () => fault === 'navigation' ? 'https://example.com' : stagingBase
    const finish = monitorStagingSolar(page, solar)
    page.emit('request', { url: () => `${fault === 'request' ? 'https://example.com' : stagingBase}/api/run`,
      method: () => 'POST', headers: () => ({ authorization: 'Bearer unit-only' }) })
    const response = stagingResponse('/api/capabilities', { families: [{ capabilities: [
      { name: 'solar-solve-proposal', availability: { runnable: false, refusal_reasons: ['cloud_offline'] } },
    ] }] })
    if (fault === 'response') response.url = () => 'https://example.com/api/capabilities'
    page.emit('response', response)
    if (fault === 'none') {
      await finish()
      assert.equal(solar.traffic.runCount, 1)
      assert.equal(solar.catalog[0].runnable, false)
    } else await assert.rejects(finish())
    assert.equal(page.listenerCount('response'), 0)
  }
})

test('staging monitor bounds stalled catalog bodies without recording response errors', async () => {
  const page = new EventEmitter(), solar = { traffic: { runCount: 0 }, catalog: [] }
  page.url = () => stagingBase
  const finish = monitorStagingSolar(page, solar)
  const reads = []
  for (const path of ['/api/capabilities', '/api/tools']) {
    const response = stagingResponse(path, {})
    response.json = () => { reads.push(path); return new Promise(() => {}) }
    page.emit('response', response)
  }
  let timer
  try {
    await Promise.race([
      finish(),
      new Promise((resolve, reject) => { timer = setTimeout(() => reject(new Error('catalog cleanup exceeded 3 s')), 3000) }),
    ])
  } finally { clearTimeout(timer) }
  assert.deepEqual(reads, ['/api/capabilities', '/api/tools'])
  assert.deepEqual(solar.catalog, [])
  assert.equal(page.listenerCount('response'), 0)
})

test('staging monitor ignores failed catalog reads but preserves bearer response origin failures', async () => {
  for (const fault of ['parse', 'abort', 'origin']) {
    const page = new EventEmitter(), solar = { traffic: { runCount: 0 }, catalog: [] }
    page.url = () => stagingBase
    const finish = monitorStagingSolar(page, solar)
    const response = stagingResponse('/api/tools', {})
    response.json = () => {
      if (fault === 'origin') assert.fail('off-origin bearer response must be rejected before reading its body')
      if (fault === 'parse') throw new SyntaxError('invalid JSON')
      return Promise.reject(new Error('body aborted'))
    }
    if (fault === 'origin') response.url = () => 'https://example.com/api/tools'
    page.emit('response', response)
    if (fault === 'origin') await assert.rejects(finish(), /bearer response origin check failed/)
    else await finish()
    assert.deepEqual(solar.catalog, [])
    assert.equal(page.listenerCount('response'), 0)
  }
})

test('staging project session rejects an unsafe target or unverifiable principal before using the page', async () => {
  await assert.rejects(withStagingProject({}, 'https://app.leafdesign.ai', stagingIds[1], '', {}, () => assert.fail('unsafe origin')))
  await assert.rejects(withStagingProject({}, stagingBase, stagingIds[1], 'invalid-token', {}, () => assert.fail('invalid identity')))
})

test('staging suite declares four tests and skips absent credentials or fixture ids before opening a project', async () => {
  const source = readFileSync(new URL('../walk-staging/project-solar.spec.mjs', import.meta.url), 'utf8')
    .replace(/^import .*\n/gm, '')
  for (const env of [{}, { LEAF_E2E_STAGING_JWT: 'unit-only' },
    { LEAF_E2E_STAGING_JWT: 'unit-only', LEAF_WALK_STAGING_SCRATCH_PROJECT_ID: stagingIds[1] }]) {
    const rows = [], annotations = [], hooks = []
    let collecting = true
    const declare = (title, callback) => rows.push({ title, callback })
    declare.beforeEach = hook => hooks.push(hook)
    declare.skip = (condition, reason) => {
      if (typeof condition === 'function') condition = condition({}, { config: { metadata: { stagingWalk: true } } })
      if (collecting) { if (condition) annotations.push(reason); return }
      if (condition) throw new Error(`SKIP: ${reason}`)
    }
    const fail = () => assert.fail('a missing credential or fixture must never open a project')
    new Function('test', 'expect', 'process', 'withStagingProject', 'publishProjectSolar', 'switchProjectSolarForms',
      'reviewStagingProposal', 'stagingAction', 'stagingRun', source)(declare, walkExpect, { env }, fail, fail, fail, fail, fail, fail)
    assert.deepEqual(rows.map(row => row.title), ['project-solar-open', 'project-solar-form-switch',
      'project-solar-published', 'project-solar-proposal-review'])
    if (!env.LEAF_E2E_STAGING_JWT) assert.match(annotations[0], /LEAF_E2E_STAGING_JWT/)
    collecting = false
    assert.throws(() => hooks[0]({}, { config: { metadata: { stagingWalk: false } } }),
      /SKIP: .*playwright\.walk-staging\.config\.mjs/)
    for (const row of rows) {
      for (const hook of hooks) await hook({}, { config: { metadata: { stagingWalk: true } } })
      await assert.rejects(row.callback({}, {}), /SKIP: LEAF_/)
    }
  }
})

test('staging browser reads bind authority and refuse off-origin responses, bad paths and stale heads', async () => {
  const context = stagingContextBody()
  const page = { url: () => stagingBase, evaluate: async (_fn, args) => {
    assert.deepEqual(args.binding, stagingBinding)
    assert.equal(args.origin, stagingBase)
    return { url: `${stagingBase}${args.path}`, status: 200, body: context }
  } }
  assert.equal((await stagingContext(page, stagingBase, stagingBinding, true)).head, stagingIds[3])
  await assert.rejects(stagingRead(page, stagingBase, '//example.com/api', stagingBinding))
  await assert.rejects(stagingRead({ ...page, evaluate: async () => ({ url: 'https://example.com/api', status: 200 }) },
    stagingBase, '/api/jobs/unit', stagingBinding))
  await assert.rejects(stagingContext({ ...page, evaluate: async () => ({ url: stagingBase, status: 200,
    body: { ...context, context: { ...context.context, head_version_id: stagingIds[5] } } }) }, stagingBase, stagingBinding))
})

test('staging publication requires a bound complete job and follows its distinct output head', async () => {
  const next = { ...stagingBinding, input_version_id: stagingIds[5] }
  const requestBody = { tool: 'solar-settings', dwg: stagingIds[3], params: { drawing_id: stagingIds[2], expected_rev: 0 } }
  const response = stagingResponse('/api/run', { job_id: stagingIds[4], status: 'submitted' }, 202, 'POST')
  response.request = () => ({ method: () => 'POST', postDataJSON: () => requestBody, headers: () => ({
    'x-org-id': stagingIds[0], 'x-project-id': stagingIds[1], authorization: 'Bearer unit-only',
    'x-checkout-capability': 'unit-only', 'idempotency-key': 'unit-only',
  }) })
  const job = { job_id: stagingIds[4], org_id: stagingIds[0], project_id: stagingIds[1], input_version_id: stagingIds[3],
    tool_name: 'solar-settings', status: 'complete', execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: stagingIds[2] },
    result: { output_version_id: stagingIds[5] } }
  const paths = []
  const page = { url: () => stagingBase, waitForResponse: async predicate => { assert.ok(predicate(response)); return response },
    evaluate: async (_fn, args) => {
      paths.push(args.path)
      return { url: `${stagingBase}${args.path}`, status: 200,
        body: args.path.startsWith('/api/jobs/') ? job : stagingContextBody(next) }
    } }
  const run = () => stagingRun({ page, expect: walkExpect, apiBase: stagingBase, binding: stagingBinding,
    before: { graph: null }, tool: 'solar-settings', action: async () => {} })
  const result = await run()
  assert.equal(result.binding.input_version_id, stagingIds[5])
  assert.equal(result.context.head, stagingIds[5])
  assert.deepEqual(paths, [`/api/jobs/${stagingIds[4]}`,
    `/api/projects/${stagingIds[1]}/drawing-versions/${stagingIds[5]}/context?drawing_id=${stagingIds[2]}`])
  requestBody.dwg = stagingIds[5]
  await assert.rejects(run())
})

const stagingAssertions = value => ({
  toBe: expected => assert.equal(value, expected),
  toMatch: expected => assert.match(value, expected),
  toHaveCount: async expected => assert.equal(value.count ?? 0, expected),
  toBeVisible: async () => assert.notEqual(value.visible, false),
  toContainText: async expected => assert.ok(value.text.includes(expected)),
  toBeEnabled: async () => assert.equal(await value.isEnabled(), true),
  toHaveAttribute: async (key, expected) => {
    const actual = await value.getAttribute(key)
    if (expected instanceof RegExp) assert.match(actual, expected)
    else assert.equal(actual, expected)
  },
})
stagingAssertions.poll = callback => ({ toBe: async expected => assert.equal(await callback(), expected) })

function workspaceEnvironmentFake({ present = true, entered = 'false', resume = true, controlsPresent = true,
  offered = true, settles = true, profileVisible = true, profileSettles = true, chooserVisible = true,
  legacyVisible = true, boardVisible = legacyVisible, returnVisible = legacyVisible,
  legacySelected = 'false', status = 'available', statusAttached = true, statusLabel = 'Ready' } = {}) {
  const events = []
  let expanded = 'false', profile = 'cad', entryClicked = false
  // Project's locator assertion delegates to _expect; environment polls read
  // attributes directly and record the state observed after the action.
  class Locator {
    constructor(attributes) { this.attributes = attributes; this._apiName = 'Locator' }
    async _expect(expression, options) {
      assert.ok(['to.have.attribute', 'to.have.attribute.value'].includes(expression))
      assert.equal(options.timeout, 15_000)
      const name = options.expressionArg, expected = options.expectedText[0].string
      const received = await this.getAttribute(name)
      assert.equal(received, expected)
      events.push(`assert:${name}=${expected}`)
      return { matches: true, received, log: [] }
    }
    async getAttribute(name) {
      const value = this.attributes()[name] ?? null
      if ((name === 'data-entered' && entryClicked) || (name === 'data-profile' && profile === 'solar')) {
        events.push(`assert:${name}=${value}`)
        if (name === 'data-entered') entryClicked = false
      }
      return value
    }
  }
  const project = Object.assign(new Locator(() => ({ 'aria-expanded': expanded })), {
    count: async () => 1,
    click: async options => {
      assert.deepEqual(options, { timeout: 15_000 })
      events.push('Close Project panel'); expanded = 'false'
    },
  })
  const environment = Object.assign(new Locator(() => ({ 'data-entered': entered, 'data-profile': profile })), {
    count: async () => present ? 1 : 0,
    getByRole: (role, { name, exact }) => {
      assert.equal(role, 'button')
      assert.equal(exact, true)
      assert.ok(['Continue workspace', 'Open project or material'].includes(name))
      return { isVisible: async () => offered && name === (resume ? 'Continue workspace' : 'Open project or material'), click: async options => {
        assert.deepEqual(options, { timeout: 15_000 })
        assert.equal(name, resume ? 'Continue workspace' : 'Open project or material')
        events.push(name); entered = settles ? 'true' : 'false'; entryClicked = true
        if (!resume) expanded = 'true'
      } }
    },
  })
  const profileControl = {
    count: async () => 1, isVisible: async () => controlsPresent && profileVisible,
    inputValue: async () => profile,
    selectOption: async option => {
      if (typeof option === 'string') {
        events.push(`Select profile:${option}`)
        if (profileSettles) profile = option
      } else {
        assert.deepEqual(option, { label: 'Solar CAD' }); profile = 'solar'; events.push('Select Solar CAD')
      }
    },
  }
  const legacy = {
    text: statusLabel,
    isVisible: async () => legacyVisible,
    click: async () => { events.push('Select legacy tab'); legacySelected = 'true' },
    getAttribute: async name => { assert.equal(name, 'aria-selected'); return legacySelected },
    locator: selector => {
      assert.equal(selector, '[data-state]')
      return { isVisible: async () => false, getAttribute: async name => {
        assert.equal(name, 'data-state'); return statusAttached ? status : null
      } }
    },
  }
  const controls = { count: async () => controlsPresent ? 1 : 0, getByRole: (role, { name, exact }) => {
    assert.equal(exact, true)
    if (role === 'button' && name === 'Project') return project
    assert.equal(role, 'combobox')
    assert.equal(name, 'Workspace profile')
    return profileControl
  } }
  const page = {
    locator: selector => { assert.equal(selector, 'section.workspace-environment'); return environment },
    getByRole: (role, options) => {
      if (role === 'navigation') {
        assert.deepEqual(options, { name: 'Workspace controls', exact: true }); return controls
      }
      if (role === 'tablist') {
        assert.equal(options.name, 'Workspace profile')
        if (controlsPresent && profileVisible) assert.equal(options.includeHidden, true)
        return { getByRole: (role, options) => {
          assert.equal(role, 'tab')
          if (controlsPresent && profileVisible) assert.equal(options.includeHidden, true)
          return legacy
        } }
      }
      if (role === 'heading') return { isVisible: async () => {
        if (options.name === 'What would you like to work on?') return chooserVisible
        assert.equal(options.name, 'Project board'); return boardVisible
      } }
      if (role === 'button') {
        assert.equal(options.name, 'Return to drawing')
        return { isVisible: async () => returnVisible }
      }
      assert.equal(role, 'tab')
      assert.deepEqual(options, { name: 'Solar CAD', exact: true })
      return { click: async options => {
        assert.deepEqual(options, { timeout: 15_000 }); events.push('Solar CAD')
      } }
    },
  }
  return { page, events, profileControl, legacy, environment,
    setPresent: value => { present = value }, setEntered: value => { entered = value } }
}

const workspaceOracleAssertions = locator => ({
  toBeVisible: async () => assert.equal(await locator.isVisible(), true),
  toContainText: async expected => assert.ok(locator.text.includes(expected)),
  toHaveValue: async expected => assert.equal(await locator.inputValue(), expected),
  toHaveAttribute: async (name, expected) => assert.equal(await locator.getAttribute(name), expected),
})

test('WL23 runProbe helpers are exported functions for the production guarded paths', () => {
  assert.equal(typeof absenceNamePattern, 'function')
  assert.equal(typeof resolveSurfaceControl, 'function')
})

test('WL23 absence identity ignores unrelated buttons and catches moved or hidden actions', () => {
  const buttons = Array.from({ length: 122 }, (_, index) => ({ name: `Other ${index}` }))
  for (const name of ['New drawing', 'Print', 'Save version', 'Save [version].+']) {
    const pattern = absenceNamePattern({ name })
    const absent = rows => assert.equal(rows.filter(button => pattern.test(button.name)).length, 0)
    absent(buttons)
    absent([...buttons, { name: `Other ${name}` }, { name: `${name} other` }])
    for (const hidden of [false, true]) for (const suffix of ['', ' (unavailable: No drawing)']) {
      assert.throws(() => absent([...buttons, { name: name + suffix, hidden, scope: 'elsewhere' }]), assert.AssertionError)
    }
  }
  const explicit = /^Only this refusal$/
  assert.equal(absenceNamePattern({ name: 'Print', unavailableName: explicit }), explicit)
  assert.equal(absenceNamePattern({ name: 'Print', unavailableName: 'Exact refusal' }), 'Exact refusal')
  assert.equal(absenceNamePattern({ name: 'Print (unavailable: Old reason)' }).test('Print'), true)
  assert.equal(absenceNamePattern({ name: /^refusal$/, availableName: 'Print' }).test('Print'), true)
  for (const locator of [undefined, {}, { name: '' }, { name: '  ' }, { name: /Print/ },
    { unavailableName: '' }, { unavailableName: null }]) {
    assert.throws(() => absenceNamePattern(locator), /stable action name/)
  }
})

const surfaceProbe = id => ({ kind: 'surface', sourceId: id, state: 'ready',
  locator: { role: 'tab', name: id, exact: true,
    scope: { role: 'tablist', name: 'Workspace profile', exact: true } } })

test('WL23 modern surface control selects the requested profile id despite hidden legacy tabs', async () => {
  for (const id of ['browser', 'lab', 'ops', 'cad', 'solar']) {
    const fake = workspaceEnvironmentFake({ legacyVisible: false })
    const resolved = await resolveSurfaceControl(fake.page, surfaceProbe(id))
    assert.equal(resolved.locator, fake.profileControl)
    await resolved.activate()
    await resolved.assertSelected(workspaceOracleAssertions)
    assert.equal(fake.events.includes(`Select profile:${id}`), true)
    assert.equal(fake.events.includes('Select legacy tab'), false)
    fake.profileControl.inputValue = async () => 'wrong'
    await assert.rejects(resolved.assertSelected(workspaceOracleAssertions), assert.AssertionError)
    fake.profileControl.inputValue = async () => id
    fake.environment.getAttribute = async () => 'wrong'
    await assert.rejects(resolved.assertSelected(workspaceOracleAssertions), assert.AssertionError)
  }
})

test('WL23 absent or hidden modern strip preserves legacy tab activation and selection checks', async () => {
  for (const options of [{ controlsPresent: false }, { profileVisible: false }]) {
    const fake = workspaceEnvironmentFake(options)
    const resolved = await resolveSurfaceControl(fake.page, surfaceProbe('solar'))
    assert.equal(resolved.locator, fake.legacy)
    await assert.rejects(resolved.assertSelected(workspaceOracleAssertions), assert.AssertionError)
    await resolved.activate()
    await resolved.assertSelected(workspaceOracleAssertions)
    assert.deepEqual(fake.events, ['Select legacy tab'])
  }
})

test('WL23 readiness requires the expected data-state on an attached hidden legacy badge', async () => {
  for (const state of ['available', 'beta', 'paused']) {
    for (const options of [{ status: state }, { status: 'wrong' }, { status: state, statusAttached: false }]) {
      const fake = workspaceEnvironmentFake({ legacyVisible: false, ...options })
      const resolved = await resolveSurfaceControl(fake.page, surfaceProbe('solar'))
      const check = () => assertSurfaceState(resolved.legacy, state, workspaceOracleAssertions)
      if (options.status === state && options.statusAttached !== false) await check()
      else await assert.rejects(check(), assert.AssertionError)
    }
  }
})

test('WL23 surface effect checks the modern profile and hidden legacy readiness together', async () => {
  for (const [id, state, label] of [['browser', 'available', 'Ready'], ['lab', 'available', 'Ready'],
    ['ops', 'available', 'Ready'], ['cad', 'paused', 'Execution paused'], ['solar', 'beta', 'Beta']]) {
    for (const statusAttached of [true, false]) {
      const fake = workspaceEnvironmentFake({ legacyVisible: false, status: state, statusLabel: label, statusAttached })
      const probe = { ...surfaceProbe(id), assertion: { target: `surface:${id}`, state, label } }
      const resolved = await resolveSurfaceControl(fake.page, probe)
      await resolved.activate()
      const check = () => assertEffect(probe, { page: fake.page }, resolved.locator, {}, workspaceOracleAssertions)
      if (statusAttached) await check()
      else await assert.rejects(check(), assert.AssertionError)
    }
  }
})

test('WL23 Start requires the visible chooser and exited environment, or the legacy board and Return', async () => {
  const probe = { assertion: { target: 'project-board' } }
  for (const options of [{ entered: 'false' }, { entered: 'true' }, { chooserVisible: false },
    { present: false }, { present: false, boardVisible: false }, { present: false, returnVisible: false }]) {
    const fake = workspaceEnvironmentFake(options)
    const check = () => assertEffect(probe, { page: fake.page }, null, {}, workspaceOracleAssertions)
    if (options.entered === 'true' || options.chooserVisible === false || options.boardVisible === false || options.returnVisible === false) {
      await assert.rejects(check(), assert.AssertionError)
    } else await check()
  }
})

test('WL23 project-board routes every environment and legacy check through injected assertions', async () => {
  const probe = { assertion: { target: 'project-board' } }
  for (const present of [true, false]) {
    const calls = []
    const environment = { count: async () => present ? 1 : 0 }
    const page = {
      locator: selector => {
        assert.equal(selector, 'section.workspace-environment')
        return environment
      },
      getByRole: (role, options) => ({ role, ...options }),
    }
    const assertions = locator => {
      const call = { locator }
      calls.push(call)
      return {
        toBeVisible: async () => { call.matcher = 'toBeVisible' },
        toHaveAttribute: async (...args) => { call.matcher = 'toHaveAttribute'; call.args = args },
      }
    }
    await assertEffect(probe, { page }, null, {}, assertions)
    assert.deepEqual(calls, present ? [
      { locator: { role: 'heading', name: 'What would you like to work on?', exact: true }, matcher: 'toBeVisible' },
      { locator: environment, matcher: 'toHaveAttribute', args: ['data-entered', 'false'] },
    ] : [
      { locator: { role: 'heading', name: 'Project board', exact: true }, matcher: 'toBeVisible' },
      { locator: { role: 'button', name: 'Return to drawing', exact: true }, matcher: 'toBeVisible' },
    ])
  }
})

function boundedWorkspacePoll({ passes = 4, beforePass = () => {} } = {}) {
  const polls = []
  const assertions = (value, message) => walkExpect(value, message)
  assertions.poll = (read, options) => ({ toBe: async expected => {
    const poll = { timeout: options.timeout, passes: 0 }
    polls.push(poll)
    for (let pass = 1; pass <= passes; pass++) {
      poll.passes = pass
      beforePass(pass, poll)
      if (await read() === expected) return
    }
    throw new Error('fake poll reached its timeout')
  } })
  return { polls, assertions }
}

function studioRailFake({ visible = true, expanded = 'false' } = {}) {
  const workspace = workspaceEnvironmentFake()
  const events = workspace.events
  class Locator {
    constructor(kind) { this.kind = kind; this._apiName = 'Locator' }
    async isVisible() { return visible }
    async getAttribute(name) {
      assert.equal(name, 'aria-expanded')
      assert.equal(visible, true, 'absent Studio must not be read')
      return expanded
    }
    async click() {
      events.push('Studio click')
      expanded = expanded === 'true' ? 'false' : 'true'
    }
    async _expect(expression, options) {
      if (this.kind === 'button') {
        assert.ok(['to.have.attribute', 'to.have.attribute.value'].includes(expression))
        assert.equal(options.expressionArg, 'aria-expanded')
        assert.equal(options.expectedText[0].string, 'true')
        assert.equal(expanded, 'true')
        events.push('Studio expanded wait')
      } else {
        assert.equal(expression, 'to.be.visible')
        assert.equal(expanded, 'true')
        events.push('Studio rail visible wait')
      }
      return { matches: true, received: expanded, log: [] }
    }
  }
  const button = new Locator('button')
  const page = {
    getByRole: (role, options) => {
      assert.equal(role, 'navigation')
      assert.deepEqual(options, { name: 'Workspace controls', exact: true })
      return { getByRole: (role, options) => {
        assert.equal(role, 'button')
        assert.deepEqual(options, { name: 'Studio', exact: true })
        return button
      } }
    },
    locator: selector => {
      assert.equal(selector, '#project-workspace-activity')
      return new Locator('rail')
    },
  }
  return { page, events, workspace, expanded: () => expanded }
}

test('Studio rail reveal opens once and waits for the expanded button and visible rail', async () => {
  const fake = studioRailFake()
  assert.equal(await revealStudioRail(fake.page), true)
  assert.deepEqual(fake.events, ['Studio click', 'Studio expanded wait', 'Studio rail visible wait'])
})

test('Studio rail reveal leaves an already open panel alone', async () => {
  const fake = studioRailFake({ expanded: 'true' })
  assert.equal(await revealStudioRail(fake.page), false)
  assert.deepEqual(fake.events, [])
})

test('Studio rail reveal tolerates builds without Workspace controls', async () => {
  const fake = studioRailFake({ visible: false })
  assert.equal(await revealStudioRail(fake.page), false)
  assert.deepEqual(fake.events, [])
})

function phonePanelFake({ modern = true, studio = false, content = true, drawer = 'none', ledger = true,
  navStaysVisible = false, sidebarStaysVisible = false } = {}) {
  const events = [], legacy = { Jobs: false, Plan: false }
  let view = false, more = false
  class Locator {
    constructor(kind) { this.kind = kind; this._apiName = 'Locator' }
    async isVisible() {
      if (this.kind === 'Studio') return modern
      if (this.kind === 'sidebar') return studio || sidebarStaysVisible
      if (this.kind === 'ledger') return studio && content && ledger
      if (this.kind === 'heading') return studio && content
      if (this.kind === 'nav') return studio || navStaysVisible
      if (this.kind === 'Close Studio') return studio
      if (this.kind === 'Entitlements') return drawer === 'plan' && content
      if (this.kind === 'View panel') return view && more
      if (this.kind === 'More panels') return view
      return true
    }
    async count() { return 1 }
    async inputValue() { assert.equal(this.kind, 'Command bar'); return 'keep this command' }
    async getAttribute(name) {
      if (name === 'data-drawer') return drawer
      if (this.kind === 'Studio') return String(studio)
      if (this.kind === 'View tab') return String(view)
      if (this.kind === 'More panels') return String(more)
      return String(legacy[this.kind])
    }
    async click() {
      events.push(this.kind)
      if (this.kind === 'Studio') studio = !studio
      else if (this.kind === 'Close Studio') studio = false
      else if (this.kind === 'View tab') view = true
      else if (this.kind === 'More panels') more = !more
      else if (this.kind === 'properties') {
        assert.ok(view && more, 'properties must be disclosed before activation')
        drawer = drawer === 'plan' ? 'none' : 'plan'
      } else legacy[this.kind] = !legacy[this.kind]
    }
    async _expect(expression, options) {
      if (expression === 'to.be.visible') assert.equal(await this.isVisible(), true)
      else {
        assert.ok(['to.have.attribute', 'to.have.attribute.value'].includes(expression))
        assert.equal(await this.getAttribute(options.expressionArg), options.expectedText[0].string)
      }
      return { matches: true, log: [] }
    }
  }
  const locator = kind => new Locator(kind)
  const toolbar = { getByRole: (role, options) => {
    if (role === 'group') { assert.equal(options.name, 'View'); return locator('View panel') }
    assert.ok(['More panels', 'properties'].includes(options.name))
    return locator(options.name)
  } }
  const page = {
    getByRole: (role, options) => {
      if (role === 'navigation') {
        assert.equal(options.name, 'Workspace controls')
        return { getByRole: (role, options) => { assert.equal(options.name, 'Studio'); return locator('Studio') } }
      }
      if (role === 'toolbar') { assert.equal(options.name, 'Drafting tools'); return toolbar }
      if (role === 'combobox') { assert.equal(options.name, 'Command bar'); return locator('Command bar') }
      if (role === 'tablist') {
        assert.equal(options.name, 'Ribbon')
        return { getByRole: (role, options) => { assert.equal(options.name, 'View'); return locator('View tab') } }
      }
      if (role === 'group') {
        assert.equal(options.name, 'Workspace panels')
        assert.equal(modern, false, 'modern panels must never use legacy tabs')
        return { getByRole: (role, options) => locator(options.name) }
      }
      if (role === 'region') {
        assert.ok(['Studio sidebar', 'Entitlements'].includes(options.name))
        return locator(options.name === 'Studio sidebar' ? 'sidebar' : 'Entitlements')
      }
      if (role === 'button') { assert.equal(options.name, 'Close Studio'); return locator('Close Studio') }
      assert.equal(role, 'heading'); assert.match('Job monitor', options.name)
      return locator('heading')
    },
    locator: selector => {
      assert.ok(['.app', '#project-workspace-activity', 'aside.rail .rail-ledger', 'aside.nav'].includes(selector))
      return locator(selector === '.app' ? 'app' : selector === '#project-workspace-activity' ? 'sidebar'
        : selector === 'aside.nav' ? 'nav' : 'ledger')
    },
  }
  const assertions = value => ({
    toHaveAttribute: async (name, expected) => assert.equal(await value.getAttribute(name), expected),
    toBeVisible: async () => assert.equal(await value.isVisible(), true),
    toBeHidden: async () => assert.equal(await value.isVisible(), false),
    toHaveValue: async expected => assert.equal(await value.inputValue(), expected),
  })
  return { page, events, assertions }
}

test('phone Jobs transitions are idempotent and require visible Studio content', async () => {
  const fake = phonePanelFake()
  for (const open of [true, true, false, false]) {
    await setPhonePanel(fake.page, 'Jobs', open, fake.assertions)
    assert.equal(await readPhonePanel(fake.page, 'Jobs'), open)
  }
  assert.deepEqual(fake.events, ['Studio', 'Close Studio'])
})

test('expanded Studio with hidden content cannot prove Jobs open', async () => {
  const fake = phonePanelFake({ studio: true, content: false })
  assert.equal(await readPhonePanel(fake.page, 'Jobs'), false)
  await assert.rejects(assertPhonePanel(fake.page, 'Jobs', true, fake.assertions), assert.AssertionError)
  await assert.rejects(setPhonePanel(fake.page, 'Jobs', true, fake.assertions), assert.AssertionError)
  assert.deepEqual(fake.events, [])
})

test('desktop Studio Jobs opens once, requires its ledger, and closes through Close Studio', async () => {
  const fake = phonePanelFake()
  for (const open of [true, true, false, false]) await setStudioDrawer(fake.page, 'jobs', open, fake.assertions)
  await assertStudioDrawer(fake.page, 'jobs', false, fake.assertions)
  assert.deepEqual(fake.events, ['Studio', 'Close Studio'])
  const missing = phonePanelFake({ ledger: false })
  await assert.rejects(setStudioDrawer(missing.page, 'jobs', true, missing.assertions), assert.AssertionError)
  assert.deepEqual(missing.events, ['Studio'])
  const stuck = phonePanelFake({ studio: true, sidebarStaysVisible: true })
  await assert.rejects(setStudioDrawer(stuck.page, 'jobs', false, stuck.assertions), assert.AssertionError)
  assert.deepEqual(stuck.events, ['Close Studio'])
})

test('Studio drawer recipes resolve Jobs and nav on both viewports without using legacy controls', async () => {
  const map = buildFeatureMap()
  for (const viewport of ['desktop', 'phone']) {
    for (const drawer of ['jobs', 'nav']) {
      for (const state of ['closed', 'open']) {
        const fake = phonePanelFake({ studio: state === 'open' })
        const probe = resolveProbe(map.entries.find(entry => entry.id === `drawer:${drawer}`), state)
        const locator = await studioDrawerProbeLocator(fake.page, probe)
        assert.equal(locator.kind, state === 'open' ? 'Close Studio' : 'Studio')
        await locator.click()
        const runtime = { page: fake.page, testInfo: { project: { name: viewport } } }
        await assertEffect(probe, runtime, locator, {}, fake.assertions)
        assert.deepEqual(fake.events, [state === 'open' ? 'Close Studio' : 'Studio'])
        assert.equal(await studioDrawerLocator(fake.page, drawer).isVisible(), state === 'closed')
      }
    }
  }
})

test('phone nav open closes via Close Studio and rejects a still-visible nav', async () => {
  const map = buildFeatureMap()
  const probe = resolveProbe(map.entries.find(entry => entry.id === 'drawer:nav'), 'open')
  for (const navStaysVisible of [false, true]) {
    const fake = phonePanelFake({ studio: true, navStaysVisible })
    const locator = await studioDrawerProbeLocator(fake.page, probe)
    assert.equal(locator.kind, 'Close Studio')
    await locator.click()
    const check = () => assertEffect(probe, { page: fake.page, testInfo: { project: { name: 'phone' } } },
      locator, {}, fake.assertions)
    if (navStaysVisible) await assert.rejects(check(), assert.AssertionError)
    else await check()
    assert.deepEqual(fake.events, ['Close Studio'])
  }
})

test('phone drawer:none resolves a visible Close Studio target while preserving keyboard Escape', async () => {
  const map = buildFeatureMap()
  const fake = phonePanelFake({ studio: true })
  const probe = resolveProbe(map.entries.find(entry => entry.id === 'drawer:none'), 'drawer-open')
  assert.equal(probe.locator.trigger, 'keyboard')
  assert.equal(probe.locator.key, 'Escape')
  assert.equal((await studioDrawerProbeLocator(fake.page, probe)).kind, 'Close Studio')
  const legacy = phonePanelFake({ modern: false })
  assert.equal(await studioDrawerProbeLocator(legacy.page, probe), null)
})

test('desktop Studio setup routes both job and tool rail states through the shared host', async () => {
  for (const setter of [setJobRail, setToolRail]) {
    const fake = phonePanelFake()
    for (const open of [true, true, false, false]) await setter(fake.page, open, false, fake.assertions)
    assert.deepEqual(fake.events, ['Studio', 'Close Studio'])
  }
})

test('legacy tool rail keeps desktop spine and phone Tool rail transitions without Studio', async () => {
  for (const phone of [false, true]) {
    let open = false
    const events = []
    const button = collapse => ({
      isVisible: async () => collapse ? open : phone || !open,
      getAttribute: async () => String(open),
      click: async () => { events.push(collapse ? 'Collapse the tool rail to a spine' : 'Tool rail'); open = !open },
    })
    const page = {
      getByRole: (role, options) => {
        if (role === 'navigation') return { getByRole: () => ({ isVisible: async () => false }) }
        if (role === 'toolbar') return { isVisible: async () => true }
        assert.equal(role, 'button')
        assert.ok(['Tool rail', 'Collapse the tool rail to a spine'].includes(options.name))
        return button(options.name !== 'Tool rail')
      },
      locator: selector => {
        assert.equal(selector, 'aside.nav')
        return { isVisible: async () => open, getAttribute: async () => String(!open) }
      },
    }
    const assertions = value => ({
      toBeVisible: async () => assert.equal(await value.isVisible(), true),
      toBeHidden: async () => assert.equal(await value.isVisible(), false),
      toHaveAttribute: async (name, expected) => assert.equal(await value.getAttribute(name), expected),
    })
    for (const desired of [false, true, false, false]) await setToolRail(page, desired, phone, assertions)
    assert.deepEqual(events, ['Tool rail', phone ? 'Tool rail' : 'Collapse the tool rail to a spine'])
  }
})

test('rail-expand selects the first visible family launcher and requires its contents after activation', async () => {
  for (const contents of [true, false]) {
    const fake = phonePanelFake()
    const registry = { isVisible: async () => false }
    const launchers = [false, true, true].map((visible, index) => ({
      isVisible: async () => visible,
      getAttribute: async () => `Open Family ${index} in the tool rail (2 tools)`,
      click: async () => { fake.events.push(`Family ${index}`); await setStudioDrawer(fake.page, 'nav', true, fake.assertions) },
    }))
    const page = { ...fake.page,
      getByRole: (role, options) => {
        if (options.name instanceof RegExp && options.name.test('Open Family 1 in the tool rail (2 tools)')) {
          return { count: async () => launchers.length, nth: index => launchers[index] }
        }
        if (options.name instanceof RegExp && options.name.test('Family 1 · 2 hide')) return { family: 'Family 1' }
        return fake.page.getByRole(role, options)
      },
      locator: selector => {
        if (selector !== 'aside.nav .section') return fake.page.locator(selector)
        return { filter: ({ has }) => {
          assert.equal(has.family, 'Family 1')
          return { locator: selector => {
            assert.equal(selector, '.section-body')
            return { isVisible: async () => contents }
          } }
        } }
      },
    }
    const probe = { featureId: 'action:rail-expand', state: 'ready', assertion: { target: 'drawer:nav', value: 'nav' } }
    const runtime = { page, testInfo: { project: { name: 'desktop' } } }
    const launcher = await railExpandLauncher(probe, runtime, registry)
    assert.equal(launcher, launchers[1])
    await launcher.click()
    assert.deepEqual(fake.events, ['Family 1', 'Studio'])
    assert.equal(runtime.railExpandFamily, 'Family 1')
    if (contents) await assertEffect(probe, runtime, launcher, {}, fake.assertions)
    else await assert.rejects(assertEffect(probe, runtime, launcher, {}, fake.assertions), assert.AssertionError)
    if (contents) await assertRailExpandFamily(page, 'Family 1', fake.assertions)
  }
})

test('rail-expand keeps the missing-control failure when neither registry control nor launcher is visible', async () => {
  const fake = phonePanelFake()
  const registry = { isVisible: async () => false }
  const runtime = { page: { getByRole: () => ({ count: async () => 0 }) },
    testInfo: { project: { name: 'desktop' } } }
  const probe = { featureId: 'action:rail-expand', state: 'ready' }
  assert.equal(await railExpandLauncher(probe, runtime, registry), null)
  await assert.rejects(fake.assertions(registry).toBeVisible(), assert.AssertionError)
  assert.equal(runtime.railExpandFamily, undefined)
  assert.equal(await railExpandLauncher(probe, runtime, { isVisible: async () => true }), null)
  runtime.testInfo.project.name = 'phone'
  assert.equal(await railExpandLauncher(probe, runtime, registry), null)
})

test('phone Plan discloses View and More panels and checks both content and drawer state', async () => {
  const fake = phonePanelFake()
  for (const open of [true, true, false, false]) {
    await setPhonePanel(fake.page, 'Plan', open, fake.assertions)
    assert.equal(await readPhonePanel(fake.page, 'Plan'), open)
  }
  assert.deepEqual(fake.events, ['View tab', 'More panels', 'properties', 'properties'])
  const hidden = phonePanelFake({ drawer: 'plan', content: false })
  await assert.rejects(assertPhonePanel(hidden.page, 'Plan', true, hidden.assertions), assert.AssertionError)
  await assert.rejects(assertPhonePanel(hidden.page, 'Plan', false, hidden.assertions), assert.AssertionError)
  const stale = phonePanelFake()
  const wrongDrawer = { ...stale.page, locator: () => ({ getAttribute: async () => 'plan' }) }
  await assert.rejects(assertPhonePanel(wrongDrawer, 'Plan', false, stale.assertions), assert.AssertionError)
  const visible = phonePanelFake({ drawer: 'plan' })
  const wrongOpenDrawer = { ...visible.page, locator: () => ({ getAttribute: async () => 'none' }) }
  await assert.rejects(assertPhonePanel(wrongOpenDrawer, 'Plan', true, visible.assertions), assert.AssertionError)
})

test('phone panels retain legacy Workspace panels transitions without Workspace controls', async () => {
  const fake = phonePanelFake({ modern: false })
  for (const panel of ['Jobs', 'Plan']) {
    for (const open of [true, true, false, false]) {
      await setPhonePanel(fake.page, panel, open, fake.assertions)
      assert.equal(await readPhonePanel(fake.page, panel), open)
    }
  }
  assert.deepEqual(fake.events, ['Jobs', 'Jobs', 'Plan', 'Plan'])
})

test('desktop drawer-state uses Studio for Jobs and retains the legacy Plan button', async () => {
  const legacy = phonePanelFake({ modern: false })
  const modern = phonePanelFake()
  const page = { ...modern.page, getByRole: (role, options) =>
    (role === 'group' ? legacy.page : modern.page).getByRole(role, options) }
  const runtime = { page, evidence: {}, testInfo: { project: { name: 'desktop' } } }
  for (const name of ['Jobs', 'Plan']) {
    for (const open of [true, true, false, false]) {
      await setupStep({ featureId: `drawer:${name.toLowerCase()}` }, runtime,
        { kind: 'drawer-state', name, open }, legacy.assertions)
    }
  }
  assert.deepEqual(legacy.events, ['Plan', 'Plan'])
  assert.deepEqual(modern.events, ['Studio', 'Close Studio'])
})

test('phone Properties oracle requires the inverse Plan content state on modern builds', async () => {
  for (const visible of [false, true]) {
    const fake = phonePanelFake({ drawer: visible ? 'none' : 'plan' })
    const runtime = { page: fake.page, evidence: {}, testInfo: { project: { name: 'phone' } } }
    const probe = { assertion: { target: 'properties-pane' } }
    await assertEffect(probe, runtime, null, { visible }, fake.assertions)
    assert.deepEqual(runtime.evidence.phoneProperties, { host: 'Plan', before: visible, after: !visible })
    await assert.rejects(assertEffect(probe, runtime, null, { visible: !visible }, fake.assertions), assert.AssertionError)
  }
})

test('phone Escape drawer recipes use Studio and oracles require both panels closed', async () => {
  for (const featureId of ['drawer:none', 'action:bar-escape']) {
    const fake = phonePanelFake()
    const runtime = { page: fake.page, evidence: {}, testInfo: { project: { name: 'phone' } } }
    const probe = { featureId, state: 'drawer-open', assertion: {
      target: featureId === 'drawer:none' ? 'drawer:none' : 'escape:drawer', value: 'none',
    } }
    await setupStep(probe, runtime, { kind: 'drawer-state', name: 'Catalog', open: true }, fake.assertions)
    assert.deepEqual(fake.events, ['Studio'])
    const before = { command: 'keep this command' }
    await assert.rejects(assertEffect(probe, runtime, null, before, fake.assertions), assert.AssertionError)
    await setPhonePanel(fake.page, 'Jobs', false, fake.assertions)
    await assertEffect(probe, runtime, null, before, fake.assertions)
    await setPhonePanel(fake.page, 'Plan', true, fake.assertions)
    await assert.rejects(assertEffect(probe, runtime, null, before, fake.assertions), assert.AssertionError)
    await setPhonePanel(fake.page, 'Plan', false, fake.assertions)
    if (featureId === 'action:bar-escape') {
      await assert.rejects(assertEffect(probe, runtime, null, { command: 'changed' }, fake.assertions), assert.AssertionError)
    }
  }
})

test('Studio rail features contain exactly the three rail controls', () => {
  assert.equal(Object.isFrozen(STUDIO_RAIL_FEATURES), true)
  assert.deepEqual([...STUDIO_RAIL_FEATURES].sort(), [
    'control:job-monitor-expand', 'control:linked-services', 'control:notification-collapse',
  ])
})

test('workspace entry continues an existing workspace and waits for entry', async () => {
  const { page, events } = workspaceEnvironmentFake()
  await enterWorkspaceEnvironment(page)
  assert.deepEqual(events, ['Continue workspace', 'assert:data-entered=true'])
})

test('workspace entry opens project intake, closes Project and waits for entry', async () => {
  const { page, events } = workspaceEnvironmentFake({ resume: false })
  await enterWorkspaceEnvironment(page)
  assert.deepEqual(events, ['Open project or material', 'Close Project panel',
    'assert:aria-expanded=false', 'assert:data-entered=true'])
})

test('workspace entry leaves absent and already entered environments alone', async () => {
  const { assertions } = boundedWorkspacePoll()
  for (const options of [{ present: false }, { entered: 'true' }]) {
    const { page, events } = workspaceEnvironmentFake(options)
    await enterWorkspaceEnvironment(page, assertions)
    assert.deepEqual(events, [])
  }
})

test('workspace entry waits for the environment to attach on the third poll pass', async () => {
  const fake = workspaceEnvironmentFake({ present: false })
  const { polls, assertions } = boundedWorkspacePoll({ beforePass: pass => fake.setPresent(pass >= 3) })
  await enterWorkspaceEnvironment(fake.page, assertions)
  assert.deepEqual(polls, [{ timeout: 20_000, passes: 3 }])
  assert.deepEqual(fake.events, ['Continue workspace', 'assert:data-entered=true'])
})

test('workspace entry tolerates an environment absent for the whole settle bound', async () => {
  const { page, events } = workspaceEnvironmentFake({ present: false })
  const { polls, assertions } = boundedWorkspacePoll()
  await enterWorkspaceEnvironment(page, assertions)
  assert.deepEqual(polls, [{ timeout: 20_000, passes: 4 }])
  assert.deepEqual(events, [])
})

test('workspace entry refuses a fourth entry click with a data-entered diagnostic', async () => {
  const { page, events } = workspaceEnvironmentFake({ settles: false })
  const { polls, assertions } = boundedWorkspacePoll({ passes: 8 })
  await assert.rejects(enterWorkspaceEnvironment(page, assertions), /data-entered.*3 clicks/)
  assert.deepEqual(polls, [{ timeout: 20_000, passes: 4 }])
  assert.equal(events.filter(event => event === 'Continue workspace').length, 3)
})

test('foreign checkout reload re-enters the chooser before restoring the ribbon tab', async () => {
  const fake = workspaceEnvironmentFake({ entered: 'true' })
  const { page, events } = fake
  const getByRole = page.getByRole
  const assertEntered = async () => assert.equal(
    await page.locator('section.workspace-environment').getAttribute('data-entered'), 'true')
  page.request = { post: async (path, options) => {
    assert.equal(path, '/api/drawings/private/checkout')
    assert.deepEqual(options, { data: { holder: 'walk-other-editor', ttl_s: 300 }, timeout: 15_000 })
    return { status: () => 200, json: async () => ({ acquired: true }) }
  } }
  page.reload = async options => {
    assert.equal(options, undefined)
    events.push('reload')
    fake.setEntered('false')
  }
  page.getByText = (text, options) => {
    assert.equal(text, 'Editing locked by walk-other-editor')
    assert.deepEqual(options, { exact: false })
    return { _apiName: 'Locator', _expect: async expression => {
      assert.equal(expression, 'to.be.visible')
      await assertEntered()
      events.push('lock visible')
      return { matches: true, log: [] }
    } }
  }
  page.getByRole = (role, options) => {
    if (role === 'tablist') {
      assert.deepEqual(options, { name: 'Ribbon', exact: true })
      return { getByRole: (role, options) => {
        assert.equal(role, 'tab')
        assert.deepEqual(options, { name: 'Manage', exact: true })
        return { click: async () => { await assertEntered(); events.push('Manage') } }
      } }
    }
    if (role === 'button' && options.name === 'More panels') return { isVisible: async () => false }
    return getByRole(role, options)
  }
  await setupStep({}, { page, evidence: {}, drawingId: 'private', ribbonTab: 'Manage' }, { kind: 'foreign-checkout' })
  assert.deepEqual(events, ['reload', 'Continue workspace', 'assert:data-entered=true', 'lock visible', 'Manage'])
})

test('reloadAndReenter reloads once without entry clicks on builds without an environment', async () => {
  const { page, events } = workspaceEnvironmentFake({ present: false })
  const options = { timeout: 60_000 }
  page.reload = async received => { assert.equal(received, options); events.push('reload') }
  await reloadAndReenter(page, options)
  assert.deepEqual(events, ['reload'])
})

test('signed-out session enters the chooser before Details without clicking Sign in', async () => {
  const { page, events } = workspaceEnvironmentFake({ resume: false })
  const getByRole = page.getByRole
  let detailsOpen = false
  const panel = { visible: () => detailsOpen,
    getByTestId: id => {
      assert.equal(id, 'diagnostics-block')
      return { text: 'session signed out' }
    },
    getByRole: (role, options) => {
      assert.equal(role, 'button')
      assert.deepEqual(options, { name: 'Refresh', exact: true })
      return { visible: () => detailsOpen }
    },
  }
  page.getByRole = (role, options) => {
    if (role === 'navigation') return getByRole(role, options)
    if (role === 'dialog') {
      assert.deepEqual(options, { name: 'Session · provenance', exact: true })
      return panel
    }
    assert.equal(role, 'button')
    assert.equal(options.exact, true)
    if (options.name === 'Sign out') return { count: 0 }
    assert.equal(options.name, 'Details', 'Sign in must never be used to enter the signed-out workspace')
    return { count: 1, click: async () => {
      assert.equal(await page.locator('section.workspace-environment').getAttribute('data-entered'), 'true')
      events.push('Details'); detailsOpen = true
    } }
  }
  const assertions = value => ({
    toHaveCount: async expected => assert.equal(value.count, expected),
    toBeVisible: async () => assert.equal(value.visible(), true),
    toHaveText: async expected => assert.match(value.text, expected),
  })
  await assertEffect({ assertion: { target: 'signed-out-session' } }, { page }, null, null, assertions)
  assert.deepEqual(events, ['Open project or material', 'Close Project panel',
    'assert:aria-expanded=false', 'assert:data-entered=true', 'Details'])
})

test('workspace chooser entry ignores Describe and returns false when no entry is offered', async () => {
  for (const options of [{ offered: false }, { present: false }, { entered: 'true' }]) {
    // The fake rejects any button lookup or click for Describe what to create.
    const { page, events } = workspaceEnvironmentFake(options)
    assert.equal(await enterWorkspaceIfChooser(page), false)
    assert.deepEqual(events, [])
  }
  for (const resume of [true, false]) {
    const { page, events } = workspaceEnvironmentFake({ resume })
    assert.equal(await enterWorkspaceIfChooser(page), true)
    assert.equal(events[0], resume ? 'Continue workspace' : 'Open project or material')
  }
})

test('private drawing canvas readiness re-enters after a workspace scope reset', async () => {
  const fake = workspaceEnvironmentFake()
  let canvasPasses = 0
  const { polls, assertions } = boundedWorkspacePoll()
  const page = { ...fake.page,
    goto: async () => {},
    request: {
      post: async () => ({ status: () => 202, json: async () => ({ drawing_id: 'scope-reset-drawing' }) }),
      get: async () => ({ ok: () => true, json: async () => ({ status: 'ready' }) }),
    },
    getByRole: (role, options) => {
      if (role !== 'region') return fake.page.getByRole(role, options)
      return { locator: () => ({ boundingBox: async () => {
        if (++canvasPasses === 1) {
          fake.setEntered('false')
          return null
        }
        assert.equal(fake.events.filter(event => event === 'Continue workspace').length, 2)
        return { width: 100, height: 100 }
      } }) }
    },
  }
  const runtime = { page, evidence: {} }
  await setupStep({ setup: { steps: [], context: {} } }, runtime, { kind: 'open-private-drawing', surface: 'cad' }, assertions)
  assert.equal(canvasPasses, 2)
  assert.equal(fake.events.filter(event => event === 'Continue workspace').length, 2)
  assert.deepEqual(polls, [
    { timeout: 20_000, passes: 1 }, { timeout: 60_000, passes: 2 },
  ])
  assert.equal(runtime.evidence.drawingId, 'scope-reset-drawing')
})

test('Solar profile selection proves the environment profile and reuses workspace entry', async () => {
  for (const resume of [true, false]) {
    const { page, events } = workspaceEnvironmentFake({ resume })
    await openSolarCadProfile(page)
    assert.deepEqual(events, ['Select Solar CAD', 'assert:data-profile=solar',
      ...(resume ? ['Continue workspace'] : ['Open project or material', 'Close Project panel', 'assert:aria-expanded=false']),
      'assert:data-entered=true'])
  }
  const { page, events } = workspaceEnvironmentFake({ controlsPresent: false })
  await openSolarCadProfile(page)
  assert.deepEqual(events, ['Solar CAD'])
})

test('empty workspace setup enters immediately after navigation', async () => {
  const { page, events } = workspaceEnvironmentFake()
  page.goto = async url => { assert.equal(url, '/try?surface=cad'); events.push('navigate') }
  await setupStep({}, { page, evidence: {} }, { kind: 'open-empty-workspace', surface: 'cad' })
  assert.deepEqual(events, ['navigate', 'Continue workspace', 'assert:data-entered=true'])
})

function stagingVersionSelectionFake({ loads = true } = {}) {
  const events = [], canvas = { visible: true, count: 1, evaluate: async () => true }
  let loaded = false, polls = 0
  const versionCount = { count: async () => {
    loaded = loads && ++polls > 1
    events.push(loaded ? 'versions:loaded' : 'versions:loading')
    return loaded ? 1 : 0
  } }
  const assertions = (value, message) => value === versionCount ? {
    toHaveCount: async (expected, options) => {
      assert.deepEqual(options, { timeout: 30_000 })
      for (let attempt = 0; attempt < 2; attempt++) {
        if (await value.count() === expected) return
      }
      assert.fail(message)
    },
  } : stagingAssertions(value)
  const page = { getByRole: (role, { name, exact }) => {
    if (role === 'region') {
      assert.equal(name, 'Project workspace')
      assert.equal(exact, true)
      return { locator: selector => {
        assert.equal(selector, '[data-tile="versions"] > strong')
        return versionCount
      } }
    }
    return { click: async () => {
      assert.equal(loaded, true, 'navigation must wait for loaded versions')
      events.push(name)
    } }
  },
    getByTestId: () => ({ visible: true }), locator: selector => selector === '[data-pane="versions"]'
      ? { getByRole: (_role, { name }) => ({ click: async () => {
        assert.ok(name.test(`v2 · ${stagingIds[3]}`)); events.push('version')
      } }) } : selector.includes('canvas') ? canvas : { count: async () => 0, visible: true, text: stagingIds[3] } }
  return { runtime: { page, projectSolar: { binding: stagingBinding } }, assertions, events }
}

test('shared staging version selection traverses Versions and Solar CAD before checking the bound canvas', async () => {
  const { runtime, assertions, events } = stagingVersionSelectionFake()
  await selectProjectSolarVersion(runtime, assertions)
  assert.deepEqual(events, ['versions:loading', 'versions:loaded', 'Open versions', 'version', 'Solar CAD'])
})

test('shared staging version selection rejects a project that never loads before opening Versions', async () => {
  const { runtime, assertions, events } = stagingVersionSelectionFake({ loads: false })
  await assert.rejects(selectProjectSolarVersion(runtime, assertions), {
    message: `project ${stagingBinding.project_id} did not open in the UI within 30 s`,
  })
  assert.deepEqual(events, ['versions:loading', 'versions:loading'])
})

test('shared checkout adapter always registers UI release once the lease is acquired', async () => {
  for (const badBinding of [false, true]) {
    const events = []
    const runtime = { projectSolar: { binding: stagingBinding, apiBase: stagingBase }, evidence: { projectSolar: { steps: [] } },
      cleanup: [], page: { getByRole: () => ({ getByRole: (_role, { name }) => ({
        click: async () => events.push(name), isEnabled: async () => true,
      }) }) } }
    const action = async (_page, base, method, path, click) => {
      assert.equal(base, stagingBase)
      assert.equal(path, `/api/projects/${stagingIds[1]}/drawings/${stagingIds[2]}/checkout`)
      await click()
      return stagingResponse(path, method === 'POST' ? { acquired: true, ...stagingBinding,
        project_id: badBinding ? stagingIds[0] : stagingIds[1], checkout: { fence: '1' } }
        : { released: true, checkout: null })
    }
    if (badBinding) await assert.rejects(takeProjectSolarCheckout(runtime, stagingAssertions, action))
    else await takeProjectSolarCheckout(runtime, stagingAssertions, action)
    assert.equal(runtime.cleanup.length, 1)
    await runtime.cleanup[0]()
    assert.deepEqual(events, ['Take', 'Release'])
  }
})

test('shared tool opener reports a disabled Solve control only when refusal is permitted', async () => {
  const control = { isVisible: async () => true, isEnabled: async () => false, click: () => assert.fail('disabled tool') }
  const page = { locator: selector => selector.includes('editor') ? { isVisible: async () => false } : control,
    getByRole: (role) => role === 'tab' ? { click: async () => {} } : { isVisible: async () => false },
    getByTestId: () => ({ locator: () => control }) }
  assert.equal(await openProjectSolarTool({ page }, 'solar-solve-proposal', stagingAssertions, { allowDisabled: true }), false)
  await assert.rejects(openProjectSolarTool({ page }, 'solar-solve-proposal', stagingAssertions))
})

test('shared publication supports initialize and existing scratch graphs through the staging adapters', async () => {
  for (const scenario of ['initialize', 'edit', 'run-failure']) {
    const mode = scenario === 'run-failure' ? 'edit' : scenario
    const events = [], values = {}
    const next = { ...stagingBinding, input_version_id: stagingIds[5] }
    const clickable = name => ({ isVisible: async () => true, isEnabled: async () => true,
      click: async () => events.push(name) })
    const settings = { getAttribute: async () => mode,
      getByTestId: id => ({ textContent: async () => {
        assert.equal(events.includes('Release'), false)
        return `${id}: checkout still held lco1.secret`
      } }),
      getByLabel: name => ({ fill: async value => { values[name] = value } }),
      getByRole: (role, { name, exact }) => role === 'combobox' ? { selectOption: async value => {
        assert.equal(name, 'Drawing units'); assert.equal(exact, true); values[name] = value
      } } : clickable(name) }
    const runtime = { cleanup: [], evidence: { projectSolar: { steps: [] } }, projectSolar: {
      binding: stagingBinding, apiBase: stagingBase, context: { graph: mode === 'edit' ? stagingGraph() : null },
    } }
    runtime.page = {
      getByRole: (role, { name }) => role === 'group' ? { getByRole: (_role, { name }) => clickable(name) } : clickable(name),
      getByTestId: name => name === 'solar-settings-form' ? settings : name === 'drafting-ribbon'
        ? { locator: () => clickable('settings') } : { visible: true },
      locator: selector => selector.includes('editor') ? { isVisible: async () => false } : selector.includes('canvas')
        ? { count: 1, visible: true, evaluate: async () => true } : { count: async () => 0, visible: true, text: stagingIds[5] },
    }
    const publication = publishProjectSolar(runtime, stagingAssertions, { allowExistingGraph: true,
      action: async (_page, _base, method, path, click) => {
        await click()
        return stagingResponse(path, method === 'POST' ? { acquired: true, ...stagingBinding, checkout: { fence: '7' } }
          : { released: true, checkout: null })
      }, run: async options => {
        await options.action()
        if (scenario === 'run-failure') throw new Error('no POST /api/run within 30 s')
        options.checkParams({ changes: { panels_in_sequence: mode === 'edit' ? 2 : 1 },
          ...(mode === 'initialize' ? { initialize: { units: { drawing_units: 'm' } } } : {}) })
        return { binding: next, context: { head: stagingIds[5] }, step: { output_version_id: stagingIds[5] } }
      } })
    if (scenario === 'run-failure') {
      await assert.rejects(publication, error => {
        assert.match(error.message, /no POST \/api\/run within 30 s/)
        assert.match(error.message, /Solar settings before cleanup:/)
        assert.match(error.message, /checkout still held \[redacted\]/)
        assert.match(error.message, /"startEnabled":"true"/)
        assert.match(error.message, /"releaseEnabled":"true"/)
        return true
      })
      await runtime.cleanup[0]()
      assert.equal(events.at(-1), 'Release')
      continue
    }
    await publication
    assert.equal(values['Panels in sequence'], mode === 'edit' ? '2' : '1')
    assert.equal(values['Drawing units'], mode === 'initialize' ? 'm' : undefined)
    assert.ok(events.includes(mode === 'edit' ? 'Apply settings' : 'Start Solar design'))
    assert.equal(runtime.evidence.projectSolar.publishedHead, stagingIds[5])
    await runtime.cleanup[0]()
    assert.equal(events.at(-1), 'Release')
  }
})

test('shared publication reports settings diagnostics when its button never enables', async () => {
  for (const mode of ['initialize', 'edit']) {
    const buttonName = mode === 'initialize' ? 'Start Solar design' : 'Apply settings'
    const timeoutError = new Error('button enable wait timed out')
    const reads = []
    const clickable = { isVisible: async () => true, isEnabled: async () => true, click: async () => {} }
    const start = { click: () => assert.fail('disabled publication must not submit') }
    const settings = { getAttribute: async () => mode,
      getByLabel: () => ({ fill: async () => {} }),
      getByRole: (role, { name }) => {
        if (role === 'combobox') return { selectOption: async () => {} }
        assert.equal(name, buttonName)
        return start
      },
      getByTestId: testId => ({ count: async () => 1, textContent: async options => {
        assert.deepEqual(options, { timeout: 2_000 })
        reads.push(testId)
        return testId === 'solar-settings-reason' ? '  Drawing calibration is required.  ' : `  ${'r'.repeat(310)}  `
      } }),
    }
    const runtime = { cleanup: [], evidence: { projectSolar: { steps: [] } }, projectSolar: {
      binding: stagingBinding, apiBase: stagingBase, context: { graph: null },
    }, page: {
      getByRole: role => role === 'group' ? { getByRole: () => clickable } : clickable,
      getByTestId: name => name === 'solar-settings-form' ? settings : { locator: () => clickable },
      locator: () => ({ isVisible: async () => false }),
    } }
    const assertions = value => value === start ? { toBeEnabled: async options => {
      assert.deepEqual(options, { timeout: 30_000 })
      throw timeoutError
    } } : stagingAssertions(value)
    assertions.poll = stagingAssertions.poll
    await assert.rejects(publishProjectSolar(runtime, assertions, { allowExistingGraph: true,
      action: async (_page, _base, _method, path, click) => {
        await click()
        return stagingResponse(path, { acquired: true, ...stagingBinding, checkout: { fence: '7' } })
      }, run: () => assert.fail('disabled publication must not run'),
    }), error => {
      assert.equal(error.cause.cause, timeoutError)
      assert.equal(error.cause.message, `${buttonName} did not become enabled within 30 s; solar-settings-reason: Drawing calibration is required.; solar-settings-run: ${'r'.repeat(300)}`)
      assert.ok(error.message.includes('Drawing calibration is required.'))
      return true
    })
    assert.deepEqual(reads, ['solar-settings-reason', 'solar-settings-run'])
  }
})

test('proposal review attempts an enabled UI solve even when the catalog says not runnable; never commits', async () => {
  for (const scenario of ['disabled-control', 'disabled-fields', 'disabled-submit', 'ui-refusal', 'http-refusal', 'job-refusal', 'review']) {
    const events = [], fields = {}
    const page = new EventEmitter()
    let status = ''
    const submit = { isEnabled: async () => scenario !== 'disabled-submit', click: async () => {
      events.push('Run solve')
      if (scenario === 'ui-refusal') { status = 'The server refused this Solar request. (SOLVE_CONTEXT_REQUIRED)'; return }
      page.emit('response', stagingResponse('/api/run', scenario === 'http-refusal'
        ? { error: { reason_code: 'CLOUD_OFFLINE' } } : { job_id: stagingIds[4] }, scenario === 'http-refusal' ? 403 : 202, 'POST'))
    } }
    page.getByRole = (role, { name } = {}) => role === 'form' ? {
      getByLabel: label => ({ isEnabled: async () => scenario !== 'disabled-fields',
        fill: async value => { fields[label] = value }, selectOption: async value => { fields[label] = value } }),
      getByRole: (role, { name, exact } = {}) => role === 'status' ? { allTextContents: async () => [status] }
        : role === 'combobox' ? { selectOption: async value => {
          assert.equal(name, 'Phase'); assert.equal(exact, true); fields[name] = value
        } } : submit,
    } : { visible: true, click: () => assert.fail(`must never click ${name}`) }
    const runtime = { page, projectSolar: { apiBase: stagingBase, binding: stagingBinding, context: { graph: null },
      catalog: [{ runnable: false, reason_code: 'SOLVE_CONTEXT_REQUIRED' }] } }
    const result = await reviewStagingProposal(runtime, { assertions: stagingAssertions,
      open: async (_runtime, tool, _assertions, options) => {
        events.push('open')
        assert.equal(tool, 'solar-solve-proposal'); assert.equal(options.allowDisabled, true)
        return scenario !== 'disabled-control'
      }, checkout: async () => events.push('checkout'), read: async () => ({
        job_id: stagingIds[4], org_id: stagingIds[0], project_id: stagingIds[1], input_version_id: stagingIds[3],
        tool_name: 'solar-solve-proposal', status: scenario === 'job-refusal' ? 'failed' : 'complete',
        error: scenario === 'job-refusal' ? { reason_code: 'CLOUD_OFFLINE' } : null,
      }) })
    const expected = scenario.startsWith('disabled') ? 'run_solve_disabled' : scenario === 'ui-refusal' ? 'SOLVE_CONTEXT_REQUIRED'
      : scenario === 'review' ? undefined : 'CLOUD_OFFLINE'
    assert.equal(events[0], 'checkout')
    assert.equal(events[1], 'open')
    assert.equal(result.reason, expected)
    assert.equal(events.includes('Run solve'), !scenario.startsWith('disabled'))
    assert.equal(page.listenerCount('response'), 0)
    if (scenario === 'review') assert.equal(result.reviewVisible, true)
  }
})

function authHeader(token) { const headers = {}; headers.Authorization = `Bearer ${token}`; return headers }

test('WL16n setup headers preserve the tenant and read the current page bearer only for its own origin under live auth', async () => {
  const originalStorage = globalThis.localStorage
  let token = 'page-token', reads = 0
  globalThis.localStorage = { getItem: (key) => {
    assert.equal(key, 'leaf.jwt')
    reads++
    return token
  } }
  const runtime = { stack: { baseURL: 'https://walk.example:8230' }, workerFacts: {}, page: {
    url: () => 'https://walk.example:8230/app?surface=cad', evaluate: async (callback) => callback(),
  } }
  const tenant = { 'X-Tenant-Id': 'demo-tenant' }
  try {
    for (const authLive of [undefined, false]) {
      runtime.workerFacts.authLive = authLive
      assert.deepEqual(await setupRequestHeaders(runtime, '/api/orgs'), tenant)
    }
    assert.equal(reads, 0)
    runtime.workerFacts.authLive = true
    for (const path of ['/api/drawings/upload', '/api/drawings/private/upload-status',
      '/api/orgs', 'https://walk.example:8230/api/orgs']) {
      assert.deepEqual(await setupRequestHeaders(runtime, path), { ...tenant, ...authHeader('page-token') })
    }
    token = 'refreshed-page-token'
    assert.deepEqual(await setupRequestHeaders(runtime, '/api/orgs'), { ...tenant, ...authHeader('refreshed-page-token') })
    for (const path of ['https://elsewhere.example/api/orgs', '//elsewhere.example/api/orgs',
      'http://walk.example:8230/api/orgs', 'https://walk.example:8231/api/orgs']) {
      assert.deepEqual(await setupRequestHeaders(runtime, path), tenant)
    }
    assert.equal(reads, 5, 'foreign origins must not read the bearer')
    runtime.page.url = () => 'https://elsewhere.example/app'
    assert.deepEqual(await setupRequestHeaders(runtime, '/api/orgs'), tenant,
      'relative API requests resolve against the stack baseURL, not a foreign page')
    assert.equal(reads, 5)
    runtime.page.url = () => 'https://walk.example:8230/app'
    for (const missing of [null, '', '   ']) {
      token = missing
      await assert.rejects(setupRequestHeaders(runtime, '/api/orgs'), {
        name: 'WalkSetupAuthError', message: /setup_auth_token_missing: \/api\/orgs requires leaf.jwt/,
      })
    }
    delete runtime.workerFacts
    runtime.evidence = { workerFacts: { authLive: true } }
    token = 'evidence-page-token'
    assert.deepEqual(await setupRequestHeaders(runtime, '/api/orgs'), { ...tenant, ...authHeader('evidence-page-token') })
  } finally { globalThis.localStorage = originalStorage }
})

test('WL16n project recipe uploads and extraction reads use setup headers, loading the page origin before live-auth uploads', async () => {
  const map = buildFeatureMap()
  const solar = map.entries.find((entry) => entry.id === 'surface:solar')
  const probes = ['project-solar-open', 'project-solar-published', 'project-solar-form-switch']
    .map((state) => resolveProbe(solar, state))
  probes.push(resolveProbe(map.entries.find((entry) => entry.id === 'action:bar-escape'), 'project-open'))
  for (const probe of probes) for (const authLive of [false, true]) {
    const calls = []
    const environment = workspaceEnvironmentFake()
    let currentURL = 'about:blank'
    const runtime = { stack: { baseURL: 'http://walk' }, workerFacts: { authLive }, evidence: {}, cleanup: [],
      ...(probe.setup.context.projectSolar ? { projectSolar: {} } : {}), page: {
        url: () => currentURL,
        goto: async (path) => {
          assert.deepEqual(environment.events, [])
          calls.push(['navigate', path]); currentURL = new URL(path, 'http://walk').href
        },
        locator: environment.page.locator,
        evaluate: async () => {
          assert.equal(new URL(currentURL).origin, 'http://walk')
          return 'upload-page-token'
        },
        request: {
          post: async (path, options) => {
            calls.push(['upload', path])
            assert.equal(path, '/api/drawings/upload')
            assert.deepEqual(options.headers, { 'X-Tenant-Id': 'demo-tenant',
              ...(authLive ? authHeader('upload-page-token') : {}) })
            assert.equal(options.timeout, 60_000)
            return { status: () => 202, json: async () => ({ drawing_id: 'private-drawing' }) }
          },
          get: async (path, options) => {
            calls.push(['extraction', path])
            assert.equal(path, '/api/drawings/private-drawing/upload-status')
            assert.deepEqual(options.headers, { 'X-Tenant-Id': 'demo-tenant',
              ...(authLive ? authHeader('upload-page-token') : {}) })
            assert.equal(options.timeout, 30_000)
            return { ok: () => true, json: async () => ({ status: 'ready' }) }
          },
        },
        getByRole: () => ({ locator: () => ({ boundingBox: async () => {
          assert.deepEqual(environment.events, ['Continue workspace', 'assert:data-entered=true'])
          return { width: 100, height: 100 }
        } }) }),
      } }
    await setupStep(probe, runtime, probe.setup.steps[0])
    assert.deepEqual(calls, [
      ...(authLive ? [['navigate', '/try?surface=cad']] : []),
      ['upload', '/api/drawings/upload'], ['extraction', '/api/drawings/private-drawing/upload-status'],
      ['navigate', '/app?drawing=private-drawing&surface=cad'],
    ])
    assert.equal(runtime.evidence.drawingId, 'private-drawing')
  }
})

test('WL16b canonical publication requires a complete bound job, a distinct output and the current head', () => {
  const ids = [1, 2, 3, 4, 5, 6].map((id) => `00000000-0000-4000-8000-${String(id).padStart(12, '0')}`)
  const binding = { organization_id: ids[0], project_id: ids[1], drawing_id: ids[2], input_version_id: ids[3] }
  const job = { job_id: ids[4], org_id: ids[0], project_id: ids[1], input_version_id: ids[3], tool_name: 'solar-settings',
    execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: ids[2] }, status: 'complete', error: null,
    result: { output_version_id: ids[5] } }
  assert.equal(inspectJob(job, binding, ids[4], 'solar-settings'), ids[5])
  assert.equal(inspectJob({ ...job, status: 'running' }, binding, ids[4], 'solar-settings'), null)
  for (const patch of [{ status: 'failed' }, { project_id: ids[0] }, { input_version_id: ids[5] },
    { result: { output_version_id: ids[3] } }, { error: { reason_code: 'FAILED' } }]) {
    assert.throws(() => inspectJob({ ...job, ...patch }, binding, ids[4], 'solar-settings'), /job/)
  }
  const next = { ...binding, input_version_id: ids[5] }
  const context = { context: { ...next, head_version_id: ids[5], is_head: true, intake_sha256: 'a'.repeat(64) },
    intake: { solar_design_graph: { rev: 1, panels: [], strings: [], inverters: [], routes: [], schedules: [] },
      solar_design_graph_sha256: 'b'.repeat(64) } }
  assert.equal(inspectContext(context, next).head, ids[5])
  for (const patch of [{ head_version_id: ids[3] }, { is_head: false }, { drawing_id: ids[0] }]) {
    assert.throws(() => inspectContext({ ...context, context: { ...context.context, ...patch } }, next), /context binding/)
  }
})

test('WL16b setup failures name the step and real server response without becoming unavailable', async () => {
  for (const [status, body] of [[403, { error: { reason_code: 'PROJECT_FORBIDDEN' } }],
    [409, { detail: 'checkout held elsewhere' }], [503, { detail: 'worker offline' }]]) {
    const runtime = { evidence: {} }
    await assert.rejects(projectSolarStep(runtime, 'publish-project-solar', () => projectSolarResponse({
      status: () => status, json: async () => body,
    }, 'POST /api/run', [202])), (error) => {
      assert.ok(!(error instanceof UnsupportedLocalError))
      assert.match(error.message, /publish-project-solar: POST \/api\/run: HTTP/)
      assert.ok(error.message.includes(String(status)))
      assert.ok(error.message.includes(JSON.stringify(body)))
      return true
    })
    assert.equal(runtime.evidence.projectSolar.steps[0].complete, undefined)
    assert.ok(runtime.evidence.projectSolar.steps[0].error)
  }
  const response = { status: () => 202, json: async () => ({ job_id: 'accepted' }) }
  assert.deepEqual(await projectSolarResponse(response, 'run', [202]), { job_id: 'accepted' })
  await assert.rejects(projectSolarResponse({ status: () => 500, json: async () => { throw new Error('not JSON') } },
    'canonical import', [201]), /canonical import: HTTP 500.*response JSON unreadable/)
  const runtime = { evidence: {}, projectSolar: { drainDiagnostics: async () => {
    runtime.evidence.projectSolar.serverFailure = { path: '/api/jobs/id', status: 500, body: { error: 'real failure' } }
  } } }
  await assert.rejects(projectSolarStep(runtime, 'job publication', async () => { throw new Error('read failed') }),
    /job publication: read failed; server response.*real failure/)
  const apiRuntime = { evidence: {} }
  await assert.rejects(projectSolarStep(apiRuntime, 'create org', async () => {
    await projectSolarResponse({ status: () => 201, json: async () => ({ org: null }) }, 'org receipt', [201], apiRuntime)
    throw new Error('org identity missing')
  }), /create org: org identity missing; server response.*"status":201.*"org":null/)
})

test('WL16b diagnostics observe drawing and project setup before canonical import and release listeners', async () => {
  for (const [path, status, body] of [
    ['/api/drawings/upload', 403, { error: 'upload refused' }],
    ['/api/drawings/private/upload-status', 200, { status: 'failed', error: 'extraction failed' }],
    ['/api/orgs', 500, { detail: 'workspace failed' }],
    ['/api/projects', 409, { detail: 'project refused' }],
    ['/api/projects/project/drawing-versions/import', 422, { detail: 'import refused' }],
    ['/api/jobs/00000000-0000-4000-8000-000000000001', 200, { status: 'failed', error: 'worker failed' }],
    ['/api/projects/project/drawing-versions/version/context', 404, { detail: 'context missing' }],
  ]) {
    const page = new EventEmitter()
    const runtime = { page, stack: { baseURL: 'http://127.0.0.1:8230' }, evidence: {}, cleanup: [] }
    observeProjectSolarResponses(runtime)
    const response = { url: () => `${runtime.stack.baseURL}${path}`, status: () => status, json: async () => body }
    page.emit('response', response)
    await assert.rejects(projectSolarStep(runtime, 'setup under test', async () => { throw new Error('step failed') }),
      (error) => error.message.includes(path) && error.message.includes(body.error || body.detail))
    const proof = runtime.evidence.projectSolar
    assert.equal(proof.serverFailure.status, status)
    assert.equal(proof.steps[0].complete, undefined)
    for (const cleanup of runtime.cleanup.reverse()) await cleanup()
    assert.equal(page.listenerCount('response'), 0)
  }
  const page = new EventEmitter()
  const runtime = { page, stack: { baseURL: 'http://127.0.0.1:8230' }, evidence: {}, cleanup: [] }
  observeProjectSolarResponses(runtime)
  page.emit('response', { url: () => `${runtime.stack.baseURL}/api/projects/project/drawings/drawing/checkout`,
    status: () => 200, json: async () => assert.fail('successful checkout capabilities must not enter diagnostics') })
  await runtime.projectSolar.drainDiagnostics()
  assert.equal(runtime.evidence.projectSolar.lastCanonicalResponse, undefined)
  for (const cleanup of runtime.cleanup.reverse()) await cleanup()
})

test('WL16b drawing oracle refuses an old displayed UUID or an empty canonical canvas', async () => {
  const binding = { input_version_id: 'published-uuid' }
  for (const fault of [null, 'old-title', 'empty-canvas']) {
    const runtime = { projectSolar: { binding }, page: {
      locator: (selector) => ({ selector, count: async () => 0, evaluate: async () => fault !== 'empty-canvas' }),
      getByTestId: (selector) => ({ selector }),
    } }
    const checked = []
    const assertions = (value) => ({
      toBeVisible: async () => checked.push(value.selector),
      toHaveCount: async (count) => assert.equal(count, 1),
      toContainText: async (uuid) => assert.equal(uuid, fault === 'old-title' ? 'old-uuid' : binding.input_version_id),
      toBe: (expected) => assert.equal(value, expected),
    })
    if (fault) await assert.rejects(assertProjectSolarDrawing(runtime, assertions), assert.AssertionError)
    else {
      await assertProjectSolarDrawing(runtime, assertions)
      assert.deepEqual(checked, ['.project-solar-workspace', 'solar-flow-rail', 'solar-flow-seat', '.studio-ground .viewer-canvas canvas'])
    }
  }
})

test('project Solar drawing opens a collapsed flow seat once and leaves open or absent seats alone', async () => {
  for (const initial of ['false', 'true', null]) {
    let expanded = initial, clicks = 0
    const toggle = { count: async () => initial === null ? 0 : 1,
      getAttribute: async key => { assert.equal(key, 'aria-expanded'); return expanded },
      click: async () => { clicks++; expanded = 'true' } }
    const runtime = { projectSolar: { binding: stagingBinding }, page: {
      locator: selector => selector.includes('solar-flow-seat-toggle') ? toggle
        : selector.includes('canvas') ? { count: 1, visible: true, evaluate: async () => true }
          : { visible: true, text: stagingIds[3] },
      getByTestId: () => ({ visible: true }),
    } }
    await assertProjectSolarDrawing(runtime, stagingAssertions)
    await assertProjectSolarDrawing(runtime, stagingAssertions)
    assert.equal(clicks, initial === 'false' ? 1 : 0)
  }
})

test('WL16b form switching detects stale drafts and never clicks a submit control', async () => {
  for (const retainDraft of [false, true]) {
    let active = null
    const edits = [], clicks = []
    const phases = ['initial', 'background']
    const fields = () => ({ 'Grant ref': '', Request: '', Frame: '', Phase: 'initial', Memberships: '' })
    let values = fields()
    const solveNames = ['Grant ref', 'Request', 'Frame', 'Phase']
    const node = (kind, name) => ({
      isVisible: async () => kind === 'editor' ? active !== null : kind !== 'more',
      getByRole: (role, options) => node(role, options.name),
      getByLabel: (label) => node('field', label),
      locator: (selector) => selector === 'option:checked' ? node('option', name)
        : node('tool', selector.match(/data-tool="([^"]+)"/)[1]),
      click: async () => {
        clicks.push(name)
        if (name instanceof RegExp) active = null
        if (kind === 'tool') { active = name; if (!retainDraft) values = fields() }
        assert.ok(!['Run solve', 'Correct strings'].includes(name), 'form switch must never submit')
      },
      fill: async (value) => { edits.push(name); values[name] = value },
      selectOption: async ({ label }) => {
        assert.equal(name, 'Phase')
        assert.ok(phases.includes(label), `unknown Phase option: ${label}`)
        edits.push(name); values[name] = label
      },
      value: () => values[name],
      count: () => kind === 'editor' ? Number(active !== null)
        : Number(active === (solveNames.includes(name) ? 'solar-solve-proposal' : 'solar-correct-string')),
    })
    const runtime = { evidence: { projectSolar: { steps: [] } }, projectSolar: { traffic: { runCount: 1 } }, page: {
      locator: () => node('editor', 'editor'), getByTestId: () => node('ribbon', 'ribbon'),
      getByLabel: (name) => node('field', name),
      getByRole: (role, options) => node(options.name === 'More panels' ? 'more' : role, options.name),
    } }
    const assertions = (value) => ({
      toBe: (expected) => assert.equal(value, expected),
      toBeEnabled: async () => {},
      toBeDisabled: async () => assert.equal(values.Frame, ''),
      toHaveCount: async (count) => assert.equal(value.count(), count),
      toHaveValue: async (expected) => assert.equal(value.value(), expected),
      toHaveText: async (expected) => assert.equal(value.value(), expected),
    })
    assertions.poll = (read) => ({ toBe: async (expected) => assert.equal(await read(), expected) })
    if (retainDraft) await assert.rejects(switchProjectSolarForms(runtime, assertions), /switch Solve to Correct strings and back/)
    else {
      await switchProjectSolarForms(runtime, assertions)
      assert.deepEqual(edits, ['Grant ref', 'Request', 'Frame', 'Phase', 'Memberships'])
      assert.deepEqual(runtime.evidence.projectSolar.formSwitch, {
        solveFieldsReset: true, membershipsReset: true, runSolveDisabled: true, submittedRuns: 0,
      })
    }
    assert.ok(clicks.includes('solar-correct-string'))
    assert.ok(!clicks.some((name) => name instanceof RegExp), 'switch tools directly without closing their editors first')
  }
})

function solarChainRuntime({ failTool, staleVersion = false, staleGraph = false, releaseFails = false,
  missingPanels = false, unconfirmedSizing = false, missingStrings = false, missingZone = false,
  equipmentReady = false } = {}) {
  const requests = [], runs = []
  let rev = 0, head = 1, releases = 0
  const versions = [{ v: 1, sha256: 'a'.repeat(64) }]
  const response = (body, status = 200) => ({ ok: () => status === 200, status: () => status, json: async () => structuredClone(body) })
  const request = {
    get: async (url) => {
      requests.push(['GET', url])
      if (url.endsWith('/versions')) return response({ drawing_id: 'private', head, latest: head, versions })
      if (url === '/api/tools') return response({ tools: [...new Set(solarChainSteps('routed').map((row) => row.tool)), 'solar-electrical-zones']
        .map((name) => ({ name, catalog_digest: `digest:${name}` })) })
      if (url === '/api/session?dwg=private') return response({ intake: { solar_design_graph: {
        rev: staleGraph ? rev - 1 : rev, settings: { panels_in_sequence: 3,
          global_string_sizing_confirmed: !unconfirmedSizing && rev >= 4,
          extra: rev >= 4 ? { string_sizing: { mode: 'manual-global', records: {} } } : {} },
        panels: !missingPanels && rev >= 3 ? [{ id: 'physical-panel' }] : [],
        strings: !missingStrings && rev >= 5 ? [{ id: 'local-string' }] : [],
        electrical_zones: !missingZone && runs.some((row) => row.tool === 'solar-electrical-zones') ? [{ id: 'local-zone' }] : [],
      } } })
      if (url.startsWith('/api/capabilities?drawing_id=private&drawing_version=')) return response({ families: [{ capabilities: [
        { name: 'solar-assign-equipment', availability: { input_ready: equipmentReady, refusal_reasons: equipmentReady ? [] : ['valid_strings_required'] } },
      ] }] })
      assert.fail(url)
    },
    post: async (url, options) => {
      requests.push(['POST', url])
      if (url.endsWith('/checkout')) return response({ acquired: true, checkout_capability: 'lease' })
      assert.equal(url, '/api/run?wait=1')
      assert.equal(options.headers['X-Checkout-Capability'], 'lease')
      assert.equal(options.data.dwg, 'private')
      assert.equal(options.data.catalog_digest, `digest:${options.data.tool}`)
      assert.equal(options.data.params.expected_rev, rev)
      runs.push(options.data)
      if (options.data.tool === failTool) return response({ ok: false, error: 'SERVER_REFUSAL' }, 409)
      rev++
      if (!staleVersion) {
        versions.push({ v: head + 1, parent: head, sha256: 'b'.repeat(64) })
        head++
      }
      return response({ ok: true, result: { after_rev: rev, new_version: { version: head } } })
    },
    delete: async (url, options) => {
      assert.equal(url, '/api/drawings/private/checkout')
      assert.equal(options.headers['X-Checkout-Capability'], 'lease')
      requests.push(['DELETE', url]); releases++
      return response({ ok: !releaseFails, error: releaseFails ? 'release failed' : undefined }, releaseFails ? 500 : 200)
    },
  }
  return { runtime: { page: { request }, drawingId: 'private', evidence: {} }, requests, runs, releases: () => releases }
}

test('WL15 seed builds only the requested prefix using one lease and persisted version/revision advances', async () => {
  for (const stage of ['panels', 'sized', 'strung']) {
    const { runtime, requests, runs, releases } = solarChainRuntime()
    await seedSolarChain(runtime, { stage })
    assert.deepEqual(runs.map((row) => row.tool), solarChainSteps(stage).map((row) => row.tool))
    assert.deepEqual(runs.map((row) => row.params.expected_rev), runs.map((_, index) => index))
    assert.deepEqual(runs[0].params.initialize.source_intake_sha256, 'a'.repeat(64))
    assert.deepEqual(runs[1].params.project_changes, { name: 'Walk rooftop', zip_code: '44224', latitude: null, longitude: null })
    if (stage !== 'panels') assert.deepEqual(runs[3].params, { expected_rev: 3, mode: 'manual-global', confirm: true })
    // solar_string_add.json requires operation; the legacy builtin dispatches on it.
    if (stage === 'strung') assert.deepEqual(runs[4].params, {
      expected_rev: 4, operation: 'add-string', ordered_panel_refs: ['physical-panel'],
    })
    assert.equal(requests.filter(([method, path]) => method === 'POST' && path.endsWith('/checkout')).length, 1)
    assert.equal(releases(), 1)
    assert.equal(runtime.drawingVersion, runs.length + 1)
    assert.equal(runtime.evidence.solarChain.version, runtime.drawingVersion)
    assert.equal(runtime.evidence.solarChain.completedStage, stage)
    for (const proof of runtime.evidence.solarChain.stages) {
      assert.ok(proof.elapsedMs >= 0)
      assert.ok(proof.operations.length)
    }
  }
})

test('WL15 a zone uses the local producer only for its requesting probe', async () => {
  const { runtime, runs } = solarChainRuntime()
  await seedSolarChain(runtime, { stage: 'panels', electricalZone: true })
  assert.deepEqual(runs.map((row) => row.tool), [...solarChainSteps('panels').map((row) => row.tool), 'solar-electrical-zones'])
  assert.deepEqual(runs.at(-1).params, { expected_rev: 3, operation: 'add', name: 'Walk zone', color_index: 1 })
  assert.equal(runtime.evidence.solarChain.stages.at(-1).stage, 'zone')
})

test('WL15 server refusals, stale persistence and stale graph reads fail setup loudly and release the lease', async () => {
  for (const [fault, pattern, stage] of [
    [{ failTool: 'solar-panels-from-drawing' }, /panels.*solar-panels-from-drawing.*SERVER_REFUSAL/, 'panels'],
    [{ failTool: 'solar-size-strings' }, /sized.*solar-size-strings.*SERVER_REFUSAL/, 'sized'],
    [{ failTool: 'solar-string-add' }, /strung.*solar-string-add.*SERVER_REFUSAL/, 'strung'],
    [{ staleVersion: true }, /panels.*head did not advance/, 'panels'],
    [{ staleGraph: true }, /panels.*session graph revision/, 'strung'],
    [{ missingPanels: true }, /panels.*physical DXF produced no panels/, 'panels'],
    [{ unconfirmedSizing: true }, /sized.*confirmed manual sizing was not persisted/, 'strung'],
    [{ missingStrings: true }, /strung.*local string was not persisted/, 'strung'],
  ]) {
    const { runtime, releases } = solarChainRuntime(fault)
    await assert.rejects(seedSolarChain(runtime, { stage }), pattern)
    assert.equal(releases(), 1)
    assert.ok(runtime.evidence.solarChain.error)
    assert.ok(runtime.evidence.solarChain.stages.at(-1).elapsedMs >= 0)
  }
  const { runtime, requests } = solarChainRuntime()
  await assert.rejects(seedSolarChain(runtime, { stage: 'grouped' }), /Unknown solar chain stage/)
  assert.deepEqual(requests, [])
  const zone = solarChainRuntime({ missingZone: true })
  await assert.rejects(seedSolarChain(zone.runtime, { stage: 'panels', electricalZone: true }), /zone.*local electrical zone was not persisted/)
  assert.equal(zone.releases(), 1)
})

test('WL15 equipped and routed stop at the real frame prerequisite without fabricated evidence or equipment', async () => {
  for (const stage of ['equipped', 'routed']) {
    const { runtime, runs, requests, releases } = solarChainRuntime()
    await assert.rejects(seedSolarChain(runtime, { stage }), /equipped.*needs_live_sizing.*valid_strings_required/)
    assert.equal(releases(), 1)
    assert.deepEqual(runs.map((row) => row.tool), solarChainSteps('strung').map((row) => row.tool))
    assert.equal(requests.some(([, url]) => url === '/api/capabilities?drawing_id=private&drawing_version=6'), true)
    assert.equal(runtime.evidence.solarChain.equipmentAvailability.input_ready, false)
  }
  const changed = solarChainRuntime({ equipmentReady: true })
  await assert.rejects(seedSolarChain(changed.runtime, { stage: 'routed' }), /equipment readiness did not confirm the missing frame prerequisite/)
  assert.ok(!changed.runtime.evidence.solarChain.error.includes('needs_live_sizing'))
  assert.equal(changed.releases(), 1)
})

test('WL15 release failure preserves the server refusal as the primary failure', async () => {
  const { runtime, releases } = solarChainRuntime({ failTool: 'solar-size-strings', releaseFails: true })
  await assert.rejects(seedSolarChain(runtime, { stage: 'sized' }), /sized.*SERVER_REFUSAL/)
  assert.match(runtime.evidence.solarChain.releaseError, /release failed/)
  assert.equal(releases(), 1)
  const success = solarChainRuntime({ releaseFails: true })
  await assert.rejects(seedSolarChain(success.runtime, { stage: 'panels' }), /release failed/)
})

test('WL14 Find-submit clicks its own button after preparing a fresh query', () => {
  const map = buildFeatureMap()
  const entry = map.entries.find((row) => row.id === 'control:find-drawing-submit')
  const probe = resolveProbe(entry, 'ready')
  assert.equal(probe.certify, 'both')
  assert.deepEqual(probe.locator, { role: 'button', name: 'Find', exact: true, trigger: 'click',
    tooltip: undefined, description: undefined })
  assert.deepEqual(probe.setup.steps, [{ kind: 'open-private-drawing', surface: 'cad' }, { kind: 'find-submit-state' }])
  assert.deepEqual(probe.assertion, { kind: 'renders', target: 'drawing-find-submit-no-match',
    assertionId: 'control:find-drawing-submit/ready/renders' })
  const field = resolveProbe(map.entries.find((row) => row.id === 'control:find-drawing'), 'ready')
  assert.equal(field.locator.trigger, 'fill-enter')
  assert.equal(field.locator.role, 'combobox')
})

test('G4 delete removes both fixture LINEs and one Undo restores the whole drawing', async () => {
  const geometry = { entities: [{ id: 0xA100, type: 'LINE' }, { id: 0xA101, type: 'LINE' }, { id: 9, type: 'CIRCLE' }] }
  for (const removed of [2, 1]) {
    let current = geometry
    let undoClicks = 0
    const page = {
      getByTestId: (id) => ({ id, innerText: async () => String(current.entities.length) }),
      evaluate: async () => current,
      getByRole: (role, options) => {
        assert.equal(role, 'toolbar')
        assert.equal(options.name, 'Quick access')
        return { getByRole: (role, options) => {
          assert.equal(role, 'button')
          assert.equal(options.name, ACTIONS.find((action) => action.id === 'engine:undo').label)
          return { click: async () => { undoClicks++; current = geometry } }
        } }
      },
    }
    const assertions = (value) => ({
      toBe: (expected) => assert.equal(value, expected),
      toEqual: (expected) => assert.deepEqual(value, expected),
      toHaveText: async (expected) => { assert.equal(value.id, 'dock-selection-count'); assert.equal(expected, '2 objects selected') },
    })
    assertions.poll = (read) => ({ toBe: async (expected) => assert.equal(await read(), expected) })
    const runtime = { page, evidence: {} }
    const before = await captureMultipleSelection({ assertion: { target: 'engine:delete' } }, runtime, assertions)
    assert.equal(before.selectionCount, 2)
    assert.equal(before.count, 3)
    current = { entities: geometry.entities.slice(removed) }
    if (removed === 1) {
      await assert.rejects(assertMultipleSelectionDelete(runtime, before, assertions), assert.AssertionError)
      assert.equal(undoClicks, 0)
    } else {
      await assertMultipleSelectionDelete(runtime, before, assertions)
      assert.equal(undoClicks, 1)
      assert.deepEqual(runtime.evidence.multipleSelection, { selectionCount: 2, before: 3, after: 1, restored: 3, undoSteps: 1 })
    }
  }
})
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

const fixtureSource = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')

test('rail recipes reveal Studio before their control and restore only panels they opened', async () => {
  const map = buildFeatureMap()
  for (const featureId of [...STUDIO_RAIL_FEATURES, 'control:session-details']) {
    for (const state of ['ready', 'failed-load']) {
      for (const initiallyOpen of [false, true]) {
        const fake = studioRailFake({ expanded: String(initiallyOpen) })
        const rail = STUDIO_RAIL_FEATURES.has(featureId)
        const probe = resolveProbe(map.entries.find(entry => entry.id === featureId), state)
        const recipe = probe.setup.steps.find(step => ['baseline-three-state', 'job-monitor-collapsed'].includes(step.kind))
        assert.ok(recipe, `${featureId}/${state} has a rail setup hook`)
        const studioRole = fake.page.getByRole
        const controlLocator = { visible: true, countValue: 0, isVisible: async () => false,
          getByRole: () => controlLocator }
        fake.page.getByRole = (role, options) => {
          if (role === 'navigation') return studioRole(role, options)
          if (rail) assert.equal(fake.expanded(), 'true', 'control lookup requires open Studio')
          fake.events.push('control lookup')
          return controlLocator
        }
        const studioLocator = fake.page.locator
        fake.page.locator = selector => selector === '#project-workspace-activity'
          ? studioLocator(selector) : controlLocator
        const runtime = { page: fake.page, cleanup: [], evidence: {} }
        const enter = async actualRuntime => {
          assert.equal(actualRuntime, runtime)
          await enterWorkspaceEnvironment(fake.workspace.page)
          fake.events.push('workspace ready')
        }
        const reveal = async page => {
          assert.equal(page, fake.page)
          assert.equal(fake.events.at(-1), 'workspace ready')
          fake.events.push('Studio reveal')
          return revealStudioRail(page)
        }
        await prepareStudioRail(probe, runtime, { kind: 'unrelated-recipe' }, { enter, reveal })
        assert.deepEqual(fake.events, [])
        await prepareStudioRail(probe, runtime, recipe, { enter, reveal })
        assert.equal(fake.events.includes('workspace ready'), rail)
        assert.equal(fake.events.includes('Studio reveal'), rail)
        assert.equal(runtime.cleanup.length, rail && !initiallyOpen ? 1 : 0)
        const control = recipe.control || { role: 'toolbar', name: 'Job monitor' }
        fake.page.getByRole(control.role, { name: control.name })
        if (rail && !initiallyOpen) {
          assert.ok(fake.events.indexOf('workspace ready') < fake.events.indexOf('Studio click'))
          assert.ok(fake.events.indexOf('Studio rail visible wait') < fake.events.indexOf('control lookup'))
        }
        for (const cleanup of runtime.cleanup.reverse()) await cleanup()
        for (const cleanup of runtime.cleanup) await cleanup()
        assert.equal(fake.expanded(), String(initiallyOpen))
        assert.equal(fake.events.filter(event => event === 'Studio click').length, rail && !initiallyOpen ? 2 : 0)
      }
    }
  }
})

const workspaceAssertionSource = fixtureSource.slice(fixtureSource.indexOf('export async function requireNoDrawing('),
  fixtureSource.indexOf('async function setSection(')).replace('export ', '')
const workspaceCanvasSource = fixtureSource.slice(fixtureSource.indexOf('const canvas ='),
  fixtureSource.indexOf('async function viewportBounds('))
const extractedRequireWorkspace = assertions => new Function('expect', 'enterWorkspaceEnvironment',
  `${workspaceCanvasSource}\n${workspaceAssertionSource}; return requireWorkspace`)(assertions,
  page => enterWorkspaceEnvironment(page, boundedWorkspacePoll().assertions))

function requireWorkspaceFake({ present = true, failedDrawing = false } = {}) {
  const environment = workspaceEnvironmentFake({ present })
  const checked = []
  const command = { name: 'command', visible: true }
  const drawing = { name: 'canvas', get visible() {
    return !present || environment.events.includes('assert:data-entered=true')
  } }
  const alert = { name: 'load error', visible: true }
  const undo = { name: 'undo', visible: true, disabled: true }
  const page = { ...environment.page, getByRole: (role, options) => {
    assert.deepEqual(environment.events, present ? ['Continue workspace', 'assert:data-entered=true'] : [])
    if (role === 'combobox') {
      assert.deepEqual(options, { name: 'Command bar', exact: true })
      return command
    }
    if (role === 'region') {
      assert.equal(failedDrawing, false)
      assert.deepEqual(options, { name: 'Drawing', exact: true })
      return { locator: selector => { assert.equal(selector, 'canvas:visible'); return drawing } }
    }
    if (role === 'alert') return { filter: ({ hasText }) => {
      assert.equal(hasText.test("Couldn't load drawing"), true)
      assert.equal(hasText.test('Couldn’t load drawing'), true)
      return alert
    } }
    assert.equal(role, 'toolbar')
    assert.deepEqual(options, { name: 'Quick access', exact: true })
    return { getByRole: (role, options) => {
      assert.equal(role, 'button')
      assert.deepEqual(options, { name: 'Undo version (unavailable: No saved versions yet)', exact: true })
      return undo
    } }
  } }
  const assertions = value => ({
    toBeVisible: async () => { checked.push(`${value.name}:visible`); assert.equal(value.visible, true) },
    toBeDisabled: async () => { checked.push(`${value.name}:disabled`); assert.equal(value.disabled, true) },
  })
  return { environment, drawing, alert, undo, checked,
    run: () => extractedRequireWorkspace(assertions)({ page, failedDrawing }) }
}

test('requireWorkspace enters the chooser once before asserting the command bar and canvas', async () => {
  const fake = requireWorkspaceFake()
  assert.equal(fake.drawing.visible, false)
  await fake.run()
  assert.deepEqual(fake.environment.events, ['Continue workspace', 'assert:data-entered=true'])
  assert.deepEqual(fake.checked, ['command:visible', 'canvas:visible'])
})

test('requireWorkspace without an environment asserts the workspace without clicks', async () => {
  const fake = requireWorkspaceFake({ present: false })
  await fake.run()
  assert.deepEqual(fake.environment.events, [])
  assert.deepEqual(fake.checked, ['command:visible', 'canvas:visible'])
})

test('requireWorkspace asserts the load error and no-drawing signal after entry', async () => {
  const fake = requireWorkspaceFake({ failedDrawing: true })
  await fake.run()
  assert.deepEqual(fake.checked, ['command:visible', 'load error:visible', 'undo:visible', 'undo:disabled'])
  fake.alert.visible = false
  await assert.rejects(fake.run(), assert.AssertionError)
  fake.alert.visible = true
  fake.undo.visible = false
  await assert.rejects(fake.run(), assert.AssertionError)
  fake.undo.visible = true
  fake.undo.disabled = false
  await assert.rejects(fake.run(), assert.AssertionError)
})

test('project Solar walk functions and staging steps contain no untimed empty-argument clicks', () => {
  for (const name of ['assertProjectSolarDrawing', 'openProjectSolar', 'selectProjectSolarVersion',
    'openProjectSolarTool', 'takeProjectSolarCheckout', 'publishProjectSolar', 'switchProjectSolarForms']) {
    const start = fixtureSource.indexOf(`export async function ${name}(`)
    assert.ok(start >= 0, `missing function ${name}`)
    const end = fixtureSource.indexOf('\nexport ', start + 1)
    const body = fixtureSource.slice(start, end < 0 ? undefined : end)
    assert.doesNotMatch(body, /\.click\(\s*\)/, name)
  }
  assert.doesNotMatch(readFileSync(new URL('../walk-staging/projectSolarSteps.mjs', import.meta.url), 'utf8'), /\.click\(\s*\)/)
})

test('WL16b canonical import opens material intake through the Upload drawing ribbon action', () => {
  const source = fixtureSource.slice(fixtureSource.indexOf('export async function openProjectSolar('),
    fixtureSource.indexOf('export async function openProjectSolarTool('))
  const steps = [
    "await walkClick(page.getByRole('tab', { name: 'Browser', exact: true }), 'Open Browser tab')",
    "await walkClick(page.getByRole('button', { name: 'Upload drawing', exact: true }), 'Upload drawing')",
    'const material = page.locator(\'[data-pane="material-intake"]\')',
    "await assertions(material.getByRole('button', { name: 'Upload DWG or DXF', exact: true })).toBeEnabled()",
  ]
  let previous = -1
  for (const step of steps) {
    const position = source.indexOf(step)
    assert.ok(position > previous, `canonical material import must perform in order: ${step}`)
    previous = position
  }
})

const activationSource = fixtureSource.slice(fixtureSource.indexOf('async function activate('),
  fixtureSource.indexOf('export async function assertEffect('))
const extractedActivate = (assertions) => new Function('expect', `${activationSource}; return activate`)(assertions)

test('WL14 owner Escape keeps command-bar focus through activation and menu dismissal', async () => {
  const probe = resolveProbe(buildFeatureMap().entries.find((row) => row.id === 'action:bar-escape'), 'owner-open')
  const events = []
  let focused = true, menuVisible = true
  const bar = {
    press: async (key) => {
      assert.equal(key, 'Escape')
      assert.equal(focused, true)
      assert.equal(menuVisible, true)
      events.push(key)
      menuVisible = false
    },
  }
  const menu = {}
  const runtime = { evidence: { escapeOwner: { menu: 'Tool commands', focus: 'Command bar' } }, page: {
    getByRole: (role, options) => {
      assert.equal(options.exact, true)
      if (role === 'combobox') { assert.equal(options.name, 'Command bar'); return bar }
      if (role === 'listbox') { assert.equal(options.name, 'Tool commands'); return menu }
      assert.fail(`owner Escape must not move focus to ${role}`)
    },
  } }
  const assertions = (value) => ({
    toBeFocused: async () => { assert.equal(value, bar); assert.equal(focused, true); events.push('bar-focused') },
    toBeHidden: async () => { assert.equal(value, menu); assert.equal(menuVisible, false); events.push('menu-hidden') },
    toHaveAttribute: async (name, expected) => {
      assert.equal(value, bar)
      assert.equal(name, 'aria-expanded')
      assert.equal(String(menuVisible), expected)
    },
  })
  const activate = extractedActivate(assertions)
  await activate(probe, runtime, bar)
  await assertEffect(probe, runtime, bar, {}, assertions)
  assert.deepEqual(events, ['bar-focused', 'Escape', 'menu-hidden', 'bar-focused'])
  assert.equal(runtime.evidence.escapeOwner.dismissed, true)

  focused = false
  await assert.rejects(activate(probe, runtime, bar), assert.AssertionError)
})

test('keyboard actions focus the visible Workspace controls Project button before dispatch', async () => {
  const activate = extractedActivate(() => assert.fail('ordinary keyboard actions must not assert command-bar focus'))
  for (const [key, target] of [['Escape', 'escape:drawer'], ['r', 'retry'], ['?', 'keyboard-shortcut-sheet']]) {
    const events = []
    const runtime = { page: {
      getByRole: (role, options) => {
        assert.equal(role, 'navigation')
        assert.deepEqual(options, { name: 'Workspace controls', exact: true })
        return { getByRole: (role, options) => {
          assert.equal(role, 'button')
          assert.deepEqual(options, { name: 'Project', exact: true })
          return { isVisible: async () => true, focus: async () => events.push('project-focused') }
        } }
      },
      keyboard: { press: async (pressed) => events.push(pressed) },
    } }
    await activate({ locator: { trigger: 'keyboard', key }, assertion: { target } }, runtime, {})
    assert.deepEqual(events, ['project-focused', key])
  }
})

test('other keyboard actions retain workspace-tab focus before dispatch', async () => {
  const activate = extractedActivate(() => assert.fail('ordinary keyboard actions must not assert command-bar focus'))
  for (const [key, target] of [['Escape', 'escape:drawer'], ['r', 'retry']]) {
    const events = []
    const runtime = { page: {
      getByRole: (role, options) => {
        if (role === 'navigation') {
          assert.deepEqual(options, { name: 'Workspace controls', exact: true })
          return { getByRole: (role, options) => {
            assert.equal(role, 'button')
            assert.deepEqual(options, { name: 'Project', exact: true })
            return { isVisible: async () => false, focus: () => assert.fail('absent Project button must not receive focus') }
          } }
        }
        assert.equal(role, 'tablist')
        assert.deepEqual(options, { name: 'Workspace profile', exact: true })
        return { getByRole: (role) => {
          assert.equal(role, 'tab')
          return { first: () => ({ focus: async () => events.push('tab-focused') }) }
        } }
      },
      keyboard: { press: async (pressed) => events.push(pressed) },
    } }
    await activate({ locator: { trigger: 'keyboard', key }, assertion: { target } }, runtime, {})
    assert.deepEqual(events, ['tab-focused', key])
  }
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

test('C3 snap oracle rejects an expanded trigger without a visible mode menu', async () => {
  const target = 'object-snap-menu-expanded'
  const button = { getAttribute: async () => 'true' }
  for (const menu of [{ countValue: 0, visible: false }, { countValue: 1, visible: false }]) {
    const page = { locator: (selector) => selector === CENSUS_DISCLOSURES[target].body ? button : menu }
    await assert.rejects(assertCensusEffect({ assertion: { target, value: true } }, { page }, button, {}, censusAssertions),
      assert.AssertionError)
  }
})

test('C2 and C3 disclosures click into each initial state, reject a wrong effect and close before restoring viewport', async () => {
  for (const target of Object.keys(CENSUS_DISCLOSURES)) {
    for (const expanded of [false, true]) {
      let open = false
      const events = []
      const body = { getAttribute: async () => open ? 'true' : null,
        evaluate: async (read) => read({ open }) }
      const button = { visible: true, getAttribute: async () => String(open),
        click: async (options) => { assert.equal(options.timeout, 15_000); open = !open; events.push('click') } }
      const page = { locator: (selector) => {
        assert.ok([CENSUS_DISCLOSURES[target].body, CENSUS_DISCLOSURES[target].menu, '.drawing-objects-content'].includes(selector))
        if (selector === CENSUS_DISCLOSURES[target].menu) return { countValue: open ? 1 : 0, visible: open }
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

test('project Escape observes UI creation and rejects refused or cross-org receipts while retaining cleanup', async () => {
  for (const authLive of [false, true]) for (const fault of [null, 'refused', 'wrong-org']) {
    const events = []
    const board = { visible: true, attributes: { 'data-project-state': 'project' },
      getByRole: (role, options) => {
        assert.equal(role, 'button')
        assert.deepEqual(options, { name: 'Create project from this drawing', exact: true })
        return { disabled: false, click: async () => events.push('create-from-drawing') }
      },
      getByText: (text) => {
        assert.ok(['Versions live with a workspace project', 'Runs appear here with a project open'].includes(text))
        return { countValue: 0 }
      },
    }
    const project = { project_id: 'ui-project', org_id: fault === 'wrong-org' ? 'other-org' : 'org', name: 'walk.dxf' }
    const runtime = { cleanup: [], evidence: {}, workerFacts: { authLive }, stack: { baseURL: 'http://walk' }, page: {
      url: () => 'http://walk/app',
      request: { post: async (path, options) => {
        // Project creation must come from the browser, never the fixture's API client.
        assert.equal(path, '/api/orgs')
        assert.equal(options.timeout, 15_000)
        assert.deepEqual(options.headers, { 'X-Tenant-Id': 'demo-tenant',
          ...(authLive ? authHeader('org-page-token') : {}) })
        events.push(path)
        return { ok: () => true, json: async () => ({ org: { org_id: 'org' } }) }
      } },
      waitForResponse: async (predicate, options) => {
        assert.equal(options.timeout, 15_000)
        assert.equal(predicate({ url: () => 'http://walk/api/projects', request: () => ({ method: () => 'POST' }) }), true)
        events.push('observe-create')
        return { ok: () => fault !== 'refused', request: () => ({ headers: () => ({ 'x-org-id': 'org' }) }),
          json: async () => ({ project }) }
      },
      evaluate: async (callback, value) => {
        if (value === undefined) return 'org-page-token'
        events.push(['storage', value]); return 'previous-org'
      },
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
      locator: (selector) => selector === '.workspace-summary' ? { text: project.name } : { countValue: 0 },
      getByTestId: () => ({ locator: () => ({ text: 'no selection' }) }),
    } }
    const assertions = (value) => ({ ...faultAssertions(value), toHaveText: async (expected) => assert.equal(value.text, expected) })
    const probe = resolveProbe(buildFeatureMap().entries.find((entry) => entry.id === 'action:bar-escape'), 'project-open')
    const open = () => setupStep(probe, runtime, probe.setup.steps.at(-1), assertions)
    if (fault) {
      await assert.rejects(open(), assert.AssertionError)
      assert.equal(runtime.evidence.escapeProject, undefined)
      assert.ok(!events.includes('Return to drawing'))
    } else {
      await open()
      assert.deepEqual(runtime.evidence.escapeProject, { orgId: 'org', projectId: 'ui-project', name: 'walk.dxf' })
      assert.equal(board.visible, false)
    }
    assert.deepEqual(events.slice(0, 6), ['/api/orgs', ['storage', 'org'], 'reload', 'Start', 'observe-create', 'create-from-drawing'])
    await runtime.cleanup[0]()
    assert.deepEqual(events.slice(-2), ['Close workspace', ['storage', 'previous-org']])
  }
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
        if (role === 'navigation') return { getByRole: () => ({ isVisible: async () => false }) }
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
  for (const surface of ['cad', 'solar', 'browser', 'ios']) for (const status of [400, 404]) {
    const undo = { visible: true, disabled: true }
    const environment = workspaceEnvironmentFake()
    const page = Object.assign(new EventEmitter(), {
      goto: async (url) => {
        assert.deepEqual(environment.events, [])
        assert.equal(url, `/app?surface=${surface}&drawing=missing.invalid`)
        const response = { url: () => 'http://walk/api/session?dwg=missing.invalid',
          status: () => status, json: async () => ({ error: 'missing drawing' }) }
        page.emit('response', response)
        page.emit('requestfailed', timedRequest('/api/session?dwg=missing.invalid', { failure: 'late failure' }))
      },
      locator: environment.page.locator,
      getByRole: (role, options) => {
        assert.deepEqual(environment.events, ['Continue workspace', 'assert:data-entered=true'])
        if (role === 'alert') return { filter: () => ({ visible: true }) }
        assert.equal(role, 'toolbar')
        assert.deepEqual(options, { name: 'Quick access', exact: true })
        return { getByRole: (role, options) => {
          assert.equal(role, 'button')
          assert.deepEqual(options, { name: 'Undo version (unavailable: No saved versions yet)', exact: true })
          return undo
        } }
      },
    })
    const assertions = (value) => ({ ...expect(value),
      toContain: (expected) => assert.ok(value.includes(expected)),
      toBeDisabled: async () => assert.equal(value.disabled, true),
    })
    const runtime = { page, evidence: {} }
    await setupStep({}, runtime, { kind: 'open-failed-drawing',
      url: `/app?surface=${surface}&drawing=missing.invalid` }, assertions)
    assert.equal(runtime.failedDrawing, true)
    assert.equal(runtime.evidence.failedDrawing.status, status)
    for (const event of ['request', 'response', 'requestfailed']) assert.equal(page.listenerCount(event), 0)
    undo.disabled = false
    await assert.rejects(requireNoDrawing(page, assertions), assert.AssertionError)
    undo.disabled = true
    undo.visible = false
    await assert.rejects(requireNoDrawing(page, assertions), assert.AssertionError)
  }
})

function timedRequest(path, { startTime = Date.now(), durationMs = 2500, status = 200, failure = null,
  method = 'GET' } = {}) {
  return { url: () => `http://walk${path}`, method: () => method,
    timing: () => ({ startTime, responseEnd: durationMs }),
    failure: () => failure ? { errorText: failure } : null,
    response: async () => ({ status: () => status }),
    headers: () => assert.fail('request evidence must not read headers'),
    postData: () => assert.fail('request evidence must not read bodies'),
  }
}

test('WL19 failed drawing setup rejects a matching abort during navigation with its reason and elapsed time', { timeout: 1000 }, async () => {
  const page = Object.assign(new EventEmitter(), { goto: async () => {
    await Promise.resolve()
    for (const path of ['/api/tools', '/api/session?dwg=other', '/elsewhere?dwg=missing.invalid']) {
      const unrelated = timedRequest(path, { failure: 'unrelated failure' })
      page.emit('request', unrelated)
      page.emit('requestfailed', unrelated)
    }
    page.emit('response', { url: () => 'http://walk/api/session?dwg=other' })
    const request = timedRequest('/api/session?dwg=missing.invalid', {
      startTime: Date.now() - 5100, durationMs: -1, failure: 'net::ERR_ABORTED',
    })
    page.emit('request', request)
    page.emit('requestfailed', request)
    page.emit('response', { url: () => 'http://walk/api/session?dwg=missing.invalid', status: () => 404 })
    return new Promise(() => {})
  } })
  const runtime = { page, evidence: {} }
  await assert.rejects(setupStep({}, runtime, { kind: 'open-failed-drawing', url: '/app?drawing=missing.invalid' }),
    (error) => {
      assert.match(error.message, /^open-failed-drawing: GET \/api\/session failed after \d+ ms: net::ERR_ABORTED$/)
      assert.ok(Number(error.message.match(/after (\d+) ms/)[1]) >= 5100)
      assert.ok(!(error instanceof UnsupportedLocalError))
      return true
    })
  assert.equal(runtime.failedDrawing, undefined)
  assert.equal(runtime.evidence.result, undefined)
  for (const event of ['request', 'response', 'requestfailed']) assert.equal(page.listenerCount(event), 0)
})

test('WL19b a matching response wins over a later failure and awaits navigation before drawing assertions', { timeout: 1000 }, async () => {
  let rejectNavigation, responseReceived
  const responseSeen = new Promise((resolve) => { responseReceived = resolve })
  const navigation = new Promise((_resolve, reject) => { rejectNavigation = reject })
  const page = Object.assign(new EventEmitter(), { goto: async () => {
    await Promise.resolve()
    page.emit('response', { url: () => 'http://walk/api/session?dwg=missing.invalid', status: () => 404 })
    page.emit('requestfailed', timedRequest('/api/session?dwg=missing.invalid', { failure: 'late failure' }))
    responseReceived()
    return navigation
  } })
  let assertionsStarted = false
  const navigationFailure = new Error('navigation failed after response')
  const setup = setupStep({}, { page, evidence: {} }, { kind: 'open-failed-drawing', url: '/app' }, () => {
    assertionsStarted = true
    assert.fail('drawing assertions must wait for navigation')
  })
  const rejected = assert.rejects(setup, navigationFailure)
  await responseSeen
  assert.equal(assertionsStarted, false)
  // A navigation rejection must still be observed after the response wins.
  rejectNavigation(navigationFailure)
  await rejected
  assert.equal(assertionsStarted, false)
  for (const event of ['request', 'response', 'requestfailed']) assert.equal(page.listenerCount(event), 0)
})

test('WL19 failed drawing waiter releases listeners after navigation failure', async () => {
  const failure = new Error('navigation failed')
  const page = Object.assign(new EventEmitter(), { goto: async () => { throw failure } })
  await assert.rejects(setupStep({}, { page, evidence: {} }, { kind: 'open-failed-drawing', url: '/app' }), failure)
  for (const event of ['request', 'response', 'requestfailed']) assert.equal(page.listenerCount(event), 0)
})

test('WL19 runner preserves setup failure and per-probe request evidence, then releases observers', async () => {
  const probe = resolveProbe(buildFeatureMap().entries.find((entry) => entry.id === 'control:more-panels'), 'failed-load')
  const page = Object.assign(new EventEmitter(), { goto: async () => {
    const slow = timedRequest('/api/tools?private=hidden')
    page.emit('request', slow)
    page.emit('requestfinished', slow)
    const failed = timedRequest('/api/session?dwg=missing.invalid', { durationMs: 5100, failure: 'net::ERR_ABORTED' })
    page.emit('request', failed)
    page.emit('requestfailed', failed)
  } })
  const records = []
  for (let index = 0; index < 2; index++) {
    const runtime = { page, evidence: { steps: [] }, runStep: async (_name, callback) => callback() }
    await assert.rejects(runProbe(probe, runtime), /open-failed-drawing: GET \/api\/session failed after 5100 ms: net::ERR_ABORTED/)
    assert.match(runtime.evidence.failure.message, /net::ERR_ABORTED/)
    assert.equal(runtime.evidence.result, undefined)
    assert.equal(runtime.evidence.cleanupCompleted, true)
    assert.equal(runtime.evidence.requestTimings.length, 2)
    assert.equal(runtime.evidence.requestTimings[0].path, '/api/tools')
    assert.equal(runtime.evidence.requestTimings[0].status, 200)
    assert.equal(runtime.evidence.requestTimings[1].path, '/api/session')
    assert.equal(runtime.evidence.requestTimings[1].durationMs, 5100)
    assert.equal(runtime.evidence.requestTimings[1].failure, 'net::ERR_ABORTED')
    assert.deepEqual(runtime.evidence.abortedRequests, [runtime.evidence.requestTimings[1]])
    assert.deepEqual(runtime.evidence.failedRequests, [])
    records.push(runtime.evidence.requestTimings)
    for (const event of ['request', 'response', 'requestfinished', 'requestfailed']) assert.equal(page.listenerCount(event), 0)
  }
  assert.notEqual(records[0], records[1])
})

test('WL19 request evidence records slow completions and all failures with redacted paths and request timing', async () => {
  const context = new EventEmitter()
  const page = { context: () => context }
  const evidence = {}, startedAt = Date.now()
  const stop = observeProbeRequests(page, evidence, startedAt)
  for (const path of ['/api/session', '/api/tools', '/api/capabilities', '/api/builds']) {
    const request = timedRequest(`${path}?secret=never-record-this`, { startTime: startedAt + 120, status: 503 })
    context.emit('request', request)
    context.emit('requestfinished', request)
  }
  for (const durationMs of [0, 1999, 2000]) {
    const fast = timedRequest('/api/tools', { durationMs })
    context.emit('request', fast)
    context.emit('requestfinished', fast)
  }
  const failed = timedRequest('/api/session?dwg=private', {
    startTime: startedAt + 300, durationMs: 25, failure: 'net::ERR_CONNECTION_RESET', method: 'POST',
  })
  context.emit('request', failed)
  context.emit('requestfailed', failed)
  context.emit('requestfinished', timedRequest('/not-started-in-this-probe'))
  await stop()
  assert.deepEqual(evidence.requestTimings, [
    ...['/api/session', '/api/tools', '/api/capabilities', '/api/builds'].map((path) => ({
      method: 'GET', path, startOffsetMs: 120, durationMs: 2500, status: 503,
    })),
    { method: 'POST', path: '/api/session', startOffsetMs: 300, durationMs: 25, failure: 'net::ERR_CONNECTION_RESET' },
  ])
  assert.deepEqual(evidence.failedRequests, [
    { method: 'POST', path: '/api/session', startOffsetMs: 300, durationMs: 25, failure: 'net::ERR_CONNECTION_RESET' },
  ])
  assert.deepEqual(evidence.abortedRequests, [])
  assert.ok(!JSON.stringify(evidence).includes('never-record-this'))
  for (const event of ['request', 'requestfinished', 'requestfailed']) assert.equal(context.listenerCount(event), 0)
})

test('WL19b request failures are classified with bounded, query-free timing fields', async () => {
  for (const failure of ['net::ERR_ABORTED', 'NS_BINDING_ABORTED', 'cancelled', 'canceled',
    'net::ERR_CONNECTION_RESET', null]) {
    const page = new EventEmitter(), evidence = { failedRequests: [{ stale: true }], abortedRequests: [{ stale: true }] }
    const startedAt = Date.now()
    const stop = observeProbeRequests(page, evidence, startedAt)
    for (let index = 0; index < 60; index++) {
      const request = timedRequest('/api/session?dwg=private', { startTime: startedAt + 5, durationMs: 10, failure })
      page.emit('request', request)
      page.emit('requestfailed', request)
    }
    await stop()
    const aborted = ['net::ERR_ABORTED', 'NS_BINDING_ABORTED', 'cancelled', 'canceled'].includes(failure)
    const records = evidence[aborted ? 'abortedRequests' : 'failedRequests']
    assert.equal(records.length, 50)
    assert.deepEqual(records, evidence.requestTimings)
    assert.deepEqual(records[0], { method: 'GET', path: '/api/session', startOffsetMs: 5,
      durationMs: 10, failure: failure || 'request failed' })
    assert.deepEqual(evidence[aborted ? 'failedRequests' : 'abortedRequests'], [])
    assert.ok(!JSON.stringify(evidence).includes('private'))
  }
})

test('WL19 request evidence is capped at 50 and metadata errors cannot change a verdict', async () => {
  const page = new EventEmitter(), evidence = {}
  const stop = observeProbeRequests(page, evidence)
  const broken = timedRequest('/api/tools')
  broken.timing = () => { throw new Error('timing unavailable') }
  page.emit('request', broken)
  assert.doesNotThrow(() => page.emit('requestfinished', broken))
  const noResponse = timedRequest('/api/builds', { durationMs: 2001 })
  noResponse.response = async () => { throw new Error('page closed') }
  page.emit('request', noResponse)
  page.emit('requestfinished', noResponse)
  for (let index = 0; index < 60; index++) {
    const request = timedRequest(`/api/tools?index=${index}`, { failure: 'cancelled', durationMs: 1 })
    page.emit('request', request)
    page.emit('requestfailed', request)
  }
  await stop()
  assert.equal(evidence.requestTimings.length, 50)
  assert.equal(evidence.requestTimings[0].durationMs, 2001)
  assert.equal(evidence.requestTimings[1].failure, 'cancelled')
  assert.equal(evidence.abortedRequests.length, 49)
  assert.deepEqual(evidence.failedRequests, [])
  const before = JSON.stringify(evidence)
  page.emit('requestfailed', timedRequest('/after-stop', { failure: 'cancelled' }))
  assert.equal(JSON.stringify(evidence), before)
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
    if (role === 'navigation') {
      assert.equal(options.name, 'Workspace controls')
      return { getByRole: () => ({ isVisible: async () => false }) }
    }
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
      getByRole: (role) => role === 'combobox' ? input : { getByRole: () => ({ isVisible: async () => false, first: () => ({ focus: async () => {} }) }) },
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

test('copy derives pasted kind and translation from the observed source and rejects corruption', async () => {
  for (const type of ['LINE', 'LWPOLYLINE']) for (const fault of [null, 'copy', 'untouched', 'type', 'layer', 'count', 'source-missing']) {
    const original = { id: '1', type, layer: 'Walk', vertices: [[111, 190, 0], [333, 210, 0]] }
    const before = { count: 1, sourceId: fault === 'source-missing' ? 'missing' : original.id,
      geometry: { documentId: 'private-v1.dxf', entities: [original] } }
    let geometry = structuredClone(before.geometry)
    let count = 1
    const page = {
      evaluate: async () => structuredClone(geometry),
      getByRole: () => ({ getByRole: () => ({ click: async () => {} }) }),
      getByLabel: (_, options) => ({ fill: async (value) => { assert.equal(options.exact, true); assert.equal(value, '400,100') } }),
      getByTestId: (id) => id === 'cockpit-prompt' ? { name: 'PASTE command' }
        : id === 'cad-edit-entity-count' ? { innerText: async () => String(count) }
          : { click: async () => {
            count = fault === 'count' ? 1 : 2
            geometry.entities.push({ id: '2', type: fault === 'type' ? 'CIRCLE' : type,
              layer: fault === 'layer' ? 'Panels' : 'Walk', vertices: [[400, 100, 0], [fault === 'copy' ? 623 : 622, 120, 0]] })
            if (fault === 'untouched') geometry.entities[0].vertices[0][0]++
          } },
    }
    const runtime = { page, evidence: {} }
    if (fault) await assert.rejects(assertCopiedGeometry({}, runtime, before, oracleAssertions))
    else {
      await assertCopiedGeometry({}, runtime, before, oracleAssertions)
      assert.deepEqual(runtime.evidence.clipboardPaste.base, [400, 100])
      assert.deepEqual(runtime.evidence.clipboardPaste.source, original)
      assert.deepEqual(runtime.evidence.clipboardPaste.expected,
        { type, layer: 'Walk', vertices: [[400, 100], [622, 120]] })
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

import { createActionRecorder, classifyStagingOutcome } from './stagingEvidence.mjs'

const stagingEvidenceEntry = (path, status = 200) => ({ method: 'GET', path, status,
  duration_ms: 120, error_code: null })

test('staging action requests survive 200 job and build polls', () => {
  const recorder = createActionRecorder()
  recorder.begin('POST /api/run')
  recorder.record(stagingEvidenceEntry('/api/projects/p1/context'))
  for (let index = 0; index < 200; index++) {
    recorder.record(stagingEvidenceEntry(index % 2 ? '/api/jobs?limit=20' : '/api/builds'))
  }
  recorder.end()
  assert.deepEqual(recorder.snapshot(), { actions: [{ action: 'POST /api/run',
    requests: [stagingEvidenceEntry('/api/projects/p1/context')], truncated: false }], truncated: false })
})

test('staging action recorder bounds requests and actions independently', () => {
  const recorder = createActionRecorder()
  recorder.begin('first')
  for (let index = 0; index < 45; index++) recorder.record(stagingEvidenceEntry(`/api/context/${index}`))
  const first = recorder.snapshot()
  assert.equal(first.actions[0].truncated, true)
  assert.equal(first.truncated, false)
  assert.deepEqual(first.actions[0].requests.map(entry => entry.path),
    Array.from({ length: 40 }, (_, index) => `/api/context/${index + 5}`))
  for (let index = 1; index < 21; index++) recorder.begin(`action ${index}`)
  recorder.end()
  assert.equal(recorder.snapshot().truncated, true)
  assert.deepEqual(recorder.snapshot().actions.map(window => window.action),
    Array.from({ length: 20 }, (_, index) => `action ${index + 1}`))
})

test('staging action recorder shares live entries but copies only allowed fields at snapshot time', () => {
  const recorder = createActionRecorder(), entry = stagingEvidenceEntry('/api/run', null)
  entry.headers = { private: 'secret' }
  recorder.begin('POST /api/run')
  recorder.record(entry)
  const before = recorder.snapshot()
  entry.status = 500
  entry.duration_ms = 42
  entry.error_code = 'RUN_FAILED'
  assert.equal(before.actions[0].requests[0].status, null)
  const after = recorder.snapshot()
  assert.deepEqual(after.actions[0].requests[0], { method: 'GET', path: '/api/run', status: 500,
    duration_ms: 42, error_code: 'RUN_FAILED' })
  after.actions[0].requests[0].status = 201
  assert.equal(recorder.snapshot().actions[0].requests[0].status, 500)
})

test('staging action recorder ignores entries outside windows and keeps recorders independent', () => {
  const recorder = createActionRecorder(), other = createActionRecorder()
  recorder.record(stagingEvidenceEntry('/api/before'))
  assert.deepEqual(recorder.snapshot(), { actions: [], truncated: false })
  recorder.begin('a'.repeat(100))
  recorder.record(stagingEvidenceEntry('/api/inside'))
  recorder.end()
  recorder.record(stagingEvidenceEntry('/api/after'))
  assert.equal(recorder.snapshot().actions[0].action, 'a'.repeat(80))
  assert.deepEqual(recorder.snapshot().actions[0].requests, [stagingEvidenceEntry('/api/inside')])
  assert.deepEqual(other.snapshot(), { actions: [], truncated: false })
})

test('staging action recorder honors custom bounds and exclusions without stateful regexp drift', () => {
  const recorder = createActionRecorder({ maxRequests: 1, maxActions: 1, exclude: [/^\/api\/poll/g] })
  recorder.begin('previous')
  recorder.begin('current')
  for (const path of ['/api/poll', '/api/poll', '/api/one', '/api/two']) recorder.record(stagingEvidenceEntry(path))
  assert.deepEqual(recorder.snapshot(), { actions: [{ action: 'current',
    requests: [stagingEvidenceEntry('/api/two')], truncated: true }], truncated: true })
})

function emitStagingEvidenceResponse(page, path, status = 200, error_code = null) {
  const request = { url: () => `${stagingBase}${path}`, method: () => 'GET',
    timing: () => ({ startTime: 1, responseEnd: 120 }) }
  const response = { url: request.url, request: () => request, status: () => status,
    json: async () => ({ error_code }), finished: async () => null }
  page.emit('request', request)
  page.emit('response', response)
  return response
}

test('staging network feeds the general ring and live action entries without changing the general ring', async () => {
  const results = []
  for (const useRecorder of [false, true]) {
    const page = new EventEmitter(), records = [], recorder = createActionRecorder()
    recorder.begin('POST /api/run')
    const finish = observeStagingNetwork(page, records, useRecorder ? recorder : undefined)
    emitStagingEvidenceResponse(page, '/api/projects/p1/context?token=secret', 500, 'CONTEXT_FAILED')
    for (let index = 0; index < 205; index++) emitStagingEvidenceResponse(page, `/api/jobs/${index}`)
    recorder.end()
    await finish()
    assert.equal(records.length, 40)
    assert.equal(records[0].path, '/api/jobs/165')
    if (useRecorder) assert.deepEqual(recorder.snapshot().actions[0].requests, [
      { method: 'GET', path: '/api/projects/p1/context?token', status: 500,
        duration_ms: 120, error_code: 'CONTEXT_FAILED' },
    ])
    else assert.deepEqual(recorder.snapshot().actions[0].requests, [])
    results.push(records)
  }
  assert.deepEqual(results[0], results[1])
})

test('staging classification treats the standing grant 502 as an observation on a pass', () => {
  assert.deepEqual(classifyStagingOutcome({ network: [stagingEvidenceEntry('/api/tenant/claude-grant', 502)] }),
    { status: 'passed', barrierFailure: null,
      observations: [{ method: 'GET', path: '/api/tenant/claude-grant', status: 502, error_code: null }] })
})

test('staging classification names the UI barrier independently of endpoint observations', () => {
  assert.deepEqual(classifyStagingOutcome({
    error: new Error('owner project did not open in the UI within 30 s\nstack...'),
    network: [stagingEvidenceEntry('/api/projects/p1'), stagingEvidenceEntry('/api/tenant/claude-grant', 502)],
  }), { status: 'failed', barrierFailure: 'owner project did not open in the UI within 30 s',
    observations: [{ method: 'GET', path: '/api/tenant/claude-grant', status: 502, error_code: null }] })
})

test('staging classification gives a named skip precedence and bounds barrier messages', () => {
  assert.deepEqual(classifyStagingOutcome({ skipped: 'proposal-review named skip', error: new Error('ignored') }),
    { status: 'skipped', barrierFailure: null, observations: [] })
  assert.equal(classifyStagingOutcome({ skipped: '', error: 'x'.repeat(220) }).barrierFailure, 'x'.repeat(200))
  assert.deepEqual(classifyStagingOutcome(), { status: 'passed', barrierFailure: null, observations: [] })
})

test('staging classification bounds observations in record order and excludes successful responses', () => {
  const network = [stagingEvidenceEntry('/api/ok', 200), stagingEvidenceEntry('/api/redirect', 302)]
  network[0].error_code = 'PUBLIC_CODE'
  network[1].error_code = 'PUBLIC_CODE'
  network.push(stagingEvidenceEntry('/api/failed', null), stagingEvidenceEntry('/api/legacy-failure', 'failed'))
  network.push({ ...stagingEvidenceEntry('/api/error-code'), status: undefined, error_code: 'REQUEST_FAILED' })
  for (let index = 0; index < 25; index++) network.push(stagingEvidenceEntry(`/api/errors/${index}`, 400))
  const result = classifyStagingOutcome({ network })
  assert.equal(result.status, 'passed')
  assert.equal(result.observations.length, 20)
  assert.deepEqual(result.observations, network.slice(2, 22).map(({ method, path, status, error_code }) =>
    ({ method, path, status, error_code })))
})

test('staging evidence persists classification and action windows while preserving the original verdict', async () => {
  for (const fail of [false, true]) {
    const page = new EventEmitter(), runtime = { page, evidence: { projectSolar: {} } }
    const initialSymbols = Object.getOwnPropertySymbols(page)
    page.url = () => stagingBase
    let armed = false, persisted
    const response = stagingResponse('/api/run', {}, 202, 'POST')
    page.waitForResponse = async predicate => {
      armed = true
      assert.equal(predicate(response), true)
      return response
    }
    const failure = new Error('owner project did not open in the UI within 30 s\nstack...')
    const run = () => stagingEvidenceStep(runtime, { title: 'action-evidence' }, async () => {
      await stagingAction(page, stagingBase, 'POST', '/api/run', () => {
        assert.equal(armed, true)
        emitStagingEvidenceResponse(page, '/api/projects/p1/context')
        if (fail) throw failure
      })
      emitStagingEvidenceResponse(page, '/api/outside-action')
      return 'passed'
    }, async evidence => { persisted = evidence.projectSolar })
    // Observe the endpoint error while the response wait is being armed.
    const wait = page.waitForResponse
    page.waitForResponse = predicate => {
      emitStagingEvidenceResponse(page, '/api/tenant/claude-grant', 502)
      return wait(predicate)
    }
    if (fail) await assert.rejects(run(), error => error === failure)
    else assert.equal(await run(), 'passed')
    assert.equal(persisted.classification.status, fail ? 'failed' : 'passed')
    assert.equal(persisted.classification.barrierFailure,
      fail ? 'owner project did not open in the UI within 30 s' : null)
    assert.deepEqual(persisted.classification.observations,
      [{ method: 'GET', path: '/api/tenant/claude-grant', status: 502, error_code: null }])
    assert.deepEqual(persisted.actions, { actions: [{ action: 'POST /api/run', requests: [
      stagingEvidenceEntry('/api/tenant/claude-grant', 502), stagingEvidenceEntry('/api/projects/p1/context'),
    ], truncated: false }], truncated: false })
    // A later caller on the same page must not reopen the finished step's recorder.
    await stagingAction(page, stagingBase, 'POST', '/api/run', () => {})
    assert.equal(page.listenerCount('request'), 0)
    assert.equal(page.listenerCount('response'), 0)
    assert.equal(page.listenerCount('console'), 0)
    assert.deepEqual(Object.getOwnPropertySymbols(page), initialSymbols)
    assert.equal(persisted.actions.actions.length, 1)
  }
})

for (const expectedStatus of ['skipped', 'passed']) {
  test(`staging evidence preserves a named skip with expected status ${expectedStatus}`, async () => {
    const page = new EventEmitter(), runtime = { page, evidence: { projectSolar: {} } }
    const message = 'Test is skipped: SIZED_PANEL_GROUPS_REQUIRED'
    const failure = new Error(message)
    let persisted
    await assert.rejects(stagingEvidenceStep(runtime, { title: 'named-skip', expectedStatus }, async () => {
      emitStagingEvidenceResponse(page, '/api/tenant/claude-grant', 502)
      throw failure
    }, async evidence => { persisted = structuredClone(evidence) }), error => error === failure)
    assert.equal(failure.message, message)
    assert.equal(persisted.projectSolar.classification.status, 'skipped')
    assert.equal(persisted.projectSolar.classification.barrierFailure, null)
    assert.equal(persisted.projectSolar.skipReason, 'SIZED_PANEL_GROUPS_REQUIRED')
    assert.equal(persisted.projectSolar.classification.observations[0].status, 502)
  })
}

test('staging evidence preserves failures and appends diagnostics when the test is expected to pass', async () => {
  const page = new EventEmitter(), runtime = { page, evidence: { projectSolar: {} } }
  const failure = new Error('workspace hidden')
  let persisted
  await assert.rejects(stagingEvidenceStep(runtime, { title: 'failed-barrier', expectedStatus: 'passed' }, async () => {
    emitStagingEvidenceResponse(page, '/api/projects/p1/context', 500, 'CONTEXT_FAILED')
    throw failure
  }, async evidence => { persisted = structuredClone(evidence) }), error => error === failure)
  assert.match(failure.message, /workspace hidden\nStaging network:.*CONTEXT_FAILED/)
  assert.match(failure.message, /\nStaging console:/)
  assert.equal(persisted.projectSolar.classification.status, 'failed')
  assert.equal(persisted.projectSolar.classification.barrierFailure, 'workspace hidden')
  assert.equal(Object.hasOwn(persisted.projectSolar, 'skipReason'), false)
})

test('staging evidence records a passing action without a skip reason', async () => {
  const page = new EventEmitter(), runtime = { page, evidence: { projectSolar: {} } }
  let persisted
  assert.equal(await stagingEvidenceStep(runtime, { title: 'passing-action', expectedStatus: 'passed' },
    async () => 'passed', async evidence => { persisted = structuredClone(evidence) }), 'passed')
  assert.equal(persisted.projectSolar.classification.status, 'passed')
  assert.equal(persisted.projectSolar.classification.barrierFailure, null)
  assert.equal(Object.hasOwn(persisted.projectSolar, 'skipReason'), false)
})

function emitG10Overflow(page, status = 409, errorCode = 'CONTEXT_FAILED') {
  emitStagingEvidenceResponse(page, '/api/projects/p1/context?drawing_id=private&token=secret', status, errorCode)
  for (let index = 0; index < 205; index++) {
    emitStagingEvidenceResponse(page, `/api/${index % 2 ? 'jobs' : 'builds'}/${index}?limit=private`)
  }
}

function g10ActionDiagnostics(error) {
  const line = error.message.split('\n').find(line => line.startsWith('Staging actions: '))
  assert.ok(line, 'original error must retain action diagnostics')
  return JSON.parse(line.slice('Staging actions: '.length))
}

test('G10 failure text retains submit context after polling overflow', async () => {
  for (const [status, errorCode] of [[409, 'CONTEXT_FAILED'], [200, null]]) {
    const page = new EventEmitter(), runtime = { page, evidence: { projectSolar: {} } }
    page.url = () => stagingBase
    const failure = new Error('submit UI barrier did not complete')
    let rejectWait, persisted
    page.waitForResponse = () => new Promise((resolve, reject) => { rejectWait = reject })
    await assert.rejects(stagingEvidenceStep(runtime, { title: 'G10-overflow' }, () =>
      stagingAction(page, stagingBase, 'POST', '/api/run', () => {
        emitG10Overflow(page, status, errorCode)
        rejectWait(failure)
      }), async evidence => { persisted = structuredClone(evidence.projectSolar) }), error => error === failure)
    assert.equal(persisted.network.length, 40)
    assert.equal(persisted.network.some(record => record.path.includes('context')), false)
    assert.equal(persisted.network.some(record => record.method === 'POST'), false)
    assert.deepEqual(persisted.classification,
      { status: 'failed', barrierFailure: 'submit UI barrier did not complete', observations: [] })
    assert.ok(failure.message.startsWith('submit UI barrier did not complete\nStaging network:'))
    const expected = { actions: [{ action: 'POST /api/run', requests: [{ method: 'GET',
      path: '/api/projects/p1/context?drawing_id&token', status, duration_ms: 120, error_code: errorCode }],
    truncated: false }], truncated: false }
    assert.deepEqual(persisted.actions, expected)
    assert.deepEqual(g10ActionDiagnostics(failure), expected)
    assert.equal(/private|secret/.test(failure.message), false)
  }
})

test('G10 failure text survives evidence write failure', async () => {
  const page = new EventEmitter(), runtime = { page, evidence: { projectSolar: {} } }
  page.url = () => stagingBase
  const failure = new Error('submit UI barrier did not complete')
  let rejectWait, writes = 0
  page.waitForResponse = () => new Promise((resolve, reject) => { rejectWait = reject })
  await assert.rejects(stagingEvidenceStep(runtime, { title: 'G10-disk-failure' }, () =>
    stagingAction(page, stagingBase, 'POST', '/api/run', () => {
      emitG10Overflow(page)
      rejectWait(failure)
    }), async () => { writes++; throw new Error('disk unavailable') }), error => error === failure)
  assert.equal(writes, 1)
  assert.equal(runtime.evidence.projectSolar.network.length, 40)
  const retained = g10ActionDiagnostics(failure).actions[0].requests
  assert.deepEqual(retained, [{ method: 'GET', path: '/api/projects/p1/context?drawing_id&token',
    status: 409, duration_ms: 120, error_code: 'CONTEXT_FAILED' }])
  assert.equal(failure.message.includes('disk unavailable'), false)
})

test('G10 retained diagnostics redact and bound action windows', () => {
  const jwt = 'eyJsynthetic.payload.signature', capability = 'lco1.synthetic-capability'
  const requests = Array.from({ length: 45 }, (_, index) => ({ method: `GET ${jwt}`,
    path: `/api/context/${index}/${capability}?drawing_id=private-value&token=${jwt}`,
    status: 409, duration_ms: index, error_code: capability,
    body: 'private-body', headers: { authorization: 'private-header' } }))
  const actions = { actions: Array.from({ length: 23 }, (_, index) => ({
    action: `${index} ${jwt} ${capability} ${'a'.repeat(100)}`, requests, truncated: false,
  })), truncated: false }
  const ring = [requests[0]], consoleMessages = [{ level: 'warning', text: jwt }]
  const original = structuredClone({ actions, ring, consoleMessages })
  const output = stagingNetworkFailure(ring, consoleMessages, actions)
  const lines = output.split('\n')
  assert.equal(lines.length, 3)
  assert.equal(lines[0].startsWith('Staging network: '), true)
  assert.equal(lines[1].startsWith('Staging console: '), true)
  const safe = JSON.parse(lines[2].slice('Staging actions: '.length))
  assert.equal(safe.truncated, true)
  assert.equal(safe.actions.length, 20)
  assert.equal(safe.actions[0].action.startsWith('3 '), true)
  assert.equal(safe.actions.at(-1).action.startsWith('22 '), true)
  for (const window of safe.actions) {
    assert.equal(window.action.length, 80)
    assert.equal(window.truncated, true)
    assert.equal(window.requests.length, 40)
    assert.match(window.requests[0].path, /^\/api\/context\/5\//)
    assert.match(window.requests.at(-1).path, /^\/api\/context\/44\//)
    for (const request of window.requests) {
      assert.deepEqual(Object.keys(request), ['method', 'path', 'status', 'duration_ms', 'error_code'])
      assert.ok(request.path.endsWith('?drawing_id&token'))
    }
  }
  for (const marker of [jwt, capability, 'private-value', 'private-body', 'private-header', 'headers', 'body']) {
    assert.equal(output.includes(marker), false)
  }
  assert.deepEqual({ actions, ring, consoleMessages }, original)
  assert.equal(stagingNetworkFailure(ring, consoleMessages), lines.slice(0, 2).join('\n'))
  assert.equal(stagingNetworkFailure([]), 'Staging network: []\nStaging console: []')
  for (const truncated of [false, true]) {
    const snapshot = { actions: [{ action: 'small', requests: [], truncated }], truncated }
    assert.deepEqual(g10ActionDiagnostics({ message: stagingNetworkFailure([], [], snapshot) }), snapshot)
  }
  const discarded = g10ActionDiagnostics({ message: stagingNetworkFailure([], [], {
    actions: [{ action: 'filtered', requests: [{ ...requests[0], path: '/assets/ignored' }], truncated: false }],
    truncated: false,
  }) })
  assert.deepEqual(discarded, { actions: [{ action: 'filtered', requests: [], truncated: true }], truncated: false })
})

test('G10 cleanup and late metadata preserve action attribution', async () => {
  const page = new EventEmitter(), runtime = { page, evidence: { projectSolar: {} } }
  page.url = () => stagingBase
  const events = ['request', 'response', 'requestfailed', 'console']
  const baselineListener = () => {}
  for (const event of events) page.on(event, baselineListener)
  const counts = events.map(event => page.listenerCount(event))
  const symbols = Object.getOwnPropertySymbols(page)
  page.waitForResponse = async () => stagingResponse('/api/run', {}, 202, 'POST')
  const request = { method: () => 'GET', url: () => `${stagingBase}/api/projects/p1/context?token=private`,
    timing: () => ({ startTime: 1, responseEnd: 321 }) }
  let completeBody, completeResponse, persisted
  const body = new Promise(resolve => { completeBody = resolve })
  const finished = new Promise(resolve => { completeResponse = resolve })
  const failure = new Error('late metadata barrier')
  await assert.rejects(stagingEvidenceStep(runtime, { title: 'G10-late' }, async () => {
    emitStagingEvidenceResponse(page, '/api/before-action')
    await stagingAction(page, stagingBase, 'POST', '/api/run', () => page.emit('request', request))
    await stagingAction(page, stagingBase, 'GET', '/api/projects', () => {
      page.emit('response', { request: () => request, status: () => 409,
        json: () => body, finished: () => finished })
      completeBody({ error_code: 'CONTEXT_FAILED' })
      emitStagingEvidenceResponse(page, '/api/inside-b')
    })
    completeResponse()
    throw failure
  }, async evidence => { persisted = structuredClone(evidence.projectSolar) }), error => error === failure)
  assert.deepEqual(persisted.actions.actions[0].requests, [{ method: 'GET',
    path: '/api/projects/p1/context?token', status: 409, duration_ms: 321, error_code: 'CONTEXT_FAILED' }])
  assert.deepEqual(persisted.actions.actions[1].requests, [stagingEvidenceEntry('/api/inside-b')])
  assert.deepEqual(g10ActionDiagnostics(failure), persisted.actions)
  assert.deepEqual(events.map(event => page.listenerCount(event)), counts)
  assert.deepEqual(Object.getOwnPropertySymbols(page), symbols)
  assert.equal(await stagingEvidenceStep(runtime, { title: 'G10-reused-page' }, async () => {
    emitStagingEvidenceResponse(page, '/api/outside-next-action')
    await stagingAction(page, stagingBase, 'POST', '/api/run', () => {})
    return 'next step'
  }, async () => {}), 'next step')
  assert.deepEqual(runtime.evidence.projectSolar.actions,
    { actions: [{ action: 'POST /api/run', requests: [], truncated: false }], truncated: false })
  assert.deepEqual(events.map(event => page.listenerCount(event)), counts)
  assert.deepEqual(Object.getOwnPropertySymbols(page), symbols)
})

test('G10 pass and named skip preserve verdicts with retained actions', async () => {
  for (const skip of [false, true]) {
    const page = new EventEmitter(), runtime = { page, evidence: { projectSolar: {} } }
    page.url = () => stagingBase
    page.waitForResponse = async () => stagingResponse('/api/run', {}, 202, 'POST')
    const failure = new Error('Test is skipped: SIZED_PANEL_GROUPS_REQUIRED')
    const value = { passed: true }
    let persisted
    const run = () => stagingEvidenceStep(runtime, { title: 'G10-verdict', expectedStatus: 'passed' }, async () => {
      await stagingAction(page, stagingBase, 'POST', '/api/run', () => {
        emitG10Overflow(page)
        if (skip) throw failure
      })
      return value
    }, async evidence => { persisted = structuredClone(evidence.projectSolar) })
    if (skip) {
      await assert.rejects(run(), error => error === failure)
      assert.equal(failure.message, 'Test is skipped: SIZED_PANEL_GROUPS_REQUIRED')
      assert.equal(persisted.skipReason, 'SIZED_PANEL_GROUPS_REQUIRED')
    } else assert.equal(await run(), value)
    assert.deepEqual(persisted.classification,
      { status: skip ? 'skipped' : 'passed', barrierFailure: null, observations: [] })
    assert.equal(persisted.network.length, 40)
    assert.deepEqual(persisted.actions.actions[0].requests, [{ method: 'GET',
      path: '/api/projects/p1/context?drawing_id&token', status: 409, duration_ms: 120, error_code: 'CONTEXT_FAILED' }])
  }
})
