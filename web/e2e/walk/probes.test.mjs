import assert from 'node:assert/strict'
import test from 'node:test'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { ACTIONS, REASONS, accessibleName, reasonCode } from '../../src/lib/actionRegistry.js'
import { effectAssertion, resolveProbe, normalizedControlKey, requireControlCensusBatch, CONTROL_CENSUS_BATCH, CENSUS_RECIPE_CONTROLS } from './probes.mjs'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { observeSolarEditorRequests, solarAvailabilityProbe } from './fixtures.mjs'
import { setupStep, stackInstanceRef, UnsupportedLocalError, holdJobRoutes, discloseControlPanel, assertEffect, unsupportedBeforeSetup, UI_UNREACHABLE_STATES, VERSIONLESS_DRAWING_REASON, SOLAR_PANEL_CALIBRATION_REASON, workerCatalog, toolAvailabilityEvidence, FIXTURE_PICK_POINTS, exposedCalibrationPoints, solarCalibrationFailure, injectWalkEntities } from './fixtures.mjs'

const map = buildFeatureMap()

test('G4 selection-set probes reuse the real two-LINE recipe and registry oracles', () => {
  for (const op of ['delete', 'move', 'copy', 'rotate', 'scale', 'mirror']) {
    const entry = map.entries.find((entry) => entry.id === `action:modify-${op}`)
    const probe = resolveProbe(entry, 'multiple-selected')
    assert.deepEqual(probe.setup.steps.filter((step) => step.kind === 'select-entity'),
      [{ kind: 'select-entity', type: 'LINE', editable: true, multiple: true }])
    assert.equal(probe.assertion.operation, op)
    assert.equal(probe.assertion.target, op === 'delete' ? 'engine:delete' : 'cockpit-prompt')
    assert.equal(probe.assertion.kind, op === 'delete' ? 'submits' : 'opens')
    assert.equal(probe.locator.name, ACTIONS.find((action) => action.id === entry.source_id).label)
  }
})

test('G4 each selection-set prompt requires the armed verb and two selected objects', async () => {
  for (const op of ['move', 'copy', 'rotate', 'scale', 'mirror']) {
    const probe = resolveProbe(map.entries.find((entry) => entry.id === `action:modify-${op}`), 'multiple-selected')
    const calls = []
    const page = { getByTestId: (id) => id }
    const assertions = (value) => ({
      toBe: (expected) => assert.equal(value, expected),
      toBeVisible: async () => calls.push(['visible', value]),
      toHaveAccessibleName: async (name) => calls.push(['name', value, name]),
      toHaveText: async (text) => calls.push(['text', value, text]),
    })
    const runtime = { page, evidence: {} }
    await assertEffect(probe, runtime, {}, { selectionCount: 2 }, assertions)
    assert.deepEqual(calls, [['visible', 'cockpit-prompt'], ['name', 'cockpit-prompt', `${probe.assertion.verb} command`],
      ['text', 'dock-selection-count', '2 objects selected']])
    assert.deepEqual(runtime.evidence.multipleSelection, { selectionCount: 2, verb: probe.assertion.verb, promptArmed: true })
    await assert.rejects(assertEffect(probe, runtime, {}, { selectionCount: 1 }, assertions), assert.AssertionError)
  }
})

test('G3 ready Solar refusals locate exact disabled names from scoped catalog availability', () => {
  for (const [name, code, reason] of [
    ['solar-equipment-move', 'equipment_assignment_required', 'Assign inverter equipment first'],
    ['solar-cable-export', 'solar_output_not_current', 'Rerun the earlier Solar steps so the whole design is current first'],
    ['solar-electrical-schedules', 'solar_output_not_current', 'Rerun the earlier Solar steps so the whole design is current first'],
    ['solar-solaredge-accept', 'frames_required', 'Create panel groups first'],
  ]) {
    const entry = map.entries.find((row) => row.id === `tool:${name}`)
    const initial = resolveProbe(entry, 'ready')
    assert.equal(initial.assertion.target, 'solar-step-editor')
    assert.equal(initial.locator.name, accessibleName(name, initial.assertion.reason))
    const snapshot = JSON.parse(readFileSync(new URL('../../walk/fixtures/capabilities.snapshot.json', import.meta.url), 'utf8'))
    const tool = snapshot.response.families.flatMap((family) => family.capabilities).find((tool) => tool.name === name)
    tool.availability = { entitled: true, engine_ready: true, implemented: true,
      input_ready: false, refusal_reasons: [code] }
    const probe = solarAvailabilityProbe(initial, snapshot.response)
    assert.equal(probe.locator.name, `${name} (unavailable: ${reason})`)
    assert.equal(probe.locator.exact, true)
    assert.equal(probe.assertion.reason_code, code)
    assert.equal(probe.assertion.assertionId, `${entry.id}/ready/disabled_with_reason`)
    assert.deepEqual(probe.setup, initial.setup)
    tool.availability.input_ready = true
    const ready = solarAvailabilityProbe(initial, snapshot.response)
    assert.equal(ready.assertion.target, 'solar-step-editor')
    assert.equal(ready.locator.name, name)
    delete tool.availability
    assert.deepEqual(solarAvailabilityProbe(initial, snapshot.response), ready)
    const locked = resolveProbe(entry, 'job-running')
    assert.equal(solarAvailabilityProbe(locked, snapshot.response), locked)
    const runDecision = { ...initial, assertion: { ...initial.assertion, target: 'catalog-run-decision' } }
    tool.availability = { entitled: true, engine_ready: true, implemented: true,
      input_ready: false, refusal_reasons: [code] }
    assert.equal(solarAvailabilityProbe(runDecision, snapshot.response), runDecision)
  }
})

test('engine history lives in Quick access without a ribbon group or tab', () => {
  for (const id of ['action:engine-undo', 'action:engine-redo']) {
    const entry = map.entries.find((row) => row.id === id)
    for (const state of entry.states) {
      const probe = resolveProbe(entry, state)
      assert.deepEqual(probe.locator.scope, { role: 'toolbar', name: 'Quick access', exact: true })
      assert.equal(probe.locator.group, undefined)
      assert.equal(probe.locator.role, 'button')
      assert.ok(!probe.setup.steps.some((step) => step.kind.includes('ribbon-tab')))
      const label = id === 'action:engine-undo' ? 'Undo edit' : 'Redo edit'
      if (state === 'ready') assert.equal(probe.locator.name, label)
      else {
        assert.match(`${label} (unavailable: changed refusal)`, probe.locator.unavailableName)
        assert.doesNotMatch(`${label} version`, probe.locator.unavailableName)
      }
    }
    const kinds = resolveProbe(entry, 'ready').setup.steps.map((step) => step.kind)
    assert.equal(kinds.filter((kind) => kind === 'create-line').length, 1)
    assert.equal(kinds.includes('undo-edit'), id === 'action:engine-redo')
  }
})

test('Repeat uses Enter from eligible focus and seeds an accepted LINE only for ready', () => {
  const entry = map.entries.find((row) => row.id === 'action:engine-repeat')
  for (const state of entry.states) {
    const probe = resolveProbe(entry, state)
    assert.equal(probe.locator.trigger, 'keyboard')
    assert.equal(probe.locator.key, 'Enter')
    assert.equal(probe.locator.keyboardAction, 'engine:repeat')
    assert.equal(probe.locator.name, state === 'no-drawing' ? '' : 'Drawing')
    assert.equal(probe.locator.role, state === 'no-drawing' ? 'main' : 'region')
    assert.equal(probe.locator.scope, undefined)
    assert.equal(probe.locator.unavailableName, undefined)
    assert.ok(!probe.setup.steps.some((step) => step.kind.includes('ribbon-tab')))
    assert.equal(probe.setup.steps.some((step) => step.kind === 'create-line'), state === 'ready')
    if (state === 'ready') assert.equal(probe.assertion.verb, 'LINE')
    if (state === 'no-command-to-repeat') assert.ok(probe.setup.steps.some((step) => step.kind === 'engine-ready'))
  }
})

