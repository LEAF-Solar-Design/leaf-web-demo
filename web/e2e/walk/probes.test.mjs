import assert from 'node:assert/strict'
import test from 'node:test'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { ACTIONS, REASONS, accessibleName, reasonCode } from '../../src/lib/actionRegistry.js'
import { effectAssertion, resolveProbe, normalizedControlKey, requireControlCensusBatch, CONTROL_CENSUS_BATCH } from './probes.mjs'
import { readFileSync } from 'node:fs'
import { stackInstanceRef, UnsupportedLocalError } from './fixtures.mjs'

const map = buildFeatureMap()
// As in uxProbe.test.mjs, exercise the actual runner with fake Playwright
// steps; also run the evidence fixture's teardown to check its attachment.
const fixtureSource = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const functionBody = (start, end) => fixtureSource.slice(fixtureSource.indexOf(start) + start.length,
  fixtureSource.indexOf(end)).trim().slice(0, -1)
const unsupportedStep = new AsyncFunction('probe', 'runtime', 'reason', 'UnsupportedLocalError',
  functionBody('async function unsupported(probe, runtime, reason) {', '\nasync function engineReady'))
const probeRunner = new AsyncFunction('probe', 'runtime', 'test', 'setupStep', 'UnsupportedLocalError',
  functionBody('export async function runProbe(probe, runtime) {', '\n// This reporter'))
const evidenceFixture = new AsyncFunction('use', 'testInfo', 'workerFacts', fixtureSource.slice(
  fixtureSource.indexOf('walkEvidence: [async ({ workerFacts }, use, testInfo) => {')
    + 'walkEvidence: [async ({ workerFacts }, use, testInfo) => {'.length,
  fixtureSource.indexOf('}, { auto: true }]')))

async function unsupportedRuntime(error, log = [], attachments = []) {
  const testInfo = { title: 'unsupported unit probe', project: { name: 'desktop' }, annotations: [],
    attach: async (name, attachment) => { attachments.push({ name, ...attachment }) } }
  const probe = { featureId: 'action:unit', state: 'ready', certify: 'local',
    setup: { steps: [{ kind: 'unavailable' }, { kind: 'must-not-run' }] },
    assertion: { assertionId: 'unit/effect' } }
  let runtime, result
  await evidenceFixture(async (evidence) => {
    runtime = { evidence, testInfo, page: {} }
    result = await probeRunner(probe, runtime, { step: async (name, fn) => { log.push(name); await fn() } },
      async (_probe, current) => {
        current.cleanup.push(async () => { log.push('cleanup one') })
        current.cleanup.push(async () => { log.push('cleanup two') })
        if (error) throw error
        await unsupportedStep(probe, current, 'No local fixture', UnsupportedLocalError)
      }, UnsupportedLocalError)
  }, testInfo)
  return { result, runtime, attachments, log }
}

test('unsupported setup unwinds, cleans up and attaches unavailable evidence without activation', async () => {
  const { result, runtime, attachments, log } = await unsupportedRuntime()
  assert.deepEqual(result, { unsupported: true, reason: 'No local fixture' })
  assert.deepEqual(log, ['Setup: unavailable', 'cleanup two', 'cleanup one'])
  assert.deepEqual(runtime.testInfo.annotations, [{ type: 'unsupported_local', description: 'No local fixture' }])
  assert.equal(Object.hasOwn(runtime.evidence, 'failure'), false)
  for (const name of ['walk-result', 'walk-evidence']) {
    const attachment = attachments.find((item) => item.name === name)
    assert.equal(attachment.contentType, 'application/json')
    const evidence = JSON.parse(attachment.body.toString('utf8'))
    assert.equal(name === 'walk-result' ? evidence.result : evidence.result.result, 'unsupported_local')
  }
})

test('ordinary errors, including lookalike unsupported errors, propagate unchanged', async () => {
  for (const error of [new Error('setup broke'), Object.assign(new Error('UNSUPPORTED_LOCAL: fake'),
    { name: 'UnsupportedLocalError' })]) {
    const log = [], attachments = []
    await assert.rejects(unsupportedRuntime(error, log, attachments), (actual) => actual === error)
    assert.deepEqual(log, ['Setup: unavailable', 'cleanup two', 'cleanup one'])
    const evidence = JSON.parse(attachments.find((item) => item.name === 'walk-evidence').body.toString('utf8'))
    assert.equal(evidence.failure.message, error.message)
    assert.equal(Object.hasOwn(evidence, 'result'), false)
  }
})

