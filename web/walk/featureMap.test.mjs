import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { ACTIONS, ESCAPE_RUNGS, RETRY_RUNGS, reasonCode, REASONS, DRAW_REASONS, MODIFY_REASONS, REPEAT_REASONS } from '../src/lib/actionRegistry.js'
import { PRODUCT_SURFACES } from '../src/site/productSurfaces.js'
import { PROFILE_RIBBON_TABS } from '../src/lib/ribbonTabs.data.js'
import { STUDIO_DRAWERS } from '../src/lib/studioDrawers.js'
import { PROMPTS } from '../src/cadedit/promptKeys.js'
import {
  buildFeatureMap, checkCompleteness, DEFAULT_REGISTRIES, featureId,
  ID_GRAMMAR, summarizeFeatureMap, validateFeatureMap, validateOverrides,
} from './featureMap.mjs'

const json = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'))
const snapshot = json('./fixtures/capabilities.snapshot.json')
const overrides = json('./features.overrides.json')
const clone = (value) => structuredClone(value)
const map = buildFeatureMap()
const ids = map.entries.map((entry) => entry.id)
const entryFor = (id) => map.entries.find((entry) => entry.id === id)

test('G1 Solar editor effects follow catalog view, placement and surface fold', () => {
  const readySnapshot = clone(snapshot)
  for (const tool of readySnapshot.response.families.flatMap((family) => family.capabilities)) {
    delete tool.availability
  }
  const editorMap = buildFeatureMap({ snapshot: readySnapshot })
  for (const name of ['solar-unit-sync', 'solar-design-presets']) {
    const id = `tool:${name}`
    assert.deepEqual(editorMap.entries.find((entry) => entry.id === id).expected_effect.ready,
      { kind: 'opens', target: 'solar-step-editor', tool: name })
    assert.equal(entryFor(id).expected_effect['job-running'].kind, 'disabled_with_reason')
    for (const mutate of [
      (tool) => { tool.placement = { tab: 'manage' } },
      (tool) => { tool.solar.interaction.mode = 'none' },
      (tool) => { delete tool.solar },
      (tool, family) => { family.family_id = 'drawing' },
    ]) {
      const changed = clone(snapshot)
      const family = changed.response.families.find((row) => row.capabilities.some((tool) => tool.name === name))
      const tool = family.capabilities.find((tool) => tool.name === name)
      mutate(tool, family)
      delete tool.availability // This test isolates editor placement from readiness.
      // Use an actual folded family id from the surface manifest.
      if (family.family_id === 'drawing') family.family_id = PRODUCT_SURFACES.find((row) => row.id === 'solar').familyIds[0]
      assert.equal(buildFeatureMap({ snapshot: changed }).entries.find((row) => row.id === id)
        .expected_effect.ready.target, 'catalog-run-decision')
    }
  }
  const changed = clone(snapshot)
  const family = changed.response.families.find((row) => row.capabilities.some((tool) => tool.name === 'solar-unit-sync'))
  const tool = family.capabilities.find((tool) => tool.name === 'solar-unit-sync')
  tool.name = 'record-derived-form'
  tool.solar.name = tool.name
  delete tool.availability
  const config = clone(overrides)
  delete config.overrides['tool:solar-unit-sync']
  assert.equal(buildFeatureMap({ snapshot: changed, overrides: config }).entries
    .find((row) => row.id === 'tool:record-derived-form').expected_effect.ready.target, 'solar-step-editor')
})

test('G3 Solar editors use product refusals while catalog decisions retain runtime readiness', () => {
  for (const [name, code, sentence] of [
    ['solar-equipment-move', 'equipment_assignment_required', 'Assign inverter equipment first'],
    ['solar-cable-export', 'solar_output_not_current', 'Rerun the earlier Solar steps so the whole design is current first'],
    ['solar-electrical-schedules', 'solar_output_not_current', 'Rerun the earlier Solar steps so the whole design is current first'],
    ['solar-solaredge-accept', 'frames_required', 'Create panel groups first'],
  ]) {
    const changed = clone(snapshot)
    const tool = changed.response.families.flatMap((family) => family.capabilities).find((tool) => tool.name === name)
    const readyEffect = () => buildFeatureMap({ snapshot: changed }).entries
      .find((row) => row.id === `tool:${name}`).expected_effect.ready
    tool.availability = { entitled: true, engine_ready: true, implemented: true,
      input_ready: false, refusal_reasons: [code] }
    assert.deepEqual(readyEffect(), { kind: 'disabled_with_reason', reason: sentence, reason_code: code })
    tool.availability.input_ready = true
    // The flags, rather than a stale refusal list, decide readiness.
    assert.deepEqual(readyEffect(), { kind: 'opens', target: 'solar-step-editor', tool: name })
    delete tool.availability
    assert.deepEqual(readyEffect(), { kind: 'opens', target: 'solar-step-editor', tool: name })
    tool.availability = { entitled: true, engine_ready: true, implemented: true,
      input_ready: false, refusal_reasons: ['drawing_context_required'] }
    assert.deepEqual(readyEffect(), { kind: 'opens', target: 'solar-step-editor', tool: name })
    tool.placement = { tab: 'manage' }
    assert.deepEqual(readyEffect(), { kind: 'opens', target: 'catalog-run-decision', tool: name })
    tool.availability.refusal_reasons = [code]
    assert.deepEqual(readyEffect(), { kind: 'opens', target: 'catalog-run-decision', tool: name })
    tool.availability = { entitled: true, engine_ready: true, implemented: true, input_ready: true }
    assert.deepEqual(readyEffect(), { kind: 'opens', target: 'catalog-run-decision', tool: name })
  }
})

