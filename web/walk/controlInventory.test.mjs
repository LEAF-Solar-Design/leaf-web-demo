import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { buildFeatureMap } from './featureMap.mjs'
import { readControlInventory, validateControlInventory, resolveCensus, censusFailure, controlKey, controlNameAttributes, registryCensusMappings } from './controlInventory.mjs'
import { enumerateControls } from '../e2e/walk/controlCensus.mjs'
import { PROFILE_RIBBON_TABS } from '../src/lib/ribbonTabs.data.js'
import { CONTROL_CENSUS_BATCH, requireControlCensusBatch } from '../e2e/walk/probes.mjs'

const fixture = (name) => JSON.parse(readFileSync(new URL(`./fixtures/control-census.${name}.json`, import.meta.url), 'utf8'))
const resolve = (data, context) => resolveCensus(data.controls, data.derivedMappings, data.map, data.inventory, context)

test('live counts and disabled reasons share a stable baseline identity while raw evidence survives', () => {
  for (const { name, changed, key } of fixture('good').normalization_cases) {
    const data = fixture('good')
    data.controls[2].name = name
    data.inventory.baseline_unmapped[0].name = key
    assert.equal(resolve(data).ok, true, 'original observation matches normalized baseline')
    data.controls[2].name = changed
    const result = resolve(data)
    assert.equal(result.ok, true, 'changed data must not add an unmapped or stale row')
    assert.equal(result.baselined[0].name_key, key)
    assert.equal(result.baselined[0].raw_name, changed)
    assert.equal(result.resolved[1].feature_id, 'action:fit')
    assert.equal(controlKey({ ...data.controls[2], name }), controlKey(data.controls[2]))
    data.controls[2].name = `Different ${changed}`
    assert.equal(resolve(data).unmapped.length, 1, 'meaningful label changes still fail')
    assert.equal(resolve(data).stale.length, 1)
  }
  assert.deepEqual(controlNameAttributes('Redo version (unavailable: no versioned drawing)'), {
    raw_name: 'Redo version (unavailable: no versioned drawing)', name_key: 'Redo version', disabled_reason: 'no versioned drawing',
  })
  assert.equal(controlNameAttributes('New drawing (unavailable: start on board (Start tab))').disabled_reason, 'start on board (Start tab)')
})

test('normalized duplicate inventory identities are refused', () => {
  for (const [name, other] of fixture('bad').normalization_collisions) {
    const data = fixture('good')
    data.inventory.baseline_unmapped[0].name = name
    data.inventory.baseline_unmapped.push({ ...data.inventory.baseline_unmapped[0], name: other })
    assert.throws(() => validateControlInventory(data.inventory, data.map), /duplicate row/)
  }
})

test('active-profile Ribbon tabs and registered shell actions resolve without a baseline', () => {
  const map = buildFeatureMap()
  const inventory = { version: 1, mappings: [], baseline_unmapped: [] }
  for (const profile of ['drafting', 'solar', 'project', 'ship']) {
    const tabs = PROFILE_RIBBON_TABS[profile].map((tab, index) => ({ index, scope: 'tablist:"Ribbon"', role: 'tab',
      name: tab.reason ? `${tab.label} (unavailable: ${tab.reason})` : tab.label, disabled: !!tab.reason, visible: true }))
    const result = resolveCensus(tabs, registryCensusMappings(tabs, map, { profile }), map, inventory)
    assert.equal(result.ok, true, profile)
    assert.deepEqual(result.resolved.map((row) => row.feature_id), PROFILE_RIBBON_TABS[profile].map((tab) => `tab:${profile}:${tab.id}`))
  }
  const controls = [
    ['View', 'Fit drawing to view', 'action:fit'], ['View', 'Zoom in', 'action:zoom-in'], ['View', 'Zoom out', 'action:zoom-out'],
    ['Quick access', 'Undo version (unavailable: nothing to undo)', 'action:undo'],
    ['Quick access', 'Redo version (unavailable: no versioned drawing)', 'action:redo'],
    ['Quick access', 'Tool rail', 'action:rail-expand'],
  ].map(([toolbar, name, expected], index) => ({ index, scope: `toolbar:${JSON.stringify(toolbar)}`, role: 'button', name,
    disabled: name.includes('(unavailable:'), visible: true, expected }))
  const result = resolveCensus(controls, registryCensusMappings(controls, map), map, inventory)
  assert.equal(result.ok, true)
  assert.deepEqual(result.resolved.map((row) => row.feature_id), controls.map((row) => row.expected))
  const unregistered = ['Back to the previous view', 'Up one level', 'New drawing', 'Print'].map((name, index) => ({
    index: controls.length + index, scope: index < 2 ? 'toolbar:"View"' : 'toolbar:"Quick access"', role: 'button', name, visible: true,
  }))
  const all = [...controls, ...unregistered, { ...controls[0], index: 100, scope: 'group:"Unrelated"' }]
  const failed = resolveCensus(all, registryCensusMappings(all, map), map, inventory)
  assert.equal(failed.ok, false)
  assert.equal(failed.resolved.length, controls.length, 'registered positive controls survive')
  assert.equal(failed.unmapped.length, 5)
})