test('UnsupportedLocalError preserves the unsupported message and reason', () => {
  const error = new UnsupportedLocalError({ featureId: 'action:bar-escape', state: 'selection-present' }, 'No local fixture')
  assert.ok(error instanceof Error)
  assert.equal(error.name, 'UnsupportedLocalError')
  assert.equal(error.message, 'UNSUPPORTED_LOCAL: action:bar-escape [selection-present]: No local fixture')
  assert.equal(error.reason, 'No local fixture')
})

test('stack instance refs are private, deterministic sha256 identities of the boot', () => {
  const directory = 'leaf-walk-stack-private-directory'
  const ref = stackInstanceRef(directory, 12345, 1800000000000)
  assert.match(ref, /^[a-f0-9]{64}$/)
  assert.equal(stackInstanceRef(directory, 12345, 1800000000000), ref)
  assert.notEqual(stackInstanceRef(directory, 12346, 1800000000000), ref)
  assert.notEqual(stackInstanceRef(directory, 12345, 1800000000001), ref)
  assert.notEqual(stackInstanceRef(directory + '-other', 12345, 1800000000000), ref)
  assert.ok(!ref.includes(directory))
})

test('batch three resolves seventeen document controls, semantic targets and public setup in every state', () => {
  const expected = [["scope-add","Add: build a new capability","opens","scope-build-picker",["ready","failed-load"]],["demo-return","Back to the demo","renders","guided-demo",["failed-load"]],["claude-accounts","Claude accounts not linked","opens","claude-accounts-panel",["ready","failed-load"]],["drawing-close-start","Close the drawing view and return to Start","opens","project-board",["ready"]],["notification-collapse","Collapse the notification inbox","renders","notification-inbox-collapsed",["ready","failed-load"]],["session-details","Details","opens","session-provenance",["ready","failed-load"]],["version-history","History","opens","version-history",["ready"]],["linked-services","Linked services {n} linked","opens","linked-services-panel",["ready","failed-load"]],["project-board","Open the project board","opens","project-board",["ready","failed-load"]],["prompt-run","Run","submits","unknown-tool-resolver",["ready","failed-load"]],["prompt-scope","scope ▾","opens","scope-picker",["ready","failed-load"]],["sign-out","Sign out","renders","signed-out-session",["ready","failed-load"]],["start-board","Start","opens","project-board",["ready","failed-load"]],["take-edit-lock","Take edit lock","renders","edit-lock-held",["ready"]],["cost-panel","What Leaf costs to operate","opens","cost-panel",["ready","failed-load"]],["command-bar","Command bar","opens","tool-commands",["ready","failed-load"]],["find-drawing","Find in drawing","renders","drawing-find-no-match",["ready"]]]
  const overrides = JSON.parse(readFileSync(new URL('../../walk/features.overrides.json', import.meta.url), 'utf8')).overrides
  const sessionDetails = JSON.parse(readFileSync(new URL('../../walk/features.overrides.json', import.meta.url), 'utf8')).controls.find((control) => control.id === 'control:session-details')
  assert.deepEqual(sessionDetails.states, ['ready', 'failed-load'])
  assert.equal(sessionDetails.certify_reason, undefined)
  for (const [id, name, kind, target, states] of expected) {
    const entry = map.entries.find((row) => row.id === 'control:' + id)
    assert.deepEqual(entry.states, [...states].sort())
    for (const state of states) {
      const probe = resolveProbe(entry, state)
      assert.equal(probe.locator.scope, undefined)
      assert.equal(probe.locator.role, ['command-bar', 'find-drawing'].includes(id) ? 'combobox' : 'button')
      assert.equal(probe.locator.exact, true)
      if (id !== 'linked-services') assert.equal(probe.locator.name, name)
      assert.equal(probe.assertion.kind, kind)
      assert.equal(probe.assertion.target, target)
      assert.equal(probe.certification, null)
      const workspace = probe.setup.steps[id === 'sign-out' ? 1 : 0]
      assert.equal(workspace.kind, state === 'failed-load' ? 'open-failed-drawing' : 'open-private-drawing')
      assert.deepEqual(probe.setup.steps.at(-1), { kind: 'baseline-three-state', sourceId: id, target,
        control: probe.locator, expanded: entry.state_contexts[state].expanded })
      assert.ok(probe.setup.steps.every((step) => !['engine-ready', 'require-local-state'].includes(step.kind)))
      if (id === 'sign-out') assert.equal(probe.setup.steps[0].kind, 'fresh-sign-out-page')
      if (id === 'command-bar') {
        assert.equal(probe.locator.trigger, 'type')
        assert.equal(probe.locator.inputValue, '/')
      } else if (id === 'find-drawing') {
        assert.equal(probe.locator.trigger, 'fill-enter')
        assert.equal(probe.locator.inputValue, 'w1k-absent-object-7f942')
      } else assert.equal(probe.locator.trigger, 'click')
    }
  }
})