test('C2 certifies all seven engine and disclosure controls with real recipes', () => {
  const expected = [
    ['open-dxf', 'Open DXF', 'toggles', 'dxf-import-expanded', 'closed'],
    ['save-version', 'Save version', 'submits', 'engine-save-version', 'ready'],
    ['undo-edit', 'Undo edit', 'submits', 'engine-undo-edit', 'ready'],
    ['redo-edit', 'Redo edit', 'submits', 'engine-redo-edit', 'ready'],
    ['more-panels', 'More panels', 'toggles', 'ribbon-overflow-expanded', 'closed'],
    ['open-dxf-browser', 'Open a DXF in the browser engine', 'toggles', 'dxf-import-expanded', 'closed'],
    ['objects', 'Objects', 'toggles', 'drawing-objects-expanded', 'closed'],
  ]
  assert.deepEqual(overrides.controls.slice(39, 46).map((row) => row.id), expected.map(([id]) => `control:${id}`))
  for (const [id, title, kind, target, state] of expected) {
    const entry = entryFor(`control:${id}`)
    assert.equal(entry.title, title)
    assert.equal(entry.certify, 'both')
    assert.equal(entry.certify_reason, undefined)
    assert.equal(entry.expected_effect[state].kind, kind)
    assert.equal(entry.expected_effect[state].target, target)
    assert.equal(entry.state_contexts['failed-load'].failedLoad, true)
    assert.throws(() => checkCompleteness({ ...map, entries: map.entries.filter((row) => row.id !== entry.id) }), /completeness failed/)
    if (kind === 'toggles') {
      assert.equal(entry.state_contexts.closed.expanded, false)
      assert.equal(entry.expected_effect.closed.value, true)
      assert.equal(entry.state_contexts.open.expanded, true)
      assert.equal(entry.expected_effect.open.value, false)
      const invalid = clone(overrides)
      invalid.controls.find((row) => row.id === entry.id).expected_effect.closed.value = false
      assert.throws(() => buildFeatureMap({ overrides: invalid }), /opposite setup and effect/)
    } else {
      assert.equal(entry.expected_effect['failed-load'].kind, 'disabled_with_reason')
      assert.equal(entry.expected_effect['failed-load'].reason, MODIFY_REASONS.noDocument)
      assert.equal(entry.expected_effect['engine-busy'].reason, MODIFY_REASONS.busy)
      assert.equal(entry.expected_effect['no-document'].reason_code, `${entry.id}:unavailable`)
    }
  }
  assert.equal(entryFor('control:save-version').expected_effect['nothing-edited'].reason, 'edit something first')
  assert.equal(entryFor('control:save-version').expected_effect['no-target'].reason, 'download-only here: no project target')
  assert.equal(entryFor('control:undo-edit').expected_effect['nothing-to-undo'].reason, 'nothing to undo')
  assert.equal(entryFor('control:redo-edit').expected_effect['nothing-to-redo'].reason, 'nothing to redo')
})

test('Script controls name scoped effects, native running locks and the Run refusal ladder', () => {
  const expected = [
    ['ribbon-script', 'ribbon script', 'renders', 'ribbon-script-text'],
    ['choose-script', 'Choose script', 'opens', 'script-file-picker'],
    ['run-script', 'Run script', 'submits', 'script-run'],
  ]
  assert.deepEqual(overrides.controls.slice(46).map((row) => row.id), expected.map(([id]) => `control:${id}`))
  for (const [id, title, kind, target] of expected) {
    const entry = entryFor(`control:${id}`)
    assert.equal(entry.title, title)
    assert.equal(entry.certify, 'both')
    assert.equal(entry.certify_reason, undefined)
    assert.ok(entry.sources.includes('web/src/cadedit/ScriptPanel.jsx'))
    assert.deepEqual(entry.expected_effect.ready, { kind, target })
    assert.equal(entry.expected_effect.running.reason, 'a script is running')
    assert.equal(entry.expected_effect.running.reason_code, `${entry.id}:unavailable`)
    for (const state of entry.states) {
      assert.equal(entry.state_contexts[state].toolbar, 'Drafting tools')
      assert.equal(entry.state_contexts[state].group, 'Script')
      assert.equal(entry.state_contexts[state].failedLoad, state === 'failed-load')
    }
    if (id !== 'run-script') assert.deepEqual(entry.expected_effect['failed-load'], { kind, target })
    const wrongScope = clone(overrides)
    wrongScope.controls.find((row) => row.id === entry.id).state_contexts.ready.group = 'Layers'
    assert.throws(() => buildFeatureMap({ overrides: wrongScope }), /invalid Script control contract/)
    assert.throws(() => checkCompleteness({ ...map, entries: map.entries.filter((row) => row.id !== entry.id) }), /completeness failed/)
  }
  const input = entryFor('control:ribbon-script')
  assert.equal(input.state_contexts.ready.role, 'textbox')
  assert.equal(input.state_contexts.ready.interaction, 'type')
  assert.equal(input.state_contexts.ready.inputValue, 'line 0,0 10,10')
  assert.equal(input.state_contexts.running.name, 'ribbon script')
  assert.equal(entryFor('control:choose-script').state_contexts.running.tooltip,
    'A script is running; wait before choosing another script.')
  const run = entryFor('control:run-script')
  assert.deepEqual(run.states, ['empty-script', 'engine-busy', 'engine-crashed', 'failed-load', 'no-document', 'ready', 'running'])
  for (const [state, reason] of [
    ['empty-script', 'enter or choose a script'],
    ['no-document', DRAW_REASONS.noDocument], ['failed-load', DRAW_REASONS.noDocument],
    ['engine-busy', DRAW_REASONS.busy], ['engine-crashed', DRAW_REASONS.crashed],
    ['running', 'a script is running'],
  ]) {
    assert.deepEqual(run.expected_effect[state], { kind: 'disabled_with_reason', reason,
      reason_code: 'control:run-script:unavailable' })
    assert.equal(run.state_contexts[state].name, `Run script (unavailable: ${reason})`)
    assert.equal(run.state_contexts[state].tooltip, reason)
  }
  const invalid = clone(overrides)
  invalid.controls.find((row) => row.id === run.id).state_contexts['failed-load'].name = 'Run script'
  assert.throws(() => buildFeatureMap({ overrides: invalid }), /disabled evidence mismatch/)
})