test('G1 Solar editor reviews, dismisses and cancels without submitting a run', async () => {
  for (const [tool, submitted, railVisible] of [
    ['solar-unit-sync', false, true], ['solar-unit-sync', true, true], ['solar-design-presets', false, true],
    ['solar-unit-sync', false, false], ['solar-unit-sync', true, false], ['solar-design-presets', false, false],
  ]) {
    const calls = []
    const inputs = []
    let listener, visible = true, decisionVisible = false, focused = null
    const locator = { click: async () => { visible = true }, isVisible: async () => railVisible }
    const more = {}
    const field = { count: async () => 0 }
    const review = { click: async () => {
      calls.push('review'); decisionVisible = true
      if (submitted) listener({ method: () => 'POST', url: () => 'http://walk/api/run?wait=1' })
    } }
    const cancel = { click: async () => { calls.push('cancel'); visible = false; focused = railVisible ? locator : more } }
    const region = { getByLabel: (name) => {
      if (name === 'Expected rev') return { count: async () => 1, inputValue: async () => '',
        fill: async (value) => { inputs.push([name, value]) } }
      if (name === 'Distance unit' && tool === 'solar-unit-sync' || name === 'Subcommand' && tool === 'solar-design-presets') {
        return { count: async () => 1, selectOption: async (option) => { inputs.push([name, option]) } }
      }
      if (name === 'Name' && tool === 'solar-design-presets') return { count: async () => 1,
        fill: async (value) => { inputs.push([name, value]) } }
      return field
    }, isVisible: async () => visible,
      getByRole: (role, options) => options.name === 'Review & run' ? review : cancel }
    const decision = { focus: async () => { calls.push('decision-focus') } }
    const page = {
      locator: (selector) => { assert.equal(selector, 'button[aria-controls="drafting-ribbon-panels"]'); return more },
      on: (event, handler) => { assert.equal(event, 'request'); listener = handler },
      off: (event, handler) => { assert.equal(event, 'request'); assert.equal(handler, listener); calls.push('unobserve') },
      getByRole: (role, options) => {
        if (role === 'region') { assert.equal(options.name, `${tool} parameters`); return region }
        assert.equal(options.name, `Run ${tool}`); return decision
      },
      keyboard: { press: async (key) => { assert.equal(key, 'Escape'); calls.push('dismiss'); decisionVisible = false } },
    }
    const assertions = (value) => ({
      toBeVisible: async () => assert.equal(value === region ? visible : decisionVisible, true),
      toBeHidden: async () => assert.equal(value === region ? visible : decisionVisible, false),
      toBeEnabled: async () => assert.equal(value, review),
      toBeFocused: async () => { assert.equal(value, railVisible ? locator : more); assert.equal(focused, value) },
      toEqual: (expected) => assert.deepEqual(value, expected),
    })
    const runtime = { page, cleanup: [], evidence: {} }
    const before = observeSolarEditorRequests(runtime)
    listener({ method: () => 'GET', url: () => 'http://walk/api/run' })
    listener({ method: () => 'POST', url: () => 'http://walk/api/other' })
    const probe = { kind: 'tool', assertion: { target: 'solar-step-editor', tool } }
    const pending = assertEffect(probe, runtime, locator, before, assertions)
    if (submitted) await assert.rejects(pending, assert.AssertionError)
    else {
      await pending
      assert.deepEqual(runtime.evidence.solarStepEditor, { tool, reviewed: true, cancelled: true, runRequests: [] })
    }
    assert.deepEqual(inputs, tool === 'solar-unit-sync'
      ? [['Expected rev', '0'], ['Distance unit', { label: 'Meters' }]]
      : [['Expected rev', '0'], ['Subcommand', { label: 'Create' }], ['Name', 'Walk proof preset']])
    await runtime.cleanup[0]()
    assert.deepEqual(calls, ['review', 'decision-focus', 'dismiss', 'cancel', 'unobserve'])
  }
})

test('G1 run observation starts before activation and remains through Cancel', () => {
  const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
  const capture = source.slice(source.indexOf('async function captureBefore('), source.indexOf('async function activate('))
  assert.match(capture, /solar-step-editor.*return observeSolarEditorRequests\(runtime\)/)
  const oracle = source.slice(source.indexOf('export async function assertSolarStepEditor('), source.indexOf('export function seedSignOutIdentity'))
  assert.ok(oracle.indexOf("name: 'Cancel'") < oracle.indexOf('assertions(before.runRequests).toEqual([])'))
  assert.match(oracle, /assertions\(locator\)\.toBeFocused/)
})

test('G1 Solar form probes retain the editor effect and catalog setup', () => {
  const snapshot = JSON.parse(readFileSync(new URL('../../walk/fixtures/capabilities.snapshot.json', import.meta.url), 'utf8'))
  for (const tool of snapshot.response.families.flatMap((family) => family.capabilities)) delete tool.availability
  const readyMap = buildFeatureMap({ snapshot })
  for (const id of ['tool:solar-unit-sync', 'tool:solar-design-presets']) {
    const probe = resolveProbe(readyMap.entries.find((row) => row.id === id), 'ready')
    assert.equal(probe.assertion.target, 'solar-step-editor')
    assert.equal(probe.assertion.tool, probe.sourceId)
    assert.deepEqual(probe.setup.steps.at(-1), { kind: 'catalog-tool', name: probe.sourceId })
  }
})
test('C2 Objects uses the native disclosure summary for every state', () => {
  const entry = map.entries.find((entry) => entry.id === 'control:objects')
  for (const state of entry.states) {
    const probe = resolveProbe(entry, state)
    assert.deepEqual(probe.locator, { role: 'group', name: entry.state_contexts[state].name, exact: true,
      css: 'details.drawing-objects-panel > summary',
      trigger: 'click', tooltip: entry.state_contexts[state].tooltip,
      description: entry.state_contexts[state].description })
    assert.deepEqual(probe.setup.steps.at(-1).control, probe.locator)
    assert.equal(probe.assertion.target, 'drawing-objects-expanded')
  }
  const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
  assert.match(source, /return recipe\.selector \? target\.locator\(recipe\.selector\) : target/)
})

test('control resolves CSS recipes directly and preserves role recipes', () => {
  const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
  const implementation = source.slice(source.indexOf('const control ='), source.indexOf('export async function discloseControlPanel'))
  const control = new Function('groupNames', `${implementation}; return control`)({ draw: 'Draw' })
  const calls = []
  const target = {}
  const scope = {
    locator: (css) => { calls.push(['css', css]); return target },
    getByRole: (role, options) => { calls.push(['role', role, options]); return scope },
  }
  const page = {
    locator: (css) => { calls.push(['page-css', css]); return target },
    getByRole: (role, options) => { calls.push(['page-role', role, options]); return scope },
  }
  const css = 'details.drawing-objects-panel > summary'
  assert.equal(control(page, { css }), target)
  assert.deepEqual(calls.splice(0), [['page-css', css]])
  assert.equal(control(page, { css, scope: { role: 'region', name: 'Drawing' } }), target)
  assert.deepEqual(calls.splice(0), [
    ['page-role', 'region', { name: 'Drawing', exact: true }], ['css', css],
  ])
  for (const css of [undefined, '', null, 42]) {
    assert.equal(control(page, { css, role: 'button', name: 'Line', group: 'draw',
      scope: { role: 'toolbar', name: 'Drafting tools' }, selector: 'span', exact: false }), target)
    assert.deepEqual(calls.splice(0), [
      ['page-role', 'toolbar', { name: 'Drafting tools', exact: true }],
      ['role', 'group', { name: 'Draw', exact: true }],
      ['role', 'button', { name: 'Line', exact: false }], ['css', 'span'],
    ])
  }
  assert.equal(control(page, { role: 'button', name: 'Line' }), scope)
  assert.deepEqual(calls, [['page-role', 'button', { name: 'Line', exact: true }]])
})