test('enumeration excludes containers even with tabindex and retains their scope for real controls', async (t) => {
  const originalStyle = Object.getOwnPropertyDescriptor(globalThis, 'getComputedStyle')
  globalThis.getComputedStyle = () => ({ display: 'block', visibility: 'visible' })
  t.after(() => {
    if (originalStyle) Object.defineProperty(globalThis, 'getComputedStyle', originalStyle)
    else delete globalThis.getComputedStyle
  })
  const node = (role, name, parentElement = null) => ({
    tagName: 'DIV', parentElement, tabIndex: 0, textContent: name,
    getAttribute: (key) => ({ role, 'aria-label': name, tabindex: '0' })[key] ?? null,
    hasAttribute: (key) => ['role', 'aria-label', 'tabindex'].includes(key),
    getClientRects: () => [{}], matches: () => false,
  })
  const elements = ['toolbar', 'tablist', 'group', 'navigation', 'region', 'menu', 'radiogroup'].map((role) => node(role, role))
  const button = node('button', 'Redo version (unavailable: nothing to redo)', elements[0])
  elements.push(button)
  const page = { locator: () => ({ evaluateAll: async (fn, arg) => fn(elements, arg),
    nth: (index) => ({ ariaSnapshot: async () => `- ${elements[index].getAttribute('role')} ${JSON.stringify(elements[index].textContent)}` }),
  }) }
  const controls = await enumerateControls(page)
  assert.equal(controls.length, 1)
  assert.equal(controls[0].role, 'button', 'interactive positive control survives')
  assert.equal(controls[0].scope, 'toolbar:"toolbar"')
  assert.equal(controls[0].disabled, true)
  assert.equal(controls[0].disabled_reason, 'nothing to redo')
  assert.equal(controls[0].name_key, 'Redo version')
})

test('registry mappings and explicit scoped mappings resolve; reasoned baseline covers the remaining control', () => {
  const result = resolve(fixture('good'))
  assert.equal(result.ok, true)
  assert.equal(result.total, 3)
  assert.deepEqual(result.resolved.map((row) => row.feature_id), ['drawer:nav', 'action:fit'])
  assert.equal(result.resolved[1].disabled, true)
  assert.equal(result.baselined[0].name, 'Start')
  assert.match(result.baselined[0].reason, /Shell control/)
  assert.equal(readControlInventory(buildFeatureMap()).version, 1)
})

test('new unmapped and stale baseline rows fail together, with a surviving mapped positive control', () => {
  const result = resolve(fixture('bad'))
  assert.equal(result.ok, false)
  assert.equal(result.resolved[0].feature_id, 'action:fit')
  assert.deepEqual(result.unmapped.map((row) => row.name), ['New control'])
  assert.deepEqual(result.stale.map((row) => row.name), ['Removed control'])
  const failure = censusFailure(result)
  assert.match(failure, /unmapped: scope="document" role="button" name="New control"/)
  assert.match(failure, /stale baseline: scope="document" role="button" name="Removed control"/)
})