test('reachable map states and phone-only drawers remove exactly thirty-four triples', () => {
  const triples = (featureMap) => featureMap.entries.filter((entry) => entry.certify_reason !== 'needs a walk recipe (wave C)').flatMap((entry) => entry.states.flatMap((state) =>
    (entry.state_viewports?.[state] || entry.viewports).map((viewport) => `${entry.id}/${state}/${viewport}`))).sort()
  for (const entry of map.entries) {
    for (const state of ['read-only-entity', 'no-versioned-drawing']) {
      assert.ok(!entry.states.includes(state), entry.id + '/' + state)
      assert.equal(entry.expected_effect[state], undefined)
      assert.equal(entry.state_contexts[state], undefined)
    }
  }
  for (const id of ['drawer:plan', 'drawer:result']) {
    assert.deepEqual(entryFor(id).states, ['closed', 'open'])
    assert.deepEqual(entryFor(id).viewports, ['phone'])
  }
  // Reconstruct the previous inventory to pin removals without a map snapshot.
  const previous = clone(overrides)
  previous.state_cases.action.patches['read-only-entity'] = {
    session: { selected: { editable: false, type: 'LINE' } },
  }
  previous.state_cases.action.patches['no-versioned-drawing'] = { hasVersions: false }
  for (const id of ['drawer:plan', 'drawer:result']) previous.overrides[id].viewports = ['desktop', 'phone']
  const previousTriples = triples(buildFeatureMap({ overrides: previous }))
  // 21-B2 added engine:undo, engine:redo and engine:repeat plus the engine-nothing-to-undo, engine-nothing-to-redo and no-command-to-repeat patches (793 -> 811, 759 -> 777). C2 certifies the 41 census triples (811 -> 852, 777 -> 818).
  assert.equal(previousTriples.length, 852)
  assert.equal(triples(map).length, 818)
  assert.deepEqual(triples(map), previousTriples.filter((triple) =>
    !triple.includes('/read-only-entity/') && !triple.includes('/no-versioned-drawing/')
      && !/^drawer:(plan|result)\/(closed|open)\/desktop$/.test(triple)))
  // Removing an unreachable setup must not remove the registry's actual gate.
  for (const id of ['history', 'redo', 'undo']) {
    assert.equal(ACTIONS.find((action) => action.id === id).when({ hasVersions: false }), REASONS.noVersions)
  }
})

test('W21B2-map-engine-actions', () => {
  const engineActions = ['engine:undo', 'engine:redo', 'engine:repeat']
  for (const operation of ['undo', 'redo']) {
    assert.deepEqual(entryFor(featureId('action', `engine:${operation}`)).expected_effect.ready, {
      kind: 'submits', target: `engine:${operation}`, operation, group: 'engine',
    })
  }
  assert.deepEqual(entryFor('action:engine-repeat').expected_effect.ready, {
    kind: 'opens', target: 'cockpit-prompt', operation: 'createLine', group: 'draw',
  })
  for (const id of engineActions) {
    const entry = entryFor(featureId('action', id))
    assert.equal(ACTIONS.find((action) => action.id === id).when(entry.state_contexts.ready), '')
  }
  for (const [state, disabledId, reason] of [
    ['engine-nothing-to-undo', 'engine:undo', REASONS.nothingToUndo],
    ['engine-nothing-to-redo', 'engine:redo', REASONS.nothingToRedo],
    ['no-command-to-repeat', 'engine:repeat', REPEAT_REASONS.empty],
  ]) {
    for (const id of engineActions) {
      const entry = entryFor(featureId('action', id))
      const action = ACTIONS.find((candidate) => candidate.id === id)
      if (id === disabledId) {
        assert.ok(entry.states.includes(state), id + '/' + state)
        assert.deepEqual(entry.expected_effect[state], {
          kind: 'disabled_with_reason', reason_code: reasonCode(reason), reason,
        })
        assert.equal(action.when(entry.state_contexts[state]), reason)
        for (const otherId of engineActions.filter((candidate) => candidate !== id)) {
          assert.equal(ACTIONS.find((candidate) => candidate.id === otherId).when(entry.state_contexts[state]), '')
        }
      } else {
        assert.ok(!entry.states.includes(state), id + '/' + state)
        assert.equal(entry.expected_effect[state], undefined)
      }
    }
  }
})

test('W21B1-map-disables-placed-actions', () => {
  for (const [id, ready] of [
    ['action:clipboard-copy-clip', { kind: 'toggles', target: 'browser-clipboard' }],
    ['action:clipboard-cut-clip', { kind: 'submits', target: 'browser-clipboard-cut' }],
    ['action:modify-explode', { kind: 'submits', target: 'engine:explode', operation: 'explode', group: 'modify' }],
  ]) {
    const entry = entryFor(id)
    for (const [state, reason, reason_code] of [
      ['placed-insert', 'Copy, Cut and Explode do not support INSERT block references yet', 'MODIFY_REASONS.unsupportedInsert'],
      ['placed-dimension', 'Copy, Cut and Explode do not support DIMENSION entities yet', 'MODIFY_REASONS.unsupportedDimension'],
    ]) {
      assert.ok(entry.states.includes(state), id + '/' + state)
      assert.deepEqual(entry.expected_effect[state], { kind: 'disabled_with_reason', reason_code, reason })
      assert.equal(ACTIONS.find((action) => featureId('action', action.id) === id)
        .when(entry.state_contexts[state]), reason)
    }
    assert.deepEqual(entry.expected_effect.ready, ready)
  }
})

