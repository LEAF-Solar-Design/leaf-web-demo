import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { buildFeatureMap } from './featureMap.mjs'
import { readControlInventory, validateControlInventory, resolveCensus, censusFailure, controlKey, controlNameAttributes, registryCensusMappings } from './controlInventory.mjs'
import { enumerateControls } from '../e2e/walk/controlCensus.mjs'
import { PROFILE_RIBBON_TABS } from '../src/lib/ribbonTabs.data.js'
import { CONTROL_CENSUS_BATCH, requireControlCensusBatch, locatorRecipe, normalizedControlKey } from '../e2e/walk/probes.mjs'

const fixture = (name) => JSON.parse(readFileSync(new URL(`./fixtures/control-census.${name}.json`, import.meta.url), 'utf8'))
const resolve = (data, context) => resolveCensus(data.controls, data.derivedMappings, data.map, data.inventory, context)
test('batch three retires exactly seventeen baseline keys and retains the precise Retry gap', () => {
  const map = buildFeatureMap()
  const inventory = readControlInventory(map)
  const previous = [
  {
    "scope": "document",
    "role": "button",
    "name": "Add: build a new capability",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "Back to the demo",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "Claude accounts not linked",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "Close the drawing view and return to Start",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "Collapse the notification inbox",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "Details",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "History",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "Linked services {n} linked",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "Open the project board",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "Retry",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "Run",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "scope ▾",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "Sign out",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "Start",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "Take edit lock",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "button",
    "name": "What Leaf costs to operate",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "combobox",
    "name": "Command bar",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "scope": "document",
    "role": "combobox",
    "name": "Find in drawing",
    "reason": "No feature-map coverage yet: no walk mapping",
    "states": [
      "ready"
    ],
    "viewports": [
      "desktop"
    ]
  }
]
  const additions = [
  {
    "feature_id": "control:scope-add",
    "scope": "document",
    "role": "button",
    "name": "Add: build a new capability",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:demo-return",
    "scope": "document",
    "role": "button",
    "name": "Back to the demo",
    "states": [
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:claude-accounts",
    "scope": "document",
    "role": "button",
    "name": "Claude accounts not linked",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:drawing-close-start",
    "scope": "document",
    "role": "button",
    "name": "Close the drawing view and return to Start",
    "states": [
      "ready"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:notification-collapse",
    "scope": "document",
    "role": "button",
    "name": "Collapse the notification inbox",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:session-details",
    "scope": "document",
    "role": "button",
    "name": "Details",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:version-history",
    "scope": "document",
    "role": "button",
    "name": "History",
    "states": [
      "ready"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:linked-services",
    "scope": "document",
    "role": "button",
    "name": "Linked services {n} linked",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:project-board",
    "scope": "document",
    "role": "button",
    "name": "Open the project board",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:prompt-run",
    "scope": "document",
    "role": "button",
    "name": "Run",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:prompt-scope",
    "scope": "document",
    "role": "button",
    "name": "scope ▾",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:sign-out",
    "scope": "document",
    "role": "button",
    "name": "Sign out",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:start-board",
    "scope": "document",
    "role": "button",
    "name": "Start",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:take-edit-lock",
    "scope": "document",
    "role": "button",
    "name": "Take edit lock",
    "states": [
      "ready"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:cost-panel",
    "scope": "document",
    "role": "button",
    "name": "What Leaf costs to operate",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:command-bar",
    "scope": "document",
    "role": "combobox",
    "name": "Command bar",
    "states": [
      "ready",
      "failed-load"
    ],
    "viewports": [
      "desktop"
    ]
  },
  {
    "feature_id": "control:find-drawing",
    "scope": "document",
    "role": "combobox",
    "name": "Find in drawing",
    "states": [
      "ready"
    ],
    "viewports": [
      "desktop"
    ]
  }
]
  assert.equal(previous.length, 18)
  assert.equal(additions.length, 17)
  assert.deepEqual(inventory.mappings.slice(-17), additions)
  assert.deepEqual(CONTROL_CENSUS_BATCH.slice(-17), additions)
  assert.deepEqual(inventory.baseline_unmapped, [{"scope":"document","role":"button","name":"Retry","reason":"Retry remains unmapped: the failed-load recipe uses a permanent missing drawing; bare Retry has multiple owners, and no checked public recovery recipe establishes a semantic successful retry for the observed owner.","states":["failed-load"],"viewports":["desktop"]}])
  assert.deepEqual(previous.filter((row) => !inventory.baseline_unmapped.some((other) => controlKey(other) === controlKey(row)))
    .map(controlKey).sort(), additions.map(controlKey).sort())
  for (const row of additions) {
    const original = previous.find((old) => controlKey(old) === controlKey(row))
    const { reason, ...identity } = original
    const { feature_id, ...mapped } = row
    assert.deepEqual(mapped, identity)
  }
})

test('batch three count changes preserve raw linked-service names and reject duplicate observations', () => {
  const map = buildFeatureMap()
  const inventory = { ...readControlInventory(map), baseline_unmapped: [] }
  for (const state of ['ready', 'failed-load']) {
    for (const count of ['0', '7', '1234']) {
      const rows = CONTROL_CENSUS_BATCH.filter((row) => row.states.includes(state))
        .map((row, index) => ({ ...row, index, visible: true,
          name: row.feature_id === 'control:linked-services' ? 'Linked services ' + count + ' linked' : row.name }))
      const result = resolveCensus(rows, [], map, inventory, { state })
      assert.equal(requireControlCensusBatch(result, { state }), true)
      assert.equal(result.resolved.find((row) => row.feature_id === 'control:linked-services').raw_name, 'Linked services ' + count + ' linked')
      for (const row of rows.filter((row) => inventory.mappings.slice(-17).some((mapped) => mapped.feature_id === row.feature_id))) {
        const duplicate = resolveCensus([...rows, { ...row, index: rows.length }], [], map, inventory, { state })
        assert.throws(() => requireControlCensusBatch(duplicate, { state }), /missing or misresolved/)
      }
    }
  }
})

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

test('batch two retains its twelve exact mappings after batch three retires seventeen more rows', () => {
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
  const retiredIndices = [0, 1, 2, 3, 4, 5, 6, 11, 14, 27, 28, 29]
  const retiredIds = ['properties-close', 'properties-drawing', 'properties-layers', 'properties-panels',
    'properties-plan', 'properties-selection', 'properties-walk', 'drawing-overview-collapse',
    'drawing-overview', 'layer-panels', 'layer-walk', 'job-monitor-expand']
  assert.equal(expected.length, 30, 'retain the independent batch-one reference')
  const batchTwoRemainder = expected.filter((row, index) => !retiredIndices.includes(index))
  assert.equal(batchTwoRemainder.length, 18)
  assert.deepEqual([...inventory.mappings.slice(-17), ...inventory.baseline_unmapped].map(controlKey).sort(), batchTwoRemainder.map(controlKey).sort())
  const retired = retiredIndices.map((index, position) => {
    const { reason, ...row } = expected[index]
    return { feature_id: 'control:' + retiredIds[position], ...row }
  })
  assert.deepEqual(inventory.mappings.slice(11, 23), retired)
  assert.deepEqual(CONTROL_CENSUS_BATCH.slice(10, 22), retired)
  assert.deepEqual(expected.filter((row) => !batchTwoRemainder.some((retained) => controlKey(retained) === controlKey(row)))
    .map(controlKey), retired.map(controlKey))
  const mappings = inventory.mappings.filter((row) => row.feature_id.startsWith('control:'))
  assert.deepEqual(mappings, CONTROL_CENSUS_BATCH)
  assert.equal(mappings.length, 39)
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

test('count-bearing batch controls resolve different live counts through anchored scoped recipes', () => {
  const map = buildFeatureMap()
  const inventory = { ...readControlInventory(map), baseline_unmapped: [] }
  for (const count of ['1', '42', '1,234']) {
    const expected = CONTROL_CENSUS_BATCH.filter((row) => row.states.includes('ready'))
    const controls = expected.map((row, index) => ({ ...row, index, visible: true,
      name: row.name.replace('{n}', ['control:job-monitor-expand', 'control:linked-services'].includes(row.feature_id) ? count.replace(/,/g, '') : count) }))
    const derived = []
    for (const row of controls.filter((row) => row.name !== expected[row.index].name)) {
      const entry = map.entries.find((entry) => entry.id === row.feature_id)
      const recipe = locatorRecipe(entry, entry.states[0])
      assert.match(row.name, recipe.name)
      assert.equal(normalizedControlKey(row), normalizedControlKey(expected[row.index]))
      derived.push({ index: row.index, feature_id: entry.id })
    }
    const result = resolveCensus(controls, derived, map, inventory)
    assert.equal(result.ok, true)
    assert.equal(requireControlCensusBatch(result), true)
    assert.deepEqual(result.resolved.map((row) => row.feature_id), expected.map((row) => row.feature_id))
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