test('ratchet detects deletion, hiding, relocation, role changes and newly covered baseline rows', () => {
  for (const mutate of [
    (data) => { data.controls.pop() },
    (data) => { data.controls[2].visible = false },
    (data) => { data.controls[2].scope = 'group:"Elsewhere"' },
    (data) => { data.controls[2].role = 'link' },
    (data) => { data.derivedMappings.push({ index: 2, feature_id: 'drawer:nav' }) },
  ]) {
    const data = fixture('good')
    assert.equal(resolve(data).ok, true, 'unchanged positive control')
    mutate(data)
    const result = resolve(data)
    assert.equal(result.ok, false)
    assert.equal(result.stale[0].name, 'Start')
    assert.ok(result.resolved.some((row) => row.feature_id === 'action:fit'))
  }
})

test('baseline is exact and state-specific; a disabled new control still fails', () => {
  const data = fixture('good')
  data.controls[2].name = 'Start another task'
  data.controls[2].disabled = true
  assert.equal(resolve(data).unmapped.length, 1)
  const failed = fixture('good')
  failed.controls.pop()
  assert.equal(resolve(failed, { state: 'failed-load' }).ok, true)
  assert.equal(resolve(failed, { state: 'ready' }).ok, false)
  failed.controls.push({ index: 3, scope: 'document', role: 'button', name: 'Start', visible: true, disabled: false })
  assert.equal(resolve(failed, { state: 'failed-load' }).ok, false)
})

test('ambiguous registry labels cannot silently choose a feature; explicit mapping disambiguates', () => {
  const data = fixture('good')
  data.derivedMappings.push({ index: 0, feature_id: 'action:fit' })
  assert.equal(resolve(data).unmapped[0].problem, 'ambiguous registry mapping')
  data.inventory.mappings.push({ scope: 'document', role: 'button', name: 'Tool rail', feature_id: 'drawer:nav' })
  assert.equal(resolve(data).ok, true)
  data.derivedMappings.push({ index: 1, feature_id: 'unknown:id' })
  assert.throws(() => resolve(data), /unknown feature id/)
})

test('inventory refuses unknown ids, overlapping duplicate rows, empty reasons and malformed context', () => {
  const invalid = (mutate, pattern) => {
    const data = fixture('good')
    mutate(data.inventory)
    assert.throws(() => validateControlInventory(data.inventory, data.map), pattern)
  }
  invalid((inventory) => { inventory.mappings[0].feature_id = 'action:missing' }, /unknown feature id/)
  invalid((inventory) => { inventory.mappings.push({ ...inventory.mappings[0] }) }, /duplicate row/)
  invalid((inventory) => { inventory.baseline_unmapped.push({ ...inventory.baseline_unmapped[0] }) }, /duplicate row/)
  invalid((inventory) => { inventory.baseline_unmapped[0].reason = '  ' }, /non-empty reason/)
  invalid((inventory) => { delete inventory.baseline_unmapped[0].reason }, /non-empty reason/)
  invalid((inventory) => { inventory.baseline_unmapped[0].states = [] }, /unique non-empty/)
  invalid((inventory) => { inventory.baseline_unmapped[0].viewports = ['tablet'] }, /unknown viewport/)
  invalid((inventory) => { inventory.mappings[0].selector = '.shell' }, /unknown row field/)
  invalid((inventory) => { inventory.version = 2 }, /version 1/)
  const data = fixture('good')
  data.inventory.baseline_unmapped.push({ ...data.inventory.baseline_unmapped[0], states: ['failed-load'] })
  assert.doesNotThrow(() => validateControlInventory(data.inventory, data.map))
})