test('batch three declares exactly seventeen default-build semantic controls with complete states', () => {
  const expected = [
  [
    "scope-add",
    "Add: build a new capability",
    "opens",
    "scope-build-picker",
    [
      "ready",
      "failed-load"
    ]
  ],
  [
    "demo-return",
    "Back to the demo",
    "renders",
    "guided-demo",
    [
      "failed-load"
    ]
  ],
  [
    "claude-accounts",
    "Claude accounts not linked",
    "opens",
    "claude-accounts-panel",
    [
      "ready",
      "failed-load"
    ]
  ],
  [
    "drawing-close-start",
    "Close the drawing view and return to Start",
    "opens",
    "project-board",
    [
      "ready"
    ]
  ],
  [
    "notification-collapse",
    "Collapse the notification inbox",
    "renders",
    "notification-inbox-collapsed",
    [
      "ready",
      "failed-load"
    ]
  ],
  [
    "session-details",
    "Details",
    "opens",
    "session-provenance",
    [
      "ready",
      "failed-load"
    ]
  ],
  [
    "version-history",
    "History",
    "opens",
    "version-history",
    [
      "ready"
    ]
  ],
  [
    "linked-services",
    "Linked services {n} linked",
    "opens",
    "linked-services-panel",
    [
      "ready",
      "failed-load"
    ]
  ],
  [
    "project-board",
    "Open the project board",
    "opens",
    "project-board",
    [
      "ready",
      "failed-load"
    ]
  ],
  [
    "prompt-run",
    "Run",
    "submits",
    "unknown-tool-resolver",
    [
      "ready",
      "failed-load"
    ]
  ],
  [
    "prompt-scope",
    "scope ▾",
    "opens",
    "scope-picker",
    [
      "ready",
      "failed-load"
    ]
  ],
  [
    "sign-out",
    "Sign out",
    "renders",
    "signed-out-session",
    [
      "ready",
      "failed-load"
    ]
  ],
  [
    "start-board",
    "Start",
    "opens",
    "project-board",
    [
      "ready",
      "failed-load"
    ]
  ],
  [
    "take-edit-lock",
    "Take edit lock",
    "renders",
    "edit-lock-held",
    [
      "ready"
    ]
  ],
  [
    "cost-panel",
    "What Leaf costs to operate",
    "opens",
    "cost-panel",
    [
      "ready",
      "failed-load"
    ]
  ],
  [
    "command-bar",
    "Command bar",
    "opens",
    "tool-commands",
    [
      "ready",
      "failed-load"
    ]
  ],
  [
    "find-drawing",
    "Find in drawing",
    "renders",
    "drawing-find-no-match",
    [
      "ready"
    ]
  ]
]
  assert.equal(expected.length, 17)
  assert.deepEqual(overrides.controls.slice(22, 39).map((row) => row.id), expected.map(([id]) => 'control:' + id))
  for (const [id, title, kind, target, states] of expected) {
    const row = entryFor('control:' + id)
    assert.equal(row.title, title)
    assert.deepEqual(row.states, [...states].sort())
    if (id === 'session-details') assert.equal(row.certify_reason, undefined)
    for (const state of states) {
      assert.deepEqual(row.expected_effect[state], { kind, target })
      assert.equal(row.state_contexts[state].document, true)
      assert.equal(row.state_contexts[state].failedLoad, state === 'failed-load')
    }
    assert.throws(() => checkCompleteness({ ...map, entries: map.entries.filter((entry) => entry.id !== row.id) }), /completeness failed/)
  }
})

test('batch three refuses malformed interaction fields, count policies and state/effect pairs', () => {
  for (const [id, mutate] of [
    ['command-bar', (row) => { row.state_contexts.ready.role = 'textbox' }],
    ['command-bar', (row) => { row.state_contexts.ready.role = '' }],
    ['command-bar', (row) => { row.state_contexts.ready.interaction = 'click' }],
    ['command-bar', (row) => { row.state_contexts.ready.interaction = '' }],
    ['command-bar', (row) => { row.state_contexts.ready.inputValue = null }],
    ['command-bar', (row) => { row.state_contexts.ready.inputValue = '/wrong' }],
    ['command-bar', (row) => { row.state_contexts.ready.key = 'Enter' }],
    ['find-drawing', (row) => { row.state_contexts.ready.interaction = 'type' }],
    ['find-drawing', (row) => { row.state_contexts.ready.role = 'button' }],
    ['prompt-run', (row) => { row.state_contexts.ready.inputValue = '/wrong' }],
    ['prompt-run', (row) => { row.expected_effect.ready.kind = 'opens' }],
    ['prompt-run', (row) => { row.expected_effect.ready.target = 'catalog-run-decision' }],
    ['scope-add', (row) => { row.state_contexts.ready.role = 'combobox' }],
    ['scope-add', (row) => { row.state_contexts.ready.failedLoad = true }],
    ['scope-add', (row) => { delete row.state_contexts['failed-load'] }],
    ['scope-add', (row) => { row.states = ['ready']; delete row.expected_effect['failed-load']; delete row.state_contexts['failed-load'] }],
    ['linked-services', (row) => { row.state_contexts.ready.namePolicy = 'regex' }],
    ['linked-services', (row) => { delete row.state_contexts.ready.namePolicy }],
    ['linked-services', (row) => { row.state_contexts.ready.name = 'Linked services 0 linked' }],
    ['linked-services', (row) => { row.state_contexts.ready.expanded = true }],
    ['notification-collapse', (row) => { row.state_contexts.ready.expanded = false }],
    ['sign-out', (row) => { row.expected_effect.ready.kind = 'submits' }],
  ]) {
    const config = clone(overrides)
    mutate(config.controls.find((row) => row.id === 'control:' + id))
    assert.throws(() => buildFeatureMap({ overrides: config }), /featureMap:/, id)
  }
  const earlier = clone(overrides)
  earlier.controls = earlier.controls.slice(0, 22)
  const previous = buildFeatureMap({ overrides: earlier })
  for (const row of previous.entries) assert.deepEqual(entryFor(row.id), row)
})
const exclusionOverrides = { ...overrides, overrides: { ...overrides.overrides,
  'action:fit': { effect: { kind: 'renders', target: 'viewer-home' } },
  'drawer:nav': {},
} }

test('every exported action, surface, drawer and profile tab appears exactly once', () => {
  const expected = [
    ...overrides.controls.map((control) => control.id),
    ...ACTIONS.map((action) => featureId('action', action.id)),
    ...PRODUCT_SURFACES.map((surface) => featureId('surface', surface.id)),
    ...STUDIO_DRAWERS.map((drawer) => featureId('drawer', drawer)),
    ...Object.entries(PROFILE_RIBBON_TABS).flatMap(([profile, tabs]) =>
      tabs.map((tab) => featureId('tab', tab.id, profile))),
    ...snapshot.response.families.flatMap((family) => family.capabilities.map((tool) => featureId('tool', tool.name))),
  ]
  assert.deepEqual(ids, expected.sort())
  assert.equal(new Set(ids).size, ids.length)
  for (const id of expected) assert.equal(ids.filter((value) => value === id).length, 1, id)
  assert.equal(checkCompleteness(map), true)
})