test('linked-service numeric names match only the exact anchored count identity', () => {
  const probe = resolveProbe(map.entries.find((row) => row.id === 'control:linked-services'), 'ready')
  assert.equal(probe.locator.normalizedName, 'Linked services {n} linked')
  for (const name of ['Linked services 0 linked', 'Linked services 12 linked', 'Linked services 1234 linked']) {
    assert.match(name, probe.locator.name)
    assert.equal(normalizedControlKey({ scope: 'document', role: 'button', name }),
      normalizedControlKey({ scope: 'document', role: 'button', name: probe.locator.normalizedName }))
  }
  for (const name of ['Linked services checking', 'Linked services -1 linked', 'Linked services 1,234 linked',
    'Other Linked services 2 linked', 'Linked services 2 linked extra']) assert.doesNotMatch(name, probe.locator.name)
})

test('batch three semantic oracles precede generic handlers and use real interactions', () => {
  const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
  const setup = source.slice(source.indexOf('async function baselineThreeState'), source.indexOf('async function setupStep'))
  const oracle = source.slice(source.indexOf('async function assertEffect'), source.indexOf('export async function runProbe'))
  for (const target of ['scope-build-picker', 'guided-demo', 'project-board', 'notification-inbox-collapsed',
    'unknown-tool-resolver', 'signed-out-session', 'edit-lock-held', 'tool-commands', 'drawing-find-no-match']) {
    assert.ok(setup.includes(target), target + ' initial setup')
    assert.ok(oracle.indexOf(target) < oracle.indexOf("effect.kind === 'navigates'"), target + ' semantic oracle')
  }
  for (const text of ['Session · provenance', 'Claude accounts', 'Linked services', 'Version history',
    'Close cost panel', 'rooftop_demo', 'Return to drawing', 'No matching object in this drawing.',
    'You hold the edit lock', 'slash-menu-listbox', 'aria-selected']) assert.ok(source.includes(text), text)
  assert.match(setup, /index\.resolve\(recipe\.control\.inputValue\)\.status/)
  assert.match(setup, /expect\(names\)\.not\.toContain\('w1k-no-such-tool'\)/)
  assert.match(setup, /toHaveValue\('\/w1k-no-such-tool'\)/)
  assert.match(oracle, /expect\(runtime\.localDecisionRequests\)\.toEqual\(\[\]\)/)
  assert.match(oracle, /session signed out/)
  assert.match(oracle, /name: 'Refresh'/)
  const activation = source.slice(source.indexOf('async function activate'), source.indexOf('async function assertEffect'))
  assert.match(activation, /locator\.pressSequentially\(recipe\.inputValue\)/)
  assert.match(activation, /locator\.fill\(recipe\.inputValue\)/)
  assert.match(activation, /locator\.press\('Enter'\)/)
  assert.match(activation, /page\.waitForEvent\('domcontentloaded'\), locator\.click\(\)/)
  assert.doesNotMatch(setup, /dispatchEvent|__react|route\.fulfill|unsupported\(/)
})

test('batch three corrections use the default shell demo, inbox disclosure and session dialog', () => {
  const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
  const setup = source.slice(source.indexOf('async function baselineThreeState'), source.indexOf('async function setupStep'))
  const oracle = source.slice(source.indexOf('async function assertEffect'), source.indexOf('export async function runProbe'))
  const demo = oracle.slice(oracle.indexOf("if (target === 'guided-demo')"), oracle.indexOf("if (target === 'project-board')"))
  assert.match(demo, /getByRole\('complementary', \{ name: 'Properties', exact: true \}\)/)
  assert.match(demo, /getByRole\('definition'\)/)
  assert.ok(demo.includes('^rooftop_demo\\.dwg$'))
  assert.ok(demo.includes('^sample data$'))
  assert.match(demo, /getByRole\('alert'\).*toHaveCount\(0\)/)
  assert.doesNotMatch(demo, /Guided demo: sample rooftop|name: 'Guided demo'/)
  const session = setup.slice(setup.indexOf("if (target === 'session-provenance')"),
    setup.indexOf("if (target === 'notification-inbox-collapsed')"))
  assert.match(session, /expect\(locator\)\.toHaveCount\(1\)/)
  assert.match(session, /Session · provenance.*toBeHidden\(\)/)
  assert.doesNotMatch(session, /setDrawer|Workspace panels/)
  const panels = oracle.slice(oracle.indexOf('if (probe.kind === \'control\' && baselineThreePanels[target])'),
    oracle.indexOf("if (target === 'scope-build-picker'"))
  assert.match(panels, /expect\(panel\)\.toBeVisible\(\)/)
  assert.doesNotMatch(panels, /diagnostics-block/)
  const inboxSetup = setup.slice(setup.indexOf("if (target === 'notification-inbox-collapsed')"),
    setup.indexOf("if (target === 'edit-lock-held')"))
  assert.match(inboxSetup, /expect\(locator\)\.toHaveAttribute\('aria-expanded', 'true'\)/)
  assert.doesNotMatch(inboxSetup, /getByRole\('heading'/)
  const inbox = oracle.slice(oracle.indexOf("if (target === 'notification-inbox-collapsed')"),
    oracle.indexOf("if (target === 'unknown-tool-resolver')"))
  assert.match(inbox, /Expand the notification inbox.*toHaveAttribute\('aria-expanded', 'false'\)/)
  assert.match(inbox, /Collapse the notification inbox.*toHaveCount\(0\)/)
  assert.match(inbox, /Expand the notification inbox.*toBeVisible\(\)/)
  assert.doesNotMatch(inbox, /getByRole\('heading'/)
  assert.doesNotMatch(inbox, /\.locator\(/)
  assert.doesNotMatch(setup, /\.job-inbox|\.rail-ledger|\.rail-note/)
})

test('sign-out seed is page-specific and cannot reinsert identity after logout reload', () => {
  const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
  const seed = source.slice(source.indexOf('export function seedSignOutIdentity'), source.indexOf('async function baselineThreeState'))
  const storage = () => {
    const values = new Map()
    return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
      removeItem: (key) => values.delete(key) }
  }
  const local = storage(), session = storage()
  const applySeed = new Function('localStorage', 'sessionStorage', seed.replace('export ', '') + '; return seedSignOutIdentity')
    (local, session)
  const config = { identity: { token: 'one-time-fixture' }, coachKey: 'coach' }
  applySeed(config)
  assert.equal(local.getItem('leaf.jwt'), config.identity.token)
  local.removeItem('leaf.jwt')
  applySeed(config)
  assert.equal(local.getItem('leaf.jwt'), null)
  assert.equal(session.getItem('leaf.walk.w1k.identity-seeded'), '1')
  const fresh = source.slice(source.indexOf("case 'fresh-sign-out-page':"), source.indexOf("case 'baseline-three-state':"))
  assert.match(fresh, /ordinaryPage\.context\(\)\.newPage\(\)/)
  assert.match(fresh, /fresh\.addInitScript\(seedSignOutIdentity, \{ identity: LOCAL_IDENTITY/)
  assert.match(fresh, /fresh\.close\(\)/)
  assert.doesNotMatch(fresh, /context\(\)\.addInitScript/)
})

test('batch two uses exact complementary, nested group and document scope recipes', () => {
  for (const id of ['properties-close', 'properties-drawing', 'properties-layers', 'properties-plan', 'properties-selection']) {
    const entry = map.entries.find((row) => row.id === 'control:' + id)
    for (const state of entry.states) {
      const probe = resolveProbe(entry, state)
      assert.equal(probe.locator.role, 'button')
      assert.equal(probe.locator.name, entry.title)
      assert.equal(probe.locator.exact, true)
      assert.deepEqual(probe.locator.scope, { role: 'complementary', name: 'Properties', exact: true })
      assert.equal(probe.setup.steps[0].kind, entry.state_contexts[state].failedLoad ? 'open-failed-drawing' : 'open-private-drawing')
      assert.ok(probe.setup.steps.some((step) => step.kind === 'properties-state' && step.open))
      assert.ok(probe.setup.steps.every((step) => !['engine-ready', 'require-local-state'].includes(step.kind)))
      if (id === 'properties-close') assert.deepEqual(probe.assertion.value, false)
      else {
        const setup = probe.setup.steps.at(-1)
        assert.equal(setup.kind, 'properties-section-state')
        assert.equal(probe.assertion.value, !setup.expanded)
        if (id === 'properties-selection' && !entry.state_contexts[state].failedLoad) {
          assert.ok(probe.setup.steps.some((step) => step.kind === 'select-entity' && step.viewerOnly))
        }
      }
    }
  }
  for (const layer of ['panels', 'walk']) for (const prefix of ['properties-', 'layer-']) {
    const entry = map.entries.find((row) => row.id === 'control:' + prefix + layer)
    for (const state of ['shown', 'hidden']) {
      const probe = resolveProbe(entry, state)
      assert.deepEqual(probe.locator.scope, prefix === 'properties-'
        ? { role: 'complementary', name: 'Properties', exact: true }
        : { role: 'group', name: 'Layers', exact: true, scope: { role: 'toolbar', name: 'Drafting tools', exact: true } })
      assert.deepEqual(probe.setup.steps.at(-1), { kind: 'layer-visible-state',
        name: layer === 'panels' ? 'Panels' : 'Walk', visible: state === 'shown' })
      assert.equal(probe.assertion.value, state === 'hidden')
    }
  }
  for (const id of ['drawing-overview', 'drawing-overview-collapse']) {
    const entry = map.entries.find((row) => row.id === 'control:' + id)
    const probe = resolveProbe(entry, entry.states[0])
    assert.equal(probe.locator.scope, undefined)
    assert.equal(probe.locator.name, entry.title)
    assert.equal(probe.setup.steps.at(-1).kind, id === 'drawing-overview' ? 'overview-pan-state' : 'overview-expanded-state')
  }
})

test('count names are anchored, independent of live counts and retain normalized identities', () => {
  for (const [id, good, bad] of [
    ['properties-panels', ['Panels 1', 'Panels 12', 'Panels 1,234'], ['Panels', 'Panels 1 extra', 'Other Panels 1']],
    ['properties-walk', ['Walk 2', 'Walk 9,876'], ['Walk -1', 'Walk 1.2', 'Walk 1 extra']],
    ['job-monitor-expand', ['Expand the job monitor (0 live)', 'Expand the job monitor (42 live)'],
      ['Expand the job monitor (1,234 live)', 'Expand the job monitor (2 live) extra']],
  ]) {
    const entry = map.entries.find((row) => row.id === 'control:' + id)
    for (const state of entry.states) {
      const probe = resolveProbe(entry, state)
      for (const name of good) {
        assert.match(name, probe.locator.name)
        assert.equal(normalizedControlKey({ scope: 'document', role: 'button', name }),
          normalizedControlKey({ scope: 'document', role: 'button', name: probe.locator.normalizedName }))
      }
      for (const name of bad) assert.doesNotMatch(name, probe.locator.name)
      if (id === 'job-monitor-expand') {
        assert.deepEqual(probe.locator.scope, { role: 'toolbar', name: 'Job monitor', exact: true })
        assert.equal(probe.setup.steps.at(-1).kind, 'job-monitor-collapsed')
        assert.equal(probe.assertion.target, 'job-monitor')
      }
    }
  }
})

test('presence obligations reject duplicate resolved observations even with the correct id', () => {
  const resolved = CONTROL_CENSUS_BATCH.filter((row) => row.states.includes('ready'))
  assert.equal(requireControlCensusBatch({ resolved }), true)
  assert.throws(() => requireControlCensusBatch({ resolved: [...resolved, resolved.at(-1)] }), /missing or misresolved/)
})

test('camera navigation precedes URL navigation and collapse never invokes the reopening helper', () => {
  const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
  const oracle = source.slice(source.indexOf('async function assertEffect'))
  assert.ok(oracle.indexOf("target === 'viewer-overview-pan'") < oracle.indexOf("effect.kind === 'navigates'"))
  const collapse = oracle.slice(oracle.indexOf("if (target === 'drawing-overview-expanded')"),
    oracle.indexOf("if (/^properties-(drawing|layers|plan|selection)-section$/"))
  assert.match(collapse, /Expand drawing overview/)
  assert.match(collapse, /toHaveCount\(0\)/)
  assert.doesNotMatch(collapse, /await viewportBounds\(/)
  assert.match(source.slice(source.indexOf('async function activate'), source.indexOf('async function assertEffect')), /page\.mouse\.click/)
})

test('batch two uses observed default-build effects rather than pixel hashes or predicted pan destinations', () => {
  const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
  const properties = source.slice(source.indexOf("case 'properties-state':"),
    source.indexOf("case 'properties-section-state':"))
  assert.match(properties, /name: 'properties', exact: true/)
  assert.doesNotMatch(properties, /name: 'Properties', exact: true \}\)\.click/)
  const layerSetup = source.slice(source.indexOf("case 'layer-visible-state':"),
    source.indexOf("case 'job-monitor-collapsed':"))
  assert.match(layerSetup, /setLayer\(page, recipe\.name, recipe\.visible\)/)
  // Stack identity hashing is allowed; effect oracles still cannot hash pixels.
  assert.doesNotMatch(source.slice(source.indexOf('const groupNames')), /stableCanvas|layerImages|createHash/)
  const oracle = source.slice(source.indexOf('async function assertEffect'))
  const pan = oracle.slice(oracle.indexOf("if (target === 'viewer-overview-pan')"),
    oracle.indexOf("if (target === 'drawing-overview-expanded')"))
  assert.match(pan, /current\.x !== before\.viewport\.x \|\| current\.y !== before\.viewport\.y/)
  assert.match(pan, /data-overview-viewport/)
  assert.doesNotMatch(pan, /requireViewport|overviewPan\.expected/)
  const layers = oracle.slice(oracle.indexOf("if (/^layer-(panels|walk)-visible$/"),
    oracle.indexOf("if (target === 'job-monitor')"))
  assert.match(layers, /effect\.value\)\.toBe\(!before\.visible\)/)
  assert.match(layers, /requireLayer\(page, name, effect\.value\)/)
  assert.match(layers, /before\.layersShown \+ \(effect\.value \? 1 : -1\)/)
  assert.match(layers, /ariaSnapshot\(\)/)
})

for (const entry of map.entries) {
  for (const state of entry.states) {
    test(`${entry.id} [${state}] resolves a registry locator, state recipe and effect assertion`, () => {
      const before = JSON.stringify(entry)
      const probe = resolveProbe(entry, state)
      assert.equal(probe.featureId, entry.id)
      assert.equal(probe.state, state)
      assert.ok(probe.locator.role)
      assert.ok(typeof probe.locator.name === 'string' || probe.locator.name instanceof RegExp)
      assert.ok(probe.locator.trigger)
      assert.ok(probe.setup.steps.length)
      assert.ok(probe.setup.steps.every((step) => typeof step.kind === 'string' && step.kind.length))
      assert.equal(probe.assertion.kind, entry.expected_effect[state].kind)
      assert.equal(probe.assertion.assertionId, `${entry.id}/${state}/${entry.expected_effect[state].kind}`)
      assert.equal(JSON.stringify(entry), before, 'mapping must not mutate its registry input')
      if (probe.assertion.kind === 'disabled_with_reason') {
        assert.equal(probe.assertion.reason, entry.expected_effect[state].reason)
        assert.equal(probe.assertion.reason_code, entry.expected_effect[state].reason_code)
        if (entry.kind === 'action') {
          const action = ACTIONS.find((action) => action.id === entry.source_id)
          if (!['bar', 'slash'].includes(action.surface)) {
            const name = accessibleName(probe.locator.role === 'combobox' ? action.text : action.label, probe.assertion.reason)
            if (state === 'no-drawing') assert.match(name, probe.locator.name)
            else assert.equal(probe.locator.name, name)
          }
        }
      }
      if (['unsupported_local', 'staging'].includes(entry.certify)) {
        assert.deepEqual(probe.certification, { result: entry.certify, reason: entry.certify_reason })
      } else assert.equal(probe.certification, null)
    })
  }
}

test('unknown effect kinds and missing effects throw instead of becoming a visibility check', () => {
  const entry = structuredClone(map.entries[0])
  const state = entry.states[0]
  for (const effect of [{ kind: 'unknown', target: 'somewhere' }, undefined]) {
    entry.expected_effect[state] = effect
    assert.throws(() => resolveProbe(entry, state), /Unknown effect kind/)
  }
})

test('unknown states and incomplete effect contracts fail closed', () => {
  const entry = structuredClone(map.entries[0])
  assert.throws(() => resolveProbe(entry, 'not-a-state'), /Unknown state/)
  const state = entry.states[0]
  entry.expected_effect[state] = { kind: 'disabled_with_reason', reason: 'why' }
  assert.throws(() => effectAssertion(entry, state), /user-facing reason/)
  entry.expected_effect[state] = { kind: 'opens' }
  assert.throws(() => effectAssertion(entry, state), /needs a target/)
})

test('nav uses the visible desktop rail disclosure and its phone toggle', () => {
  const entry = map.entries.find((entry) => entry.id === 'drawer:nav')
  for (const state of ['closed', 'open']) {
    const probe = resolveProbe(entry, state)
    assert.equal(probe.locator.role, 'button')
    assert.equal(probe.locator.name, state === 'open' ? 'Collapse the tool rail to a spine' : 'Tool rail')
    assert.equal(probe.locator.phone.name, 'Tool rail')
    assert.equal(probe.locator.trigger, 'click')
    assert.deepEqual(probe.setup.steps.at(-1), { kind: 'tool-rail-state', name: 'Catalog', open: state === 'open' })
  }
})

test('the feature map includes Fit no-drawing and keeps Fit ready', () => {
  const entry = map.entries.find((entry) => entry.id === 'action:fit')
  assert.ok(entry)
  assert.equal(entry.states.includes('no-drawing'), true)
  assert.equal(Object.hasOwn(entry.expected_effect, 'no-drawing'), true)
  assert.equal(entry.states.includes('ready'), true)
  assert.equal(resolveProbe(entry, 'ready').state, 'ready')
  assert.equal(resolveProbe(entry, 'no-drawing').assertion.kind, 'disabled_with_reason')
})

test('every no-drawing action uses the real failed-load screen and never the operator /try surface', () => {
  const entries = map.entries.filter((entry) => entry.kind === 'action' && entry.states.includes('no-drawing'))
  for (const id of ['action:fit', 'action:zoom-in', 'action:zoom-out']) {
    assert.ok(entries.some((entry) => entry.id === id), `${id} must generate its no-drawing probe`)
  }
  for (const entry of entries) {
    const probe = resolveProbe(entry, 'no-drawing')
    assert.equal(probe.assertion.kind, 'disabled_with_reason')
    assert.deepEqual(probe.setup.steps[0], {
      kind: 'open-failed-drawing', url: '/app?surface=cad&drawing=missing.invalid',
    })
    assert.equal(probe.setup.steps.length, 2)
    assert.equal(probe.setup.steps[1].kind, 'failed-drawing-ribbon-tab')
    assert.ok(probe.locator.availableName)
    const action = ACTIONS.find((action) => action.id === entry.source_id)
    assert.ok(probe.locator.disabledVariants.length)
    for (const variant of probe.locator.disabledVariants) {
      assert.ok([action.when(probe.setup.context), ...(action.surface === 'engine' ? [REASONS.notInEngine] : [])].includes(variant.reason))
      assert.equal(variant.reason_code, reasonCode(variant.reason))
      assert.match(variant.name, probe.locator.name)
      assert.match(variant.name, probe.locator.unavailableName)
    }
    for (const label of [action.label, action.text]) {
      assert.match(label, probe.locator.unavailableName)
      assert.match(accessibleName(label, 'unexpected reason'), probe.locator.unavailableName)
      assert.doesNotMatch(accessibleName(label, 'unexpected reason'), probe.locator.name)
    }
    assert.ok(probe.setup.steps.every((step) => step.kind !== 'open-empty-workspace' && !step.url?.startsWith('/try')))
  }
})

test('no-drawing engine probes cover display labels and registry fallback reasons, including absent Draw controls', () => {
  const copy = resolveProbe(map.entries.find((entry) => entry.id === 'action:clipboard-copy-clip'), 'no-drawing')
  const action = ACTIONS.find((action) => action.id === copy.sourceId)
  assert.equal(copy.locator.group, 'clipboard')
  assert.ok(copy.locator.disabledVariants.some((variant) => variant.name === accessibleName(action.text, REASONS.notInEngine)
    && variant.reason === REASONS.notInEngine))
  assert.match(accessibleName(action.text, REASONS.notInEngine), copy.locator.name)
  assert.doesNotMatch(accessibleName(action.label, REASONS.notInEngine), copy.locator.name)
  const line = resolveProbe(map.entries.find((entry) => entry.id === 'action:draw-create-line'), 'no-drawing')
  const lineAction = ACTIONS.find((action) => action.id === line.sourceId)
  assert.equal(line.locator.group, 'draw')
  assert.match(lineAction.label, line.locator.unavailableName)
  assert.match(lineAction.text, line.locator.unavailableName)
  assert.match(accessibleName(lineAction.text, line.assertion.reason), line.locator.unavailableName)
  assert.doesNotMatch(accessibleName(lineAction.text, REASONS.notInEngine), line.locator.name)
})

test('Fit ready changes the viewport before asserting its return home', () => {
  const probe = resolveProbe(map.entries.find((entry) => entry.id === 'action:fit'), 'ready')
  assert.equal(probe.setup.steps.at(-1).kind, 'zoom-before-fit')
  assert.deepEqual(probe.setup.steps.at(-1).control, {
    role: 'button', name: 'Zoom in', exact: true,
    scope: { role: 'toolbar', name: 'View', exact: true },
  })
  assert.equal(probe.assertion.target, 'viewer-home')
})

test('the refreshed read tool has ready, read-only and running probes', () => {
  const entry = map.entries.find((entry) => entry.id === 'tool:count-by-layer')
  assert.ok(entry)
  assert.equal(map.entries.some((entry) => entry.id === 'tool:count-panels'), false)
  for (const state of ['ready', 'read-only', 'job-running']) {
    const probe = resolveProbe(entry, state)
    assert.ok(probe.setup.steps.some((step) => step.kind === 'catalog-tool' && step.name === 'count-by-layer'))
    assert.equal(probe.assertion.kind, state === 'ready' ? 'opens' : 'disabled_with_reason')
  }
})

test('control recipes use exact named toolbars and default-build unavailable names', () => {
  const expected = [
    ['control:grid-display', 'Drafting settings', 'Grid display'],
    ['control:object-snap', 'Drafting settings', 'Object snap'],
    ['control:ortho-mode', 'Drafting settings', 'Ortho mode'],
    ['control:polar-tracking', 'Drafting settings', 'Polar tracking'],
    ['control:snap-mode', 'Drafting settings', 'Snap mode'],
    ['control:fullscreen', 'Drafting settings', 'Toggle fullscreen'],
    ['control:view-back', 'View', 'Back to the previous view'],
    ['control:view-up', 'View', 'Up one level'],
    ['control:new-drawing', 'Quick access', 'New drawing'],
    ['control:print', 'Quick access', 'Print'],
  ]
  for (const [id, toolbar, name] of expected) {
    const entry = map.entries.find((row) => row.id === id)
    for (const state of entry.states) {
      const probe = resolveProbe(entry, state)
      const reason = probe.assertion.kind === 'disabled_with_reason' && id !== 'control:view-back' ? probe.assertion.reason : ''
      assert.equal(probe.locator.name, accessibleName(name, reason))
      assert.equal(probe.locator.role, 'button')
      assert.equal(probe.locator.exact, true)
      assert.deepEqual(probe.locator.scope, { role: 'toolbar', name: toolbar, exact: true })
      assert.equal(probe.setup.steps[0].kind, state === 'failed-load' ? 'open-failed-drawing' : 'open-private-drawing')
      assert.equal(probe.certification, null)
      assert.ok(probe.setup.steps.every((step) => step.kind !== 'engine-ready' && step.kind !== 'require-local-state'))
      if (probe.assertion.kind === 'disabled_with_reason') assert.ok(probe.locator.tooltip)
    }
  }
})

test('grid and fullscreen establish opposite starting states through their real scoped controls', () => {
  for (const [id, states, stepKind, field, target] of [
    ['control:grid-display', ['off', 'on'], 'control-pressed-state', 'pressed', 'drafting-grid'],
    ['control:fullscreen', ['windowed', 'fullscreen'], 'fullscreen-state', 'fullscreen', 'document-fullscreen'],
  ]) {
    const entry = map.entries.find((row) => row.id === id)
    for (const [index, state] of states.entries()) {
      const probe = resolveProbe(entry, state)
      assert.equal(probe.setup.steps.at(-1).kind, stepKind)
      assert.equal(probe.setup.steps.at(-1)[field], index === 1)
      assert.deepEqual(probe.setup.steps.at(-1).control, probe.locator)
      assert.equal(probe.assertion.kind, 'toggles')
      assert.equal(probe.assertion.target, target)
      assert.equal(probe.assertion.value, index === 0)
    }
  }
})

test('Back requires description evidence for empty history and view restoration for populated history', () => {
  const entry = map.entries.find((row) => row.id === 'control:view-back')
  const empty = resolveProbe(entry, 'empty-history')
  assert.equal(empty.setup.steps.at(-1).kind, 'empty-view-history')
  assert.equal(empty.locator.name, 'Back to the previous view')
  assert.equal(empty.locator.description, 'There is no earlier view to go back to')
  assert.equal(empty.locator.tooltip, empty.assertion.reason)
  assert.equal(empty.assertion.reason_code, 'control:view-back:unavailable')
  const history = resolveProbe(entry, 'history-present')
  assert.equal(history.setup.steps.at(-1).kind, 'previous-view-history')
  assert.equal(history.assertion.kind, 'navigates')
  assert.equal(history.assertion.target, 'viewer-previous-view')
  const up = resolveProbe(map.entries.find((row) => row.id === 'control:view-up'), 'whole-drawing')
  assert.equal(up.setup.steps.at(-1).kind, 'whole-drawing-view')
  assert.equal(up.assertion.target, 'viewer-whole-drawing')
  const url = effectAssertion({ expected_effect: { ready: { kind: 'navigates', target: '/projects' } } }, 'ready')
  assert.equal(url.kind, 'navigates')
  assert.equal(url.target, '/projects')
})