test('batch one retires exactly ten keys and preserves the other thirty baseline rows', () => {
  const map = buildFeatureMap()
  const inventory = readControlInventory(map)
  const expected = [
    ["complementary:\"Properties\"","button","Close the properties pane",["ready","failed-load"]],
    ["complementary:\"Properties\"","button","Drawing",["ready"]],
    ["complementary:\"Properties\"","button","Layers",["ready","failed-load"]],
    ["complementary:\"Properties\"","button","Panels {n}",["ready"]],
    ["complementary:\"Properties\"","button","Plan",["ready","failed-load"]],
    ["complementary:\"Properties\"","button","Selection",["ready","failed-load"]],
    ["complementary:\"Properties\"","button","Walk {n}",["ready"]],
    ["document","button","Add: build a new capability",["ready","failed-load"]],
    ["document","button","Back to the demo",["failed-load"]],
    ["document","button","Claude accounts not linked",["ready","failed-load"]],
    ["document","button","Close the drawing view and return to Start",["ready"]],
    ["document","button","Collapse drawing overview",["ready"]],
    ["document","button","Collapse the notification inbox",["ready","failed-load"]],
    ["document","button","Details",["ready","failed-load"]],
    ["document","button","Drawing overview",["ready"]],
    ["document","button","History",["ready"]],
    ["document","button","Linked services {n} linked",["ready","failed-load"]],
    ["document","button","Open the project board",["ready","failed-load"]],
    ["document","button","Retry",["failed-load"]],
    ["document","button","Run",["ready","failed-load"]],
    ["document","button","scope ▾",["ready","failed-load"]],
    ["document","button","Sign out",["ready","failed-load"]],
    ["document","button","Start",["ready","failed-load"]],
    ["document","button","Take edit lock",["ready"]],
    ["document","button","What Leaf costs to operate",["ready","failed-load"]],
    ["document","combobox","Command bar",["ready","failed-load"]],
    ["document","combobox","Find in drawing",["ready"]],
    ["toolbar:\"Drafting tools\" > group:\"Layers\"","button","Panels",["ready"]],
    ["toolbar:\"Drafting tools\" > group:\"Layers\"","button","Walk",["ready"]],
    ["toolbar:\"Job monitor\"","button","Expand the job monitor ({n} live)",["ready","failed-load"]],
  ].map(([scope, role, name, states]) => ({ scope, role, name, states, viewports: ['desktop'],
    reason: 'No feature-map coverage yet: no walk mapping' }))
  assert.deepEqual(inventory.baseline_unmapped, expected)
  assert.equal(inventory.baseline_unmapped.length, 30)
  const mappings = inventory.mappings.filter((row) => row.feature_id.startsWith('control:'))
  assert.deepEqual(mappings, CONTROL_CENSUS_BATCH)
  assert.equal(mappings.length, 10)
  for (const row of mappings) assert.ok(!inventory.baseline_unmapped.some((other) => controlKey(other) === controlKey(row)))
})

test('batch census mappings cover available and unavailable names in each declared context', () => {
  const map = buildFeatureMap()
  const inventory = { ...readControlInventory(map), baseline_unmapped: [] }
  for (const state of ['ready', 'failed-load']) {
    const expected = CONTROL_CENSUS_BATCH.filter((row) => row.states.includes(state))
    for (const unavailable of [false, true]) {
      const controls = expected.map((row, index) => ({ ...row, index, visible: true,
        name: unavailable ? row.name + ' (unavailable: example reason)' : row.name }))
      const result = resolveCensus(controls, [], map, inventory, { state, viewport: 'desktop' })
      assert.equal(result.ok, true)
      assert.equal(requireControlCensusBatch(result, { state, viewport: 'desktop' }), true)
      assert.deepEqual(result.resolved.map((row) => row.feature_id), expected.map((row) => row.feature_id))
    }
  }
})

test('batch obligations reject wrong scope, role, name, feature id, hiding and disappearance', () => {
  const map = buildFeatureMap()
  const inventory = { ...readControlInventory(map), baseline_unmapped: [] }
  for (const state of ['ready', 'failed-load']) {
    const expected = CONTROL_CENSUS_BATCH.filter((row) => row.states.includes(state))
    for (let index = 0; index < expected.length; index++) {
      for (const mutate of [
        (rows) => { rows[index].scope = 'toolbar:"Unrelated"' },
        (rows) => { rows[index].role = 'link' },
        (rows) => { rows[index].name += ' changed' },
        (rows) => { rows[index].visible = false },
        (rows) => { rows.splice(index, 1) },
      ]) {
        const controls = expected.map((row, index) => ({ ...row, index, visible: true }))
        mutate(controls)
        const result = resolveCensus(controls, [], map, inventory, { state })
        assert.throws(() => requireControlCensusBatch(result, { state }), /required batch control missing or misresolved/)
      }
      const controls = expected.map((row, index) => ({ ...row, index, visible: true }))
      const wrong = structuredClone(inventory)
      wrong.mappings.find((row) => row.feature_id === expected[index].feature_id).feature_id = 'action:fit'
      const result = resolveCensus(controls, [], map, wrong, { state })
      assert.throws(() => requireControlCensusBatch(result, { state }), /required batch control missing or misresolved/)
    }
  }
})