test('completeness rejects every omitted row, including tools and the none drawer', () => {
  for (const entry of map.entries) {
    const incomplete = { ...map, entries: map.entries.filter((row) => row.id !== entry.id) }
    assert.throws(() => checkCompleteness(incomplete), (error) =>
      error.message.includes('completeness failed') && error.message.includes(entry.id), entry.id)
  }
  assert.throws(() => checkCompleteness({ ...map, entries: [...map.entries, map.entries[0]] }), /duplicate/)
})

test('forty-nine exact control declarations participate in independent completeness', () => {
  const expected = ['fullscreen', 'grid-display', 'new-drawing', 'object-snap', 'ortho-mode',
    'polar-tracking', 'print', 'snap-mode', 'view-back', 'view-up',
    'properties-close', 'properties-drawing', 'properties-layers', 'properties-panels', 'properties-plan',
    'properties-selection', 'properties-walk', 'layer-panels', 'layer-walk', 'job-monitor-expand',
    'drawing-overview', 'drawing-overview-collapse',
    'scope-add', 'demo-return', 'claude-accounts', 'drawing-close-start', 'notification-collapse', 'session-details', 'version-history', 'linked-services', 'project-board', 'prompt-run', 'prompt-scope', 'sign-out', 'start-board', 'take-edit-lock', 'cost-panel', 'command-bar', 'find-drawing',
    'open-dxf', 'save-version', 'undo-edit', 'redo-edit', 'more-panels', 'open-dxf-browser', 'objects',
    'ribbon-script', 'choose-script', 'run-script'].map((id) => 'control:' + id).sort()
  assert.equal(expected.length, 49)
  assert.deepEqual(map.entries.filter((row) => row.kind === 'control').map((row) => row.id), expected)
  for (const declaration of overrides.controls) {
    const entry = entryFor(declaration.id)
    assert.equal(entry.source_id, declaration.source_id)
    assert.deepEqual(entry.expected_effect, declaration.expected_effect)
    assert.deepEqual(entry.state_contexts, declaration.state_contexts)
    assert.deepEqual(entry.states, [...declaration.states].sort())
    if (declaration.id === 'control:session-details') {
      assert.deepEqual(entry.states, ['failed-load', 'ready'])
      assert.equal(declaration.certify_reason, undefined)
      assert.equal(entry.certify_reason, undefined)
    }
    assert.equal(entry.certify, declaration.certify)
    assert.deepEqual(entry.viewports, ['desktop'])
    const reference = overrides.controls.filter((row) => row.id !== declaration.id)
    assert.throws(() => checkCompleteness(map, DEFAULT_REGISTRIES, snapshot, reference), /unexpected/)
  }
})

test('malformed control declarations, duplicate ids and missing contracts fail closed', () => {
  const control = (config, id) => config.controls.find((row) => row.id === id)
  for (const mutate of [
    (config) => { config.controls = null },
    (config) => { config.controls.push(clone(control(config, 'control:grid-display'))) },
    (config) => { const row = control(config, 'control:grid-display'); config.controls = config.controls.map((entry) => entry === row ? null : entry) },
    (config) => { control(config, 'control:grid-display').selector = 'button' },
    (config) => { control(config, 'control:grid-display').id = 'control:*' },
    (config) => { control(config, 'control:grid-display').source_id = 'Grid Display' },
    (config) => { delete control(config, 'control:grid-display').expected_effect },
    (config) => { control(config, 'control:grid-display').expected_effect.extra = { kind: 'toggles', target: 'drafting-grid' } },
    (config) => { delete control(config, 'control:grid-display').state_contexts.on },
    (config) => { control(config, 'control:grid-display').state_contexts.on = {} },
    (config) => { control(config, 'control:grid-display').state_contexts.on.selector = 'button' },
    (config) => { control(config, 'control:grid-display').state_contexts.on.pressed = 'true' },
    (config) => { control(config, 'control:grid-display').expected_effect.on.value = true },
    (config) => { delete control(config, 'control:polar-tracking').state_contexts.unavailable.tooltip },
    (config) => { control(config, 'control:polar-tracking').state_contexts.unavailable.name = 'Polar tracking' },
    (config) => { control(config, 'control:grid-display').sources = ['../outside.js'] },
    (config) => { control(config, 'control:grid-display').certify = 'unknown' },
    (config) => { control(config, 'control:snap-mode').expected_effect.unavailable.reason = '' },
    (config) => { delete control(config, 'control:snap-mode').expected_effect.unavailable.reason_code },
    (config) => { control(config, 'control:snap-mode').expected_effect.unavailable.reason_code = 'made-up-code' },
    (config) => { control(config, 'control:grid-display').expected_effect.on.unrecognized = true },
    (config) => { config.control = [] },
    (config) => { config.overrides['control:unknown'] = { title: 'Unknown' } },
  ]) {
    const config = clone(overrides)
    mutate(config)
    assert.throws(() => buildFeatureMap({ overrides: config }), /featureMap:/)
  }
})

test('new control scopes, normalized names and initial toggle states fail closed', () => {
  const mutateControl = (id, mutate) => {
    const config = clone(overrides)
    mutate(config.controls.find((row) => row.id === 'control:' + id))
    assert.throws(() => buildFeatureMap({ overrides: config }), /featureMap:/)
  }
  for (const mutate of [
    (row) => { row.state_contexts.open.toolbar = 'Drafting tools' },
    (row) => { row.state_contexts.open.complementary = 'Elsewhere' },
    (row) => { delete row.state_contexts.open.complementary },
    (row) => { row.state_contexts.open.expanded = 'true' },
    (row) => { row.expected_effect.open.value = true },
    (row) => { delete row.expected_effect.open },
  ]) mutateControl('properties-drawing', mutate)
  for (const mutate of [
    (row) => { row.state_contexts.shown.group = 'Panels' },
    (row) => { row.state_contexts.shown.toolbar = 'View' },
    (row) => { row.expected_effect.shown.value = true },
    (row) => { delete row.state_contexts.shown.visible },
  ]) mutateControl('layer-panels', mutate)
  for (const mutate of [
    (row) => { row.state_contexts.shown.namePolicy = 'regex' },
    (row) => { delete row.state_contexts.shown.namePolicy },
    (row) => { row.state_contexts.shown.name = 'Panels 1' },
  ]) mutateControl('properties-panels', mutate)
  mutateControl('drawing-overview', (row) => { row.state_contexts.ready.document = false })
  mutateControl('drawing-overview', (row) => { row.state_contexts.ready.group = 'Layers' })
  mutateControl('drawing-overview-collapse', (row) => { row.expected_effect.expanded.value = true })
  mutateControl('properties-close', (row) => { row.expected_effect.ready.value = true })
  mutateControl('properties-close', (row) => { row.state_contexts.ready.visible = false })
  mutateControl('job-monitor-expand', (row) => { row.state_contexts.ready.expanded = true })
  mutateControl('job-monitor-expand', (row) => { row.state_contexts.ready.toolbar = 'View' })
})