test('C3 snap disclosure uses the Drafting settings trigger and a real menu oracle', () => {
  const entry = map.entries.find((entry) => entry.id === 'control:object-snap-modes')
  for (const state of entry.states) {
    const probe = resolveProbe(entry, state)
    assert.deepEqual(probe.locator.scope, { role: 'toolbar', name: 'Drafting settings', exact: true })
    assert.equal(probe.locator.role, 'button')
    assert.equal(probe.locator.name, 'Object snap modes')
    assert.equal(probe.assertion.target, 'object-snap-menu-expanded')
    assert.deepEqual(probe.setup.steps.map((recipe) => recipe.kind), [
      state === 'failed-load' ? 'open-failed-drawing' : 'open-private-drawing', 'census-disclosure',
    ])
  }
  const source = readFileSync(new URL('./w1z-census-snap.spec.mjs', import.meta.url), 'utf8')
  assert.match(source, /process\.env\.LEAF_WALK_PROOF === '1'/)
  assert.match(source, /for \(const state of entry\.states\)/)
  assert.match(source, /control-census:studio \[ready\]/)
  assert.match(source, /requireControlCensusBatch\(census/)
})

test('C2 and C3 resolve all 44 census rows to real setup and effect recipes', () => {
  let rows = 0
  for (const id of CENSUS_RECIPE_CONTROLS) {
    const entry = map.entries.find((entry) => entry.id === id)
    for (const state of entry.states) {
      rows++
      const probe = resolveProbe(entry, state)
      const kinds = probe.setup.steps.map((recipe) => recipe.kind)
      assert.equal(probe.certification, null, `${id}/${state}`)
      assert.equal(unsupportedBeforeSetup(probe), null, `${id}/${state}`)
      assert.equal(probe.assertion.assertionId, `${id}/${state}/${entry.expected_effect[state].kind}`)
      assert.equal(kinds.includes('require-local-state'), false)
      assert.equal(kinds.includes('open-failed-drawing'), state === 'failed-load' || state === 'no-document')
      assert.equal(kinds.includes('prepare-engine-transport'), state === 'engine-busy' || state === 'running')
      if (probe.assertion.kind === 'toggles') {
        assert.deepEqual(probe.setup.steps.at(-1), { kind: 'census-disclosure', control: probe.locator,
          target: probe.assertion.target, expanded: !probe.assertion.value })
      }
      if (['ribbon-script', 'choose-script', 'run-script'].includes(entry.source_id)) {
        assert.ok(probe.setup.steps.some((recipe) => recipe.kind === 'ribbon-tab' && recipe.name === 'View'))
        const recipe = probe.setup.steps.at(-1)
        assert.equal(recipe.kind, 'census-script')
        assert.equal(recipe.running, state === 'running')
        assert.equal(recipe.text, state === 'running' || entry.source_id === 'run-script' && state !== 'empty-script' ? 'line 0,0 10,10' : '')
      }
      if (state === 'engine-busy') assert.ok(kinds.includes('hold-engine-edit'))
      if (state === 'engine-crashed') assert.ok(kinds.includes('crash-engine-worker'))
      if (state.startsWith('nothing-')) assert.ok(kinds.includes('fresh-history'))
      if (state === 'no-target') assert.deepEqual(kinds, ['census-download-only', 'engine-ready', 'create-line'])
      if (id === 'control:redo-edit' && state === 'ready') assert.deepEqual(kinds.slice(-2), ['create-line', 'undo-edit'])
      if (id === 'control:undo-edit' && state === 'ready') assert.equal(kinds.at(-1), 'create-line')
      if (id === 'control:save-version' && state === 'engine-busy') assert.ok(kinds.indexOf('create-line') < kinds.indexOf('hold-engine-edit'))
    }
  }
  assert.equal(rows, 44)
  const source = readFileSync(new URL('./w1z-census.spec.mjs', import.meta.url), 'utf8')
  assert.match(source, /process\.env\.LEAF_WALK_PROOF === '1'/)
  assert.match(source, /for \(const featureId of CENSUS_RECIPE_CONTROLS\)/)
  assert.match(source, /for \(const state of entry\.states\)/)
  assert.match(source, /expect\(result\?\.unsupported, result\?\.reason\)\.not\.toBe\(true\)/)
  assert.match(source, /expect\(walkEvidence\.cleanupCompleted\)\.toBe\(true\)/)
})

test('C1 reachable faults replace only their matching unsupported declarations', () => {
  for (const [id, state, kind, redo] of [
    ['history', 'version-changing', 'hold-version-change', false],
    ['undo', 'version-changing', 'hold-version-change', false],
    ['redo', 'version-changing', 'hold-version-change', true],
    ['undo', 'mutations-blocked', 'fault-restored-head', false],
    ['redo', 'mutations-blocked', 'fault-restored-head', true],
    ['bar-retry', 'retry-history', 'fault-history', false],
  ]) {
    const probe = resolveProbe(map.entries.find((entry) => entry.id === `action:${id}`), state)
    assert.deepEqual(probe.setup.steps.at(-1), { kind, redo })
    assert.ok(probe.setup.steps.every((step) => step.kind !== 'require-local-state'))
    assert.equal(unsupportedBeforeSetup(probe), null)
    assert.equal(probe.setup.steps.some((step) => step.kind === 'foreign-checkout'), false)
  }
  for (const [id, state] of [
    ['bar-retry', 'retry-tools'], ['bar-retry', 'retry-catalog'],
    ['bar-retry', 'retry-refresh'], ['bar-retry', 'result-owns-retry'],
  ]) {
    const probe = resolveProbe(map.entries.find((entry) => entry.id === `action:${id}`), state)
    assert.equal(probe.setup.steps.at(-1).kind, 'require-local-state')
    assert.match(unsupportedBeforeSetup(probe), /no public fixture recipe/)
  }
})
test('project-open declares the isolated stack platform router limitation', async () => {
  const reason = 'the isolated walk stack runs without Postgres, so the platform router (/api/orgs, /api/projects) is not mounted; project-open needs a platform-enabled stack'
  const probe = resolveProbe(map.entries.find((entry) => entry.id === 'action:bar-escape'), 'project-open')
  const recipe = probe.setup.steps.at(-1)
  assert.equal(recipe.kind, 'require-local-state')
  assert.equal(recipe.reason, reason)
  assert.equal(unsupportedBeforeSetup(probe), reason)
  assert.ok(probe.setup.steps.every((step) => step.kind !== 'open-real-project'))
  const evidence = {}
  await assert.rejects(setupStep(probe, {
    page: {}, evidence, testInfo: { attach: async () => {} },
  }, recipe), (error) => error instanceof UnsupportedLocalError && error.reason === reason)
  assert.equal(evidence.result.reason, reason)
})

test('route-error probes declare the real nlPrompt fallback limitation', async () => {
  const reason = 'route errors are absorbed into local suggestions by api.js nlPrompt (transport failures and non-401 HTTP errors); routeErr is not reachable through a real fault'
  for (const [id, state] of [['bar-escape', 'errors-present'], ['bar-retry', 'retry-route']]) {
    const probe = resolveProbe(map.entries.find((entry) => entry.id === `action:${id}`), state)
    const recipe = probe.setup.steps.at(-1)
    assert.equal(recipe.kind, 'require-local-state')
    assert.equal(recipe.reason, reason)
    assert.equal(unsupportedBeforeSetup(probe), reason)
    const evidence = {}
    await assert.rejects(setupStep(probe, {
      page: {}, evidence, testInfo: { attach: async () => {} },
    }, recipe), (error) => error instanceof UnsupportedLocalError && error.reason === reason)
    assert.equal(evidence.result.reason, reason)
  }
})
test('B1 empty surface hosts use /try while Studio readiness holds its projection before upload', () => {
  for (const [id, state] of [['browser', 'signed-out'], ['solar', 'no-drawing']]) {
    const probe = resolveProbe(map.entries.find((entry) => entry.id === `surface:${id}`), state)
    assert.equal(probe.locator.role, 'main')
    assert.equal(probe.locator.name, 'Leaf operator workspace')
    assert.equal(probe.locator.trigger, 'navigate')
    assert.equal(probe.locator.url, `/try?surface=${id}`)
    assert.equal(probe.setup.steps[0].kind, 'open-empty-workspace')
  }
  const pending = resolveProbe(map.entries.find((entry) => entry.id === 'surface:solar'), 'solar-not-ready')
  assert.equal(pending.setup.steps[0].kind, 'hold-solar-projection')
  assert.equal(pending.setup.steps[1].kind, 'open-private-drawing')
  assert.equal(pending.locator.role, 'tab')
  assert.equal(pending.assertion.state, 'beta')
  const ready = resolveProbe(map.entries.find((entry) => entry.id === 'surface:solar'), 'ready')
  assert.ok(ready.setup.steps.every((step) => step.kind !== 'hold-solar-projection'))
})
test('scoped catalogs reevaluate each drawing and version without reusing worker readiness', async () => {
  const requests = []
  const facts = {}
  const probe = { kind: 'tool', sourceId: 'solar-settings', assertion: { kind: 'opens', target: 'catalog-run-decision' } }
  let ready = false
  const request = { get: async (path) => {
    requests.push(path)
    const input_ready = path.includes('drawing_id=second') || ready
    return { ok: () => true, json: async () => ({ families: [{ capabilities: [{ name: 'solar-settings',
      availability: { entitled: true, engine_ready: true, implemented: true, input_ready,
        refusal_reasons: input_ready ? [] : ['graph_seed_required'] } }] }] }) }
  } }
  await workerCatalog(facts, request)
  assert.deepEqual(toolAvailabilityEvidence(probe, facts).refusal_codes, ['graph_seed_required'])
  await workerCatalog(facts, request, { drawingId: 'first', version: 1 })
  assert.deepEqual(toolAvailabilityEvidence(probe, facts).availability_fields_false, ['input_ready'])
  await workerCatalog(facts, request, { drawingId: 'second', version: 2 })
  assert.equal(toolAvailabilityEvidence(probe, facts), null)
  await workerCatalog(facts, request, { drawingId: 'first', version: 1 })
  assert.deepEqual(toolAvailabilityEvidence(probe, facts).availability_fields_false, ['input_ready'])
  ready = true
  await workerCatalog(facts, request, { drawingId: 'first', version: 2 })
  assert.equal(toolAvailabilityEvidence(probe, facts), null)
  assert.deepEqual(requests, ['/api/capabilities',
    '/api/capabilities?drawing_id=first&drawing_version=1',
    '/api/capabilities?drawing_id=second&drawing_version=2',
    '/api/capabilities?drawing_id=first&drawing_version=1',
    '/api/capabilities?drawing_id=first&drawing_version=2'])
})

test('Solar availability is evaluated after drawing and graph setup', () => {
  const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
  const runner = source.slice(source.indexOf('export async function runProbe'), source.indexOf('// This reporter'))
  assert.ok(runner.indexOf('await workerCatalog') > runner.indexOf('for (const recipe of probe.setup.steps)'))
  for (const id of ['tool:solar-settings', 'tool:solar-autofill']) {
    const probe = resolveProbe(map.entries.find((entry) => entry.id === id), 'ready')
    const kinds = probe.setup.steps.map((step) => step.kind)
    assert.ok(kinds.indexOf('open-private-drawing') < kinds.indexOf('seed-solar-graph'))
    assert.ok(kinds.indexOf('seed-solar-graph') < kinds.indexOf('catalog-tool'))
  }
})

test('every ready Solar catalog recipe opens a standalone drawing on the Solar profile', () => {
  const entries = map.entries.filter((entry) => entry.kind === 'tool'
    && entry.source_id.startsWith('solar-') && entry.states.includes('ready'))
  assert.ok(entries.length > 0)
  for (const entry of entries) {
    const probe = resolveProbe(entry, 'ready')
    assert.deepEqual(probe.setup.steps[0], { kind: 'open-private-drawing', surface: 'solar', signedOut: false })
    assert.equal(probe.setup.steps.at(-1).kind, 'catalog-tool')
  }
})

test('the DIMENSION pick lies on the fixture dimension line and outside the other fixture geometry', () => {
  const dxf = injectWalkEntities(readFileSync(new URL('../fixtures/distinctive-panel.dxf', import.meta.url), 'utf8')).trim().split(/\r?\n/)
  const start = dxf.indexOf('DIMENSION')
  assert.ok(start > 0)
  const fields = new Map()
  for (let index = start + 1; index < dxf.length && dxf[index] !== '0'; index += 2) fields.set(dxf[index], dxf[index + 1])
  assert.equal(fields.get('5'), 'D100')
  assert.equal(Number(fields.get('70')) & 15, 0)
  const [x, y] = FIXTURE_PICK_POINTS.DIMENSION[0]
  assert.equal(y, Number(fields.get('20')))
  assert.ok(x > Number(fields.get('13')) && x < Number(fields.get('14')))
  assert.ok(y < 190 && y < 222.5)
  const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
  assert.match(source, /const points = FIXTURE_PICK_POINTS\[recipe.type\]/)
})

test('selection closes expanded overflow before calibration and leaves collapsed or absent overflow alone', async () => {
  const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
  const selection = source.slice(source.indexOf('async function selectEntity'), source.indexOf('async function setDrawer'))
  const assertions = (value) => ({ toHaveAttribute: async (key, expected) => {
    assert.equal(await value.getAttribute(key), expected)
  } })
  const select = new Function('expect', 'FIXTURE_PICK_POINTS', 'canvas', 'exposedCalibrationPoints',
    `${selection}; return selectEntity`)(assertions, FIXTURE_PICK_POINTS, (page) => page.drawing, exposedCalibrationPoints)
  for (const initial of ['true', 'false', null]) {
    let expanded = initial
    const events = []
    const more = {
      isVisible: async () => initial !== null,
      getAttribute: async (key) => { assert.equal(key, 'aria-expanded'); return expanded },
      click: async (options) => { assert.equal(options.timeout, 15_000); events.push('collapse'); expanded = 'false' },
    }
    const page = {
      getByRole: (role, options) => {
        assert.equal(role, 'button')
        assert.deepEqual(options, { name: 'More panels', exact: true })
        return more
      },
      drawing: { evaluate: async (callback) => {
        assert.equal(callback, exposedCalibrationPoints)
        assert.notEqual(expanded, 'true', 'overflow must be closed before sampling')
        events.push('sample')
        return null
      } },
    }
    await assert.rejects(select({}, { page }, { type: 'LINE', viewerOnly: true }), /two uncovered calibration points/)
    assert.deepEqual(events, initial === 'true' ? ['collapse', 'sample'] : ['sample'])
  }
})

test('calibration finds exposed edge positions when the old central grid is covered', () => {
  const original = { document: globalThis.document, innerWidth: globalThis.innerWidth, innerHeight: globalThis.innerHeight }
  const canvas = { getBoundingClientRect: () => ({ left: 0, top: 0, right: 400, bottom: 300 }) }
  globalThis.innerWidth = 400
  globalThis.innerHeight = 300
  globalThis.document = { elementFromPoint: (x) => x < 50 ? canvas : null }
  try {
    const samples = exposedCalibrationPoints(canvas)
    assert.equal(samples.length, 2)
    assert.ok(samples.every((point) => point.x < 50))
    assert.ok(Math.abs(samples[1].x - samples[0].x) > 20)
    assert.ok(Math.abs(samples[1].y - samples[0].y) > 20)
    globalThis.document.elementFromPoint = () => null
    assert.equal(exposedCalibrationPoints(canvas), null)
  } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('surface recipes name their own surface and only CAD applies the APS readiness gate', async () => {
  for (const [surface, state] of [['browser', 'ready'], ['solar', 'solar-not-ready'], ['cad', 'ready']]) {
    const probe = resolveProbe(map.entries.find((entry) => entry.id === `surface:${surface}`), state)
    const recipe = probe.setup.steps.find((step) => step.kind === 'require-surface-context')
    assert.equal(recipe.surface, surface)
    const runtime = { evidence: {}, testInfo: { attach: async () => {} }, page: { request: {
      get: async (path) => {
        assert.equal(path, '/api/health')
        return { ok: () => true, json: async () => ({ aps_live: false }) }
      },
    } } }
    if (surface === 'cad') await assert.rejects(setupStep(probe, runtime, recipe), UnsupportedLocalError)
    else await setupStep(probe, runtime, recipe)
  }
})

test('W1x catalog recipes retain their family panel and reseat it after state setup', () => {
  for (const [id, state, setup] of [
    ['tool:solar-panel-add', 'unsaved-engine-edits', 'create-line'],
    ['tool:solar-design-presets', 'write-unentitled', 'private-policy'],
  ]) {
    const probe = resolveProbe(map.entries.find((entry) => entry.id === id), state)
    assert.ok(probe.locator.panelName)
    assert.ok(probe.locator.panelId)
    assert.equal(probe.setup.steps.at(-1).kind, 'catalog-tool')
    assert.ok(probe.setup.steps.findIndex((step) => step.kind === setup) < probe.setup.steps.length - 1)
  }
})

test('Solar actions derive Solar surface and tab from their panel while Rail uses Manage', () => {
  for (const action of ACTIONS.filter((action) => action.panel === 'solar-panels')) {
    const entry = map.entries.find((entry) => entry.kind === 'action' && entry.source_id === action.id)
    for (const state of entry.states.filter((state) => state !== 'no-drawing')) {
      const probe = resolveProbe(entry, state)
      assert.equal(probe.setup.steps.find((step) => step.kind === 'open-private-drawing').surface, 'solar')
      assert.equal(probe.setup.steps.find((step) => step.kind === 'ribbon-tab').name, 'Solar')
    }
  }
  const rail = resolveProbe(map.entries.find((entry) => entry.id === 'action:rail-expand'), 'ready')
  assert.equal(rail.setup.steps.find((step) => step.kind === 'ribbon-tab').name, 'Manage')
})

test('Solar panel selection reaches setup and only the measured calibration failure is declared', () => {
  assert.equal(SOLAR_PANEL_CALIBRATION_REASON, 'The private DXF fixture has no two uncovered calibration points for solar panel placement.')
  for (const action of ACTIONS.filter((action) => action.panel === 'solar-panels')) {
    const entry = map.entries.find((entry) => entry.kind === 'action' && entry.source_id === action.id)
    for (const state of entry.states) {
      const probe = resolveProbe(entry, state)
      assert.notEqual(unsupportedBeforeSetup(probe), SOLAR_PANEL_CALIBRATION_REASON, `${entry.id}/${state}`)
      for (const recipe of probe.setup.steps) {
        assert.equal(solarCalibrationFailure(probe, recipe, new Error('The drawing needs two uncovered calibration points')),
          recipe.kind === 'select-entity' && !recipe.viewerOnly)
        assert.equal(solarCalibrationFailure(probe, recipe, new Error('selection failed')), false)
      }
    }
  }
  const move = resolveProbe(map.entries.find((entry) => entry.id === 'action:solar-panels-move'), 'ready')
  assert.equal(unsupportedBeforeSetup(move), null)
  const rectangle = resolveProbe(map.entries.find((entry) => entry.id === 'action:solar-panels-create-rectangle'), 'ready')
  assert.equal(unsupportedBeforeSetup(rectangle), null)
})

test('Solar panel disclosure resolves the actual Solar tab seat inside Drafting tools', async () => {
  const page = { getByRole: (role, options) => {
    assert.equal(role, 'toolbar')
    assert.equal(options.name, 'Drafting tools')
    return { getByRole: (role, options) => {
      assert.equal(role, 'group')
      assert.equal(options.name, 'Panel placement')
      assert.equal(options.exact, true)
      return { count: async () => 1, isVisible: async () => true }
    } }
  } }
  const probe = resolveProbe(map.entries.find((entry) => entry.id === 'action:solar-panels-create-rectangle'), 'ready')
  assert.equal(probe.locator.group, 'solar-panels')
  assert.equal(probe.locator.name, 'Panel outline')
  await discloseControlPanel(page, probe.locator)
})

test('clipboard input is seeded for paste and never as setup for copy or cut', () => {
  for (const entry of map.entries.filter((entry) => entry.kind === 'action')) {
    const action = ACTIONS.find((action) => action.id === entry.source_id)
    if (action.group !== 'clipboard') continue
    for (const state of entry.states) {
      const probe = resolveProbe(entry, state)
      const copy = probe.setup.steps.findIndex((step) => step.kind === 'copy-selection')
      if (action.op !== 'pasteClip' || !probe.setup.context.session?.clipboard || !probe.setup.context.session?.engineParsed) {
        assert.equal(copy, -1, `${entry.id}/${state}`)
      } else {
        assert.ok(copy > 0)
        assert.deepEqual(probe.setup.steps[copy - 1], { kind: 'select-entity', type: 'LINE', editable: true })
        assert.equal(probe.setup.steps[copy + 1].kind, 'clear-selection')
        for (const kind of ['hold-engine-edit', 'crash-engine-worker', 'preview-version']) {
          const blocked = probe.setup.steps.findIndex((step) => step.kind === kind)
          if (blocked >= 0) assert.ok(copy < blocked)
        }
      }
    }
  }
})

test('version navigation seeds saved versions separately from engine edit history', () => {
  for (const id of ['undo', 'redo']) {
    const entry = map.entries.find((entry) => entry.id === `action:${id}`)
    const ready = resolveProbe(entry, 'ready')
    assert.ok(ready.setup.steps.some((step) => step.kind === 'saved-version-history' && step.redo === (id === 'redo')))
    assert.ok(ready.setup.steps.every((step) => !['create-line', 'undo-edit'].includes(step.kind)))
    const edit = structuredClone(entry)
    edit.expected_effect.ready.target = 'browser-edit-' + id
    assert.ok(resolveProbe(edit, 'ready').setup.steps.some((step) => step.kind === 'undo-edit'))
  }
})

test('the map omits the versionless state while its before-setup guard remains for every probe kind', () => {
  assert.ok(Object.isFrozen(UI_UNREACHABLE_STATES))
  assert.deepEqual([...UI_UNREACHABLE_STATES], ['no-versioned-drawing'])
  assert.equal(VERSIONLESS_DRAWING_REASON, 'The product creates a saved root version for every private drawing and renders the ribbon only with a drawing open, so no-versioned-drawing is unreachable through the UI.')
  const entries = map.entries.filter((entry) => entry.states.includes('no-versioned-drawing'))
  assert.deepEqual(entries, [])
  for (const kind of ['action', 'tool', 'control', 'tab', 'surface', 'drawer']) {
    // The declaration must not inspect recipes or query catalog/engine availability.
    assert.equal(unsupportedBeforeSetup({ kind, state: 'no-versioned-drawing' }), VERSIONLESS_DRAWING_REASON)
  }
  for (const id of ['action:history', 'action:undo', 'action:redo']) {
    assert.equal(unsupportedBeforeSetup(resolveProbe(map.entries.find((entry) => entry.id === id), 'ready')), null)
  }
})

for (const reset of ['policy reload', 'LINE command entry']) test(`catalog overflow is reopened after ${reset}`, async () => {
  let visible = false, expanded = false, clicks = 0
  const page = { getByRole: () => ({ getByRole: (role, options) => {
    if (role === 'group') {
      assert.equal(options.name, 'Solar design')
      assert.equal(options.includeHidden, true)
      return { count: async () => 1, isVisible: async () => visible }
    }
    assert.equal(options.name, 'More panels')
    return { isVisible: async () => true, getAttribute: async () => String(expanded),
      click: async () => { clicks++; visible = true; expanded = true } }
  } }) }
  await discloseControlPanel(page, { panelName: 'Solar design' })
  visible = false; expanded = false
  await discloseControlPanel(page, { panelName: 'Solar design' })
  assert.equal(clicks, 2)
  assert.equal(visible, true)
  const source = readFileSync(new URL('./fixtures.mjs', import.meta.url), 'utf8')
  const runner = source.slice(source.indexOf('export async function runProbe'))
  assert.ok(runner.indexOf('evidence.setupCompleted') < runner.indexOf('await discloseControlPanel'))
  assert.ok(runner.indexOf('await discloseControlPanel') < runner.indexOf('await expect(locator).toBeVisible()'))
})

test('engine recipes use live Draw panels and Author uses Manage', () => {
  for (const action of ACTIONS.filter((action) => action.id === 'author-tool'
    || ['createBlock', 'createInsert', 'createText', 'dimLinear', 'dimAligned'].includes(action.op))) {
    const entry = map.entries.find((entry) => entry.source_id === action.id && entry.kind === 'action')
    assert.ok(entry, action.id)
    for (const state of entry.states) {
      const tab = resolveProbe(entry, state).setup.steps.find((step) => ['ribbon-tab', 'failed-drawing-ribbon-tab'].includes(step.kind))
      assert.equal(tab.name, action.id === 'author-tool' ? 'Manage' : 'Draw')
    }
  }
})

test('a panel hidden again after state setup is disclosed once', async () => {
  let visible = true, expanded = false, clicks = 0
  const page = { getByRole: () => ({ getByRole: (role, options) => {
    if (role === 'group') {
      assert.equal(options.name, 'Block')
      assert.equal(options.includeHidden, true)
      return { count: async () => 1, isVisible: async () => visible }
    }
    assert.equal(options.name, 'More panels')
    return { isVisible: async () => true, getAttribute: async () => String(expanded),
      click: async () => { clicks++; expanded = true; visible = true } }
  } }) }
  // State setup leaves ribbon focus and closes its overflow.
  visible = false
  await discloseControlPanel(page, { group: 'block' })
  await discloseControlPanel(page, { group: 'block' })
  assert.equal(clicks, 1)
  assert.equal(visible, true)
})

test('WASM hold drains concurrent and late continuations before idempotent unroute', async () => {
  let handler, unroutes = 0, finish, late
  const calls = [0, 0, 0]
  const blocked = new Promise((resolve) => { finish = resolve })
  const page = {
    route: async (pattern, callback) => { assert.equal(pattern, '**/engine/engine_bg.wasm'); handler = callback },
    unroute: async (pattern, callback) => {
      assert.equal(callback, handler)
      assert.deepEqual(calls, [1, 1, 1])
      unroutes++
    },
  }
  const runtime = { page, evidence: {}, cleanup: [] }
  await setupStep({}, runtime, { kind: 'hold-engine-boot' })
  const [cleanup] = runtime.cleanup
  const first = handler({ continue: async () => { calls[0]++; await blocked } })
  const second = handler({ continue: async () => { calls[1]++; late = handler({ continue: async () => { calls[2]++ } }) } })
  assert.deepEqual(calls, [0, 0, 0])
  const closing = cleanup()
  assert.equal(cleanup(), closing)
  await Promise.resolve()
  assert.equal(unroutes, 0)
  finish()
  await closing
  await Promise.all([first, second, late])
  await cleanup()
  assert.deepEqual(calls, [1, 1, 1])
  assert.equal(unroutes, 1)
})

test('property selects retain identity and read only the enclosing widget reason', async () => {
  const probe = resolveProbe(map.entries.find((entry) => entry.id === 'action:modify-set-color'), 'engine-busy')
  let parentReason = probe.assertion.reason, disabledChecks = 0, nameChecks = 0, titleChecks = 0
  const parent = { title: () => parentReason }
  const select = { locator: (selector) => { assert.match(selector, /ancestor::label.*ribbon-widget/); return parent } }
  const assertions = (target) => ({
    toBeDisabled: async () => { assert.equal(target, select); disabledChecks++ },
    toHaveAccessibleName: async (pattern) => { assert.equal(target, select); assert.match(probe.locator.name, pattern); nameChecks++ },
    toHaveAttribute: async (name, pattern) => { assert.equal(target, parent); assert.equal(name, 'title'); titleChecks++; assert.match(target.title(), pattern) },
  })
  await assertEffect(probe, { page: {} }, select, {}, assertions)
  assert.deepEqual([disabledChecks, nameChecks, titleChecks], [1, 1, 1])
  parentReason = 'a different reason'
  await assert.rejects(assertEffect(probe, { page: {} }, select, {}, assertions), /did not match/)
})

test('clipboard and undo setup use the registry and rendered quick names', async () => {
  const names = []
  const page = { getByRole: (role, options) => {
    names.push(options.name)
    if (role === 'group') return { count: async () => 1, isVisible: async () => true, getByRole: page.getByRole }
    return { getByRole: page.getByRole, click: async () => {} }
  } }
  const runtime = { page, evidence: {} }
  await setupStep({}, runtime, { kind: 'copy-selection' })
  await setupStep({}, runtime, { kind: 'undo-edit' })
  assert.ok(names.includes(ACTIONS.find((action) => action.op === 'copyClip').label))
  assert.ok(names.includes('copy-clip'))
  assert.ok(names.includes('Undo edit'))
  assert.ok(!names.includes('Copy') && !names.includes('Undo'))
})
test('catalog-tool evidence stays compact and hashes the catalog once per worker', async () => {
  const record = { name: 'count-by-layer', description: 'Count entities by layer',
    availability: { entitled: true, implemented: true }, input_schema: { type: 'object', properties: {} } }
  const catalog = { families: [
    { name: 'drawing', capabilities: [record] },
    { name: 'other', capabilities: Array.from({ length: 200 }, (_, index) => ({
      name: `other-tool-${index}`, description: 'A catalog capability. '.repeat(30),
      input_schema: { type: 'object', properties: { layer: { type: 'string' } } },
    })) },
  ] }
  const serialized = JSON.stringify(catalog)
  assert.ok(Buffer.byteLength(serialized) >= 100 * 1024)
  const digest = createHash('sha256').update(serialized).digest('hex')
  let serializations = 0, requests = 0
  Object.defineProperty(catalog, 'toJSON', { value() { serializations++; return { families: this.families } } })
  const tabs = []
  const page = {
    locator: () => ({ first: () => ({ getAttribute: async () => null }) }),
    request: { get: async (path) => {
      assert.equal(path, '/api/capabilities')
      requests++
      return { ok: () => true, json: async () => catalog }
    } },
    getByRole: (role, options) => {
      if (role === 'tablist') {
        assert.equal(options.name, 'Ribbon')
        return { getByRole: (role, options) => {
          assert.equal(role, 'tab')
          return { click: async () => { tabs.push(options.name) } }
        } }
      }
      if (role === 'toolbar') return { getByRole: () => ({ filter: () => ({ isVisible: async () => false }) }) }
      assert.equal(role, 'button')
      assert.equal(options.name, 'More panels')
      return { isVisible: async () => false }
    },
  }
  const probe = { featureId: 'tool:count-by-layer', state: 'ready', certify: 'local' }
  const workerFacts = {}
  const run = async (name, facts = workerFacts) => {
    const runtime = { page, workerFacts: facts, evidence: {}, testInfo: { attach: async () => {} } }
    const pending = setupStep(probe, runtime, { kind: 'catalog-tool', name })
    if (name === 'missing-tool') await assert.rejects(pending, UnsupportedLocalError)
    else await pending
    return runtime.evidence
  }
  for (const name of ['count-by-layer', 'other-tool-0', 'missing-tool']) {
    const evidence = await run(name)
    const expected = catalog.families.flatMap((family) => family.capabilities).find((tool) => tool.name === name) || null
    assert.deepEqual(evidence.catalog, { requestedTool: name, record: expected,
      catalog_sha256: digest, families: 2, capabilities: 201 })
    assert.equal(evidence.catalog.record, expected)
    assert.equal(Object.hasOwn(evidence.catalog, 'response'), false)
    assert.ok(Buffer.byteLength(JSON.stringify(evidence, null, 2)) < 16 * 1024)
    if (!expected) assert.equal(evidence.result.result, 'unsupported_local')
  }
  assert.deepEqual(tabs, ['Manage', 'Manage'])
  assert.equal(requests, 1)
  assert.equal(serializations, 1)
  await run('count-by-layer', {})
  assert.equal(requests, 2)
  assert.equal(serializations, 2)
})

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
test('disabled reason mismatches reach the oracle while missing actions fail before it', async () => {
  const runner = new AsyncFunction('probe', 'runtime', 'test', 'setupStep', 'UnsupportedLocalError',
    'control', 'expect', 'collectProbeUxEvidence', 'captureBefore', 'activate', 'assertEffect',
    functionBody('export async function runProbe(probe, runtime) {', '\n// This reporter'))
  const probe = resolveProbe(map.entries.find((entry) => entry.id === 'action:solar-panels-array-rect'), 'no-drawing')
  const observed = 'Panel array (unavailable: select an entity in the drawing)'
  for (const present of [true, false]) {
    const runtime = { page: {}, evidence: { steps: [] }, testInfo: { project: { name: 'desktop' }, annotations: [] } }
    const locator = { name: observed }
    const assertions = (value) => ({
      toBeVisible: async () => assert.ok(present, 'Missing action during setup'),
      toBeDisabled: async () => assert.equal(value, locator),
      toHaveAccessibleName: async (pattern) => assert.match(value.name, pattern),
      toHaveAttribute: async () => assert.fail('A reason mismatch must fail at the accessible name'),
    })
    const steps = []
    await assert.rejects(runner(probe, runtime,
      { step: async (name, callback) => { steps.push(name); return callback() } },
      async () => {}, UnsupportedLocalError,
      (_, recipe) => {
        assert.equal(recipe.exact, false)
        assert.ok(recipe.name.test(observed))
        assert.ok(recipe.name.test('Panel array'))
        assert.ok(!recipe.name.test('Other Panel array'))
        return locator
      }, assertions, async () => [], new AsyncFunction('probe', 'runtime',
        functionBody('async function captureBefore(probe, runtime) {', '\nasync function activate')),
      async () => assert.fail('Disabled controls cannot activate'),
      (probe, runtime, locator, before) => assertEffect(probe, runtime, locator, before, assertions)),
    (error) => {
      assert.ok(error instanceof assert.AssertionError)
      if (present) {
        assert.match(error.message, /select an entity in the drawing/)
        assert.match(error.message, /no drawing in the browser engine yet/)
      } else assert.match(error.message, /Missing action during setup/)
      return true
    })
    assert.equal(runtime.evidence.oracleReached, present ? probe.assertion.assertionId : undefined)
    assert.equal(steps.includes(probe.assertion.assertionId), present)
    assert.equal(runtime.evidence.cleanupCompleted, true)
    assert.ok(runtime.evidence.failure)
  }
})
test('failed drawing refusal mismatches preserve expected and observed text in the oracle', async () => {
  const probe = resolveProbe(map.entries.find((entry) => entry.id === 'action:solar-panels-array-rect'), 'no-drawing')
  const observed = 'Panel array (unavailable: select an entity in the drawing)'
  const locator = { getAttribute: async (name) => { assert.equal(name, 'aria-label'); return observed } }
  const runtime = { page: {}, failedDrawing: true, evidence: { failedDrawing: {}, oracleReached: probe.assertion.assertionId } }
  const assertions = (target) => ({
    toBeDisabled: async () => assert.equal(target, locator),
    toHaveAccessibleName: async (pattern) => assert.match(observed, pattern),
    toHaveAttribute: async () => assert.fail('Reason mismatch must fail before tooltip checks'),
  })
  await assert.rejects(assertEffect(probe, runtime, locator, {}, assertions), (error) => {
    assert.match(error.message, /select an entity in the drawing/)
    assert.match(error.message, /no drawing in the browser engine yet/)
    return error instanceof assert.AssertionError
  })
  assert.equal(runtime.evidence.oracleReached, probe.assertion.assertionId)
  assert.deepEqual(runtime.evidence.disabledReason, { expected: probe.locator.disabledVariants.map((variant) => variant.name), observed })
})
test('the runner ignores worker-global Solar refusal and fetches readiness after every setup step', async () => {
  const runner = new AsyncFunction('probe', 'runtime', 'test', 'setupStep', 'UnsupportedLocalError',
    'workerCatalog', 'unsupportedBeforeSetup', 'toolAvailabilityEvidence', 'unsupported',
    functionBody('export async function runProbe(probe, runtime) {', '\n// This reporter'))
  const calls = []
  const probe = { featureId: 'tool:solar-autofill', sourceId: 'solar-autofill', kind: 'tool', state: 'ready',
    setup: { steps: [{ kind: 'open-private-drawing' }, { kind: 'seed-solar-graph' }] },
    assertion: { kind: 'opens', target: 'catalog-run-decision', assertionId: 'autofill/oracle' } }
  const runtime = { page: { request: {} }, evidence: { steps: [] }, workerFacts: { catalog: { families: [{
    capabilities: [{ name: 'solar-autofill', availability: { input_ready: false, refusal_reasons: ['drawing_context_required'] } }],
  }] } }, testInfo: { annotations: [] } }
  const result = await runner(probe, runtime, { step: async (name, callback) => { calls.push(name); return callback() } },
    async (_, current, recipe) => {
      if (recipe.kind === 'open-private-drawing') current.drawingId = 'own-drawing'
      else current.drawingVersion = 2
    }, UnsupportedLocalError,
    async (facts, request, scope) => {
      assert.equal(request, runtime.page.request)
      assert.deepEqual(scope, { drawingId: 'own-drawing', version: 2 })
      assert.notEqual(facts, runtime.workerFacts)
      calls.push('scoped-readiness')
      facts.catalog = { families: [{ capabilities: [{ name: 'solar-autofill', availability: {
        entitled: true, engine_ready: true, implemented: true, input_ready: false, refusal_reasons: ['frames_required'],
      } }] }] }
    }, unsupportedBeforeSetup, toolAvailabilityEvidence,
    async (probe, current, reason) => { current.evidence.result = { reason }; throw new UnsupportedLocalError(probe, reason) })
  assert.deepEqual(calls, ['Setup: open-private-drawing', 'Setup: seed-solar-graph', 'scoped-readiness'])
  assert.equal(result.unsupported, true)
  assert.match(result.reason, /frames_required/)
  assert.doesNotMatch(result.reason, /drawing_context_required/)
  assert.deepEqual(runtime.unsupportedAvailability.refusal_codes, ['frames_required'])
  assert.ok(runtime.evidence.setupCompleted)
  assert.equal(runtime.evidence.cleanupCompleted, true)
})
const evidenceFixture = new AsyncFunction('use', 'testInfo', 'workerFacts', fixtureSource.slice(
  fixtureSource.indexOf('walkEvidence: [async ({ workerFacts }, use, testInfo) => {')
    + 'walkEvidence: [async ({ workerFacts }, use, testInfo) => {'.length,
  fixtureSource.indexOf('}, { auto: true }]')))

test('a failing WASM continuation never masks the original assertion and all cleanup runs', async () => {
  const original = Object.assign(new Error('original assertion'), { matcherResult: {} })
  let handler, continued = 0, unrouted = 0, otherCleanup = 0
  const page = {
    route: async (_pattern, callback) => { handler = callback },
    unroute: async () => { unrouted++ },
  }
  const runtime = { page, evidence: { steps: [] }, testInfo: { annotations: [] } }
  const probe = { setup: { steps: [{ kind: 'hold-engine-boot' }] }, assertion: { assertionId: 'unit/oracle' } }
  await assert.rejects(probeRunner(probe, runtime, { step: async (_name, fn) => fn() }, async (_probe, current) => {
    current.cleanup.push(async () => { otherCleanup++ })
    const cleanup = await holdJobRoutes(page, '**/engine/engine_bg.wasm')
    current.cleanup.push(cleanup)
    handler({ continue: async () => { continued++; throw new Error('continuation failed') } }).catch(() => {})
    throw original
  }, UnsupportedLocalError), (error) => error === original)
  await assert.rejects(runtime.cleanup[0](), /continuation failed/)
  assert.equal(continued, 1)
  assert.equal(unrouted, 1)
  assert.equal(otherCleanup, 1)
  assert.equal(runtime.evidence.failure.message, original.message)
  assert.deepEqual(runtime.evidence.cleanupErrors, [{ message: 'continuation failed' }])
})

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
          if (!['bar', 'slash'].includes(action.surface) && action.id !== 'engine:repeat') {
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

test('Jobs uses desktop spine controls and preserves the phone drawer locator', () => {
  const entry = map.entries.find((entry) => entry.id === 'drawer:jobs')
  for (const state of ['closed', 'open']) {
    const probe = resolveProbe(entry, state)
    if (state === 'closed') {
      assert.match('Expand the job monitor (12 live)', probe.locator.name)
      assert.doesNotMatch('Expand the job monitor (unknown live)', probe.locator.name)
    } else assert.equal(probe.locator.name, 'Collapse the job monitor to a spine')
    assert.equal(probe.locator.scope, undefined)
    assert.equal(probe.locator.phone.name, 'Jobs')
    assert.equal(probe.locator.phone.scope.name, 'Workspace panels')
    assert.deepEqual(probe.setup.steps.at(-1), { kind: 'job-rail-state', name: 'Jobs', open: state === 'open' })
  }
})

test('Explode selects a known multi-segment polyline only in its ready recipe', () => {
  const entry = map.entries.find((entry) => entry.id === 'action:modify-explode')
  assert.equal(resolveProbe(entry, 'ready').setup.steps.find((step) => step.kind === 'select-entity').type, 'LWPOLYLINE')
  const refusal = resolveProbe(entry, 'placed-dimension')
  assert.equal(refusal.setup.steps.find((step) => step.kind === 'select-entity').type, 'DIMENSION')
  // Since #1848 the product disables these with a reason on a placed DIMENSION.
  assert.equal(refusal.assertion.kind, 'disabled_with_reason')
  const dxf = injectWalkEntities('0\nENDSEC\n0\nEOF')
  assert.match(dxf, /LWPOLYLINE\n5\nA200/)
  assert.deepEqual(FIXTURE_PICK_POINTS.LWPOLYLINE, [[160, 200]])
  for (const id of ['action:clipboard-copy-clip', 'action:clipboard-cut-clip']) {
    assert.equal(resolveProbe(map.entries.find((entry) => entry.id === id), 'placed-dimension').assertion.kind, 'disabled_with_reason')
  }
})

test('zoom-out establishes an unclipped baseline without changing the Fit recipe', () => {
  const probe = resolveProbe(map.entries.find((entry) => entry.id === 'action:zoom-out'), 'ready')
  assert.equal(probe.setup.steps.at(-1).kind, 'zoom-inside-extents')
  const fit = resolveProbe(map.entries.find((entry) => entry.id === 'action:fit'), 'ready')
  assert.equal(fit.setup.steps.at(-1).kind, 'zoom-before-fit')
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
      kind: 'open-failed-drawing', url: `/app?surface=${ACTIONS.find((action) => action.id === entry.source_id).panel === 'solar-panels' ? 'solar' : 'cad'}&drawing=missing.invalid`,
    })
    if (['engine:undo', 'engine:redo', 'engine:repeat'].includes(entry.source_id)) {
      assert.equal(probe.setup.steps.length, 1)
      assert.equal(probe.locator.group, undefined)
      if (entry.source_id === 'engine:repeat') assert.equal(probe.locator.keyboardAction, 'engine:repeat')
      else assert.equal(probe.locator.scope.name, 'Quick access')
      continue
    }
    assert.equal(probe.setup.steps.length, ACTIONS.find((action) => action.id === entry.source_id).panel === 'solar-panels' ? 3 : 2)
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

test('engine drafting modes establish real pressed states and model tabs remain refusal probes', () => {
  for (const id of ['object-snap', 'ortho-mode']) {
    const entry = map.entries.find((row) => row.id === 'control:' + id)
    for (const state of entry.states) {
      const probe = resolveProbe(entry, state)
      const setup = probe.setup.steps.at(-1)
      assert.equal(setup.kind, 'engine-mode-state')
      assert.equal(setup.mode, id === 'object-snap' ? 'osnap' : 'ortho')
      assert.equal(setup.pressed, entry.state_contexts[state].pressed)
      assert.deepEqual(setup.control, probe.locator)
      assert.equal(probe.assertion.value, !setup.pressed)
      assert.equal(probe.certification, null)
    }
  }
  for (const profile of ['drafting', 'solar']) {
    const probe = resolveProbe(map.entries.find((row) => row.id === 'tab:' + profile + ':model'), 'unavailable')
    assert.equal(probe.certification, null)
    assert.equal(probe.assertion.kind, 'disabled_with_reason')
    assert.equal(probe.assertion.reason, '3D modelling is not in this engine yet')
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