test('engine drafting modes toggle provider states and placeholders remain unavailable', () => {
  for (const [id, mode] of [['object-snap', 'osnap'], ['ortho-mode', 'ortho']]) {
    const entry = entryFor('control:' + id)
    assert.deepEqual(entry.states, ['failed-load', 'off', 'on'])
    for (const state of entry.states) {
      assert.equal(entry.state_contexts[state].name, entry.title)
      assert.equal(entry.expected_effect[state].target, 'engine-mode:' + mode)
      assert.equal(entry.expected_effect[state].kind, 'toggles')
      assert.equal(entry.expected_effect[state].value, !entry.state_contexts[state].pressed)
    }
  }
  for (const id of ['polar-tracking', 'snap-mode']) {
    const entry = entryFor('control:' + id)
    for (const effect of Object.values(entry.expected_effect)) {
      assert.equal(effect.kind, 'disabled_with_reason')
      assert.equal(effect.reason, 'not in the browser viewer yet')
      assert.equal(effect.reason_code, entry.id + ':unavailable')
    }
  }
  const grid = entryFor('control:grid-display')
  assert.deepEqual(grid.expected_effect.off, { kind: 'toggles', target: 'drafting-grid', value: true })
  assert.deepEqual(grid.expected_effect.on, { kind: 'toggles', target: 'drafting-grid', value: false })
  assert.equal(entryFor('control:view-back').expected_effect['history-present'].target, 'viewer-previous-view')
  assert.equal(entryFor('control:view-up').expected_effect['whole-drawing'].target, 'viewer-whole-drawing')
})

test('checker compares injected registries independently of the built map', () => {
  for (const field of ['actions', 'surfaces', 'drawers']) {
    const fake = { ...DEFAULT_REGISTRIES, [field]: DEFAULT_REGISTRIES[field].slice(1) }
    const record = DEFAULT_REGISTRIES[field][0]
    const id = featureId({ actions: 'action', surfaces: 'surface', drawers: 'drawer' }[field], record.id || record)
    assert.throws(() => checkCompleteness(map, fake), (error) =>
      error.message.includes('unexpected') && error.message.includes(id))
  }
  for (const [profile, tabs] of Object.entries(PROFILE_RIBBON_TABS)) {
    const fake = { ...DEFAULT_REGISTRIES, tabs: { ...PROFILE_RIBBON_TABS, [profile]: tabs.slice(1) } }
    const id = featureId('tab', tabs[0].id, profile)
    assert.throws(() => checkCompleteness(map, fake), (error) => error.message.includes(id))
  }
  const extra = { ...DEFAULT_REGISTRIES, drawers: [...STUDIO_DRAWERS, 'future-drawer'] }
  assert.throws(() => checkCompleteness(map, extra), /missing \[drawer:future-drawer\]/)
  const expanded = buildFeatureMap({ registries: extra })
  assert.ok(expanded.entries.some((entry) => entry.id === 'drawer:future-drawer'))
  assert.equal(checkCompleteness(expanded, extra), true)
})

test('all rows declare valid unique ids, sources, states, effects, viewports and certification', () => {
  assert.equal(validateFeatureMap(map), map)
  for (const entry of map.entries) {
    assert.match(entry.id, ID_GRAMMAR)
    assert.ok(entry.states.length > 0)
    assert.deepEqual(Object.keys(entry.expected_effect).sort(), entry.states)
    assert.ok(entry.sources.length > 0)
    assert.ok(entry.viewports.length > 0)
    assert.ok(['local', 'staging', 'both', 'unsupported_local'].includes(entry.certify))
    if (entry.certify !== 'both') assert.ok(entry.certify_reason.trim())
    for (const state of entry.states) {
      const effect = entry.expected_effect[state]
      assert.ok(['opens', 'toggles', 'navigates', 'submits', 'disabled_with_reason', 'renders'].includes(effect.kind))
      assert.ok(effect.kind === 'disabled_with_reason' ? effect.reason_code && effect.reason : effect.target)
    }
  }
  for (const patch of [
    { states: [] }, { id: 'action:bad*' }, { sources: ['C:/secret/file.js'] },
    { viewports: [] }, { viewports: ['tablet'] },
    { certify: 'local', certify_reason: ' ' }, { expected_effect: {} },
    { expected_effect: { ready: { kind: 'unknown', target: 'somewhere' } } },
  ]) {
    const invalid = clone(map)
    Object.assign(invalid.entries[0], patch)
    assert.throws(() => validateFeatureMap(invalid), /featureMap:/)
  }
})

test('wildcard and prefix exemptions are refused instead of hiding future controls', () => {
  for (const id of ['action:*', 'action:bar-*', 'action:', 'action:bar']) {
    const config = clone(overrides)
    config.exemptions.push({ id, reason: 'Temporarily unsupported', owner: 'studio-walk' })
    assert.throws(() => validateOverrides(config, ids), /wildcard|prefix/)
  }
  const config = clone(overrides)
  config.exemptions.push({ id: ids[0], reason: 'Temporarily unsupported', owner: 'studio-walk', prefix: true })
  assert.throws(() => validateOverrides(config, ids), /prefix fields are forbidden/)
})

test('exemptions require a non-empty reason, an owner, and one known exact id', () => {
  for (const reason of ['', '   ', null]) {
    const config = clone(overrides)
    config.exemptions.push({ id: ids[0], reason, owner: 'studio-walk' })
    assert.throws(() => buildFeatureMap({ overrides: config }), /non-empty reason/)
  }
  const noOwner = clone(overrides)
  noOwner.exemptions.push({ id: ids[0], reason: 'Needs a provider', owner: '' })
  assert.throws(() => buildFeatureMap({ overrides: noOwner }), /requires an owner/)
  const unknown = clone(overrides)
  unknown.exemptions.push({ id: 'action:unknown-control', reason: 'Needs a provider', owner: 'studio-walk' })
  assert.throws(() => buildFeatureMap({ overrides: unknown }), /unknown id/)
  const exact = clone(overrides)
  exact.exemptions.push({ id: ids[0], reason: 'Needs a provider', owner: 'studio-walk' })
  const exempted = buildFeatureMap({ overrides: exact })
  assert.deepEqual(exempted.exemptions, exact.exemptions)
  assert.deepEqual(exempted.entries, map.entries)
  assert.equal(checkCompleteness(exempted), true)
})

test('unknown overrides, wildcard overrides, and silent override typos fail clearly', () => {
  for (const id of ['action:unknown-control', 'action:*']) {
    const config = clone(overrides)
    config.overrides[id] = { title: 'Unknown' }
    assert.throws(() => buildFeatureMap({ overrides: config }), /unknown id|wildcard/)
  }
  const config = clone(overrides)
  config.overrides[ids[0]] = { certfy: 'local' }
  assert.throws(() => buildFeatureMap({ overrides: config }), /unknown override field/)
})

test('state exclusions remove only named states, effects and contexts while retaining the feature', () => {
  const config = clone(exclusionOverrides)
  const baseline = buildFeatureMap({ overrides: config })
  const fit = baseline.entries.find((entry) => entry.id === 'action:fit')
  assert.ok(fit.states.includes('no-drawing'))
  assert.equal(fit.expected_effect['no-drawing'].kind, 'disabled_with_reason')
  assert.ok(fit.state_contexts['no-drawing'])
  config.overrides['action:fit'].exclude_states = ['no-drawing']
  config.overrides['action:fit'].reason = 'Synthetic exclusion exercises action states'
  const excludedMap = buildFeatureMap({ overrides: config })
  const excluded = excludedMap.entries.find((entry) => entry.id === 'action:fit')
  assert.deepEqual(excluded.states, fit.states.filter((state) => state !== 'no-drawing'))
  const expected = clone(fit)
  expected.states = expected.states.filter((state) => state !== 'no-drawing')
  delete expected.expected_effect['no-drawing']
  delete expected.state_contexts['no-drawing']
  assert.deepEqual(excluded, expected)
  assert.equal(excludedMap.entries.length, baseline.entries.length)
  assert.equal(checkCompleteness(excludedMap), true)
  config.overrides['drawer:nav'].exclude_states = ['closed']
  config.overrides['drawer:nav'].reason = 'Synthetic exclusion exercises drawer states'
  const drawer = buildFeatureMap({ overrides: config }).entries.find((entry) => entry.id === 'drawer:nav')
  assert.deepEqual(drawer.states, ['open'])
  assert.deepEqual(Object.keys(drawer.expected_effect), ['open'])
})

test('state exclusions refuse unknown states and missing or empty reasons', () => {
  const unknown = clone(exclusionOverrides)
  unknown.overrides['action:fit'].exclude_states = ['unknown-state']
  unknown.overrides['action:fit'].reason = 'Synthetic exclusion exercises unknown states'
  assert.throws(() => buildFeatureMap({ overrides: unknown }), /action:fit.*unknown state: unknown-state/)
  for (const reason of [undefined, '', '   ', null]) {
    const config = clone(exclusionOverrides)
    config.overrides['action:fit'].exclude_states = ['no-drawing']
    config.overrides['action:fit'].reason = reason
    assert.throws(() => buildFeatureMap({ overrides: config }), /state exclusion action:fit requires a non-empty reason/)
  }
})

test('state exclusions refuse malformed lists and exclusions that remove every state', () => {
  for (const states of ['no-drawing', [], [''], [null], ['no-drawing', 'no-drawing']]) {
    const config = clone(exclusionOverrides)
    config.overrides['action:fit'].exclude_states = states
    assert.throws(() => buildFeatureMap({ overrides: config }), /exclude_states requires a non-empty array of unique states/)
  }
  const config = clone(exclusionOverrides)
  config.overrides['drawer:nav'].exclude_states = ['closed', 'open']
  config.overrides['drawer:nav'].reason = 'Synthetic exclusion of all states'
  assert.throws(() => buildFeatureMap({ overrides: config }), /drawer:nav needs a title and unique states/)
})

test('action cases project registry gates and engine prompt behavior', () => {
  for (const action of ACTIONS) {
    const entry = entryFor(featureId('action', action.id))
    for (const state of entry.states) {
      const reason = action.when(entry.state_contexts[state])
      const effect = entry.expected_effect[state]
      if (reason) {
        assert.equal(effect.kind, 'disabled_with_reason')
        assert.equal(effect.reason, reason)
        assert.equal(effect.reason_code, reasonCode(reason))
      } else assert.notEqual(effect.kind, 'disabled_with_reason')
    }
    if (action.op && !overrides.overrides[entry.id]?.effect) {
      assert.equal(entry.expected_effect.ready.kind, PROMPTS[action.op] ? 'opens' : 'submits')
      assert.equal(entry.expected_effect.ready.operation, action.op)
    }
  }
  assert.equal(entryFor('action:draw-create-line').expected_effect['no-drawing'].reason, DRAW_REASONS.noDocument)
  assert.equal(entryFor('action:modify-move').expected_effect['placed-dimension'].kind, 'opens')
  assert.equal(entryFor('action:modify-move').expected_effect['placed-insert'].kind, 'opens')
  assert.deepEqual(entryFor('action:clipboard-copy-clip').expected_effect.ready, { kind: 'toggles', target: 'browser-clipboard' })
  assert.ok(!entryFor('action:fit').states.includes('unentitled'))
  assert.ok(entryFor('action:author-tool').states.includes('unentitled'))
  const escape = Object.values(entryFor('action:bar-escape').expected_effect).map((effect) => effect.target)
  for (const rung of ESCAPE_RUNGS) assert.ok(escape.includes(`escape:${rung.id}`), rung.id)
  const retry = Object.values(entryFor('action:bar-retry').expected_effect).map((effect) => effect.target)
  for (const target of Object.keys(RETRY_RUNGS)) assert.ok(retry.includes(`retry:${target}`), target)
  assert.equal(entryFor('action:bar-retry').expected_effect['result-owns-retry'].kind, 'disabled_with_reason')
})

test('surface status changes and unavailable tabs are behavior coverage', () => {
  assert.equal(entryFor('surface:cad').expected_effect['signed-out'].state, 'sign-in')
  assert.equal(entryFor('surface:cad').expected_effect['no-drawing'].state, 'setup')
  assert.equal(entryFor('surface:cad').expected_effect['execution-paused'].state, 'unavailable')
  assert.equal(entryFor('surface:solar').expected_effect['solar-not-ready'].state, 'beta')
  assert.equal(entryFor('surface:ios').expected_effect['apple-not-ready'].state, 'setup')
  assert.deepEqual(entryFor('surface:sheets').states, ['ready'])
  for (const profile of ['drafting', 'solar']) {
    const model = entryFor(`tab:${profile}:model`)
    assert.equal(model.expected_effect.unavailable.kind, 'disabled_with_reason')
    assert.equal(model.certify, 'local')
    assert.equal(model.certify_reason, PROFILE_RIBBON_TABS[profile].find((tab) => tab.id === 'model').reason)
  }
  for (const drawer of STUDIO_DRAWERS) {
    assert.ok(entryFor(featureId('drawer', drawer)).viewports.includes('phone'))
  }
})


test('state viewports remove desktop overlay cases while preserving phone and other states', () => {
  const escape = entryFor('action:bar-escape')
  assert.deepEqual(escape.state_viewports, { 'drawer-open': ['phone'] })
  assert.deepEqual(escape.viewports, ['desktop'])
  assert.deepEqual(entryFor('drawer:none').viewports, ['phone'])
  for (const state of ['drawer-open', 'drawers-closed']) assert.ok(entryFor('drawer:none').states.includes(state))
  for (const state_viewports of [null, [], { absent: ['phone'] }, { ready: [] },
    { ready: ['tablet'] }, { ready: ['phone', 'phone'] }]) {
    const config = clone(overrides)
    config.overrides['action:bar-escape'].state_viewports = state_viewports
    assert.throws(() => buildFeatureMap({ overrides: config }), /invalid state_viewports/)
  }
  const config = clone(overrides)
  config.overrides['action:fit'].state_viewports = { ready: ['phone'] }
  const fit = buildFeatureMap({ overrides: config }).entries.find((entry) => entry.id === 'action:fit')
  assert.deepEqual(fit.viewports, ['desktop'])
  assert.deepEqual(fit.state_viewports.ready, ['phone'])
  const consumer = readFileSync(new URL('../e2e/walk/features.spec.mjs', import.meta.url), 'utf8')
  assert.ok(consumer.includes('entry.state_viewports?.[state] || entry.viewports'))
})

test('catalog snapshot preserves the isolated catalog and expands every tool with its version', () => {
  assert.equal(snapshot.response.families.length, 10)
  assert.equal(map.entries.filter((entry) => entry.kind === 'tool').length, 54)
  assert.equal(snapshot.endpoint, '/api/capabilities')
  assert.equal(map.catalog_version, snapshot.catalog_version)
  const tools = map.entries.filter((entry) => entry.kind === 'tool')
  assert.equal(tools.length, snapshot.response.families.reduce((count, family) => count + family.capabilities.length, 0))
  for (const entry of tools) {
    assert.equal(entry.catalog_version, snapshot.catalog_version)
    const family = snapshot.response.families.find((family) => family.family_id === entry.family_id)
    assert.equal(entry.tool_version, family.capabilities.find((tool) => tool.name === entry.source_id).version)
    assert.ok(entry.sources.includes('server/routers/capabilities.py'))
    assert.ok(entry.sources.includes('web/walk/fixtures/capabilities.snapshot.json'))
  }
  const write = entryFor('tool:delete-marked-panel')
  const read = entryFor('tool:count-by-layer')
  assert.equal(write.expected_effect['write-locked'].reason, REASONS.writeLocked)
  assert.equal(write.expected_effect['write-unentitled'].reason, REASONS.writeUnentitled)
  assert.equal(write.expected_effect['unsaved-engine-edits'].reason, REASONS.unsavedEngineEdits)
  assert.ok(!read.states.includes('write-locked'))
  assert.equal(read.expected_effect['job-running'].reason, REASONS.running)
  const expanded = clone(snapshot)
  const readTool = snapshot.response.families.flatMap((family) => family.capabilities).find((tool) => tool.name === 'count-by-layer')
  expanded.catalog_version = 'isolated-catalog-expanded-v2'
  expanded.response.families.push({ family_id: 'additional', capabilities: [{ ...readTool, name: 'extra-read-tool' }] })
  const built = buildFeatureMap({ snapshot: expanded })
  assert.ok(built.entries.some((entry) => entry.id === 'tool:extra-read-tool' && entry.catalog_version === expanded.catalog_version))
  assert.equal(checkCompleteness(built, DEFAULT_REGISTRIES, expanded), true)
  const unversioned = clone(snapshot)
  delete unversioned.catalog_version
  assert.throws(() => buildFeatureMap({ snapshot: unversioned }), /catalog_version/)
  const colliding = clone(snapshot)
  colliding.response.families[0].capabilities.push(clone(readTool))
  assert.throws(() => buildFeatureMap({ snapshot: colliding }), /duplicate id|collision/)
})

test('two builds are byte-identical and summary counts exactly partition the map', () => {
  assert.equal(JSON.stringify(buildFeatureMap()), JSON.stringify(buildFeatureMap()))
  assert.deepEqual(ids, [...ids].sort())
  const summary = summarizeFeatureMap(map)
  assert.equal(summary.catalog_version, snapshot.catalog_version)
  assert.equal(summary.total, map.entries.length)
  for (const field of ['by_kind', 'by_certify']) {
    assert.equal(Object.values(summary[field]).reduce((sum, count) => sum + count, 0), map.entries.length)
    for (const [value, count] of Object.entries(summary[field])) {
      assert.equal(count, map.entries.filter((entry) => entry[field === 'by_kind' ? 'kind' : 'certify'] === value).length)
    }
  }
})
