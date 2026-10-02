import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { ACTIONS, ESCAPE_RUNGS, RETRY_RUNGS, reasonCode, REASONS, DRAW_REASONS, MODIFY_REASONS } from '../src/lib/actionRegistry.js'
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

test('every exported action, surface, drawer and profile tab appears exactly once', () => {
  const expected = [
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
    assert.ok(entry.viewports.includes('desktop'))
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
  const config = clone(overrides)
  delete config.overrides['action:fit'].exclude_states
  delete config.overrides['action:fit'].reason
  const baseline = buildFeatureMap({ overrides: config })
  const fit = baseline.entries.find((entry) => entry.id === 'action:fit')
  assert.ok(fit.states.includes('no-drawing'))
  assert.equal(fit.expected_effect['no-drawing'].kind, 'disabled_with_reason')
  assert.ok(fit.state_contexts['no-drawing'])
  const excluded = entryFor('action:fit')
  assert.deepEqual(excluded.states, fit.states.filter((state) => state !== 'no-drawing'))
  const expected = clone(fit)
  expected.states = expected.states.filter((state) => state !== 'no-drawing')
  delete expected.expected_effect['no-drawing']
  delete expected.state_contexts['no-drawing']
  assert.deepEqual(excluded, expected)
  assert.equal(map.entries.length, baseline.entries.length)
  assert.equal(checkCompleteness(map), true)
  config.overrides['drawer:nav'].exclude_states = ['closed']
  config.overrides['drawer:nav'].reason = 'Synthetic exclusion exercises drawer states'
  const drawer = buildFeatureMap({ overrides: config }).entries.find((entry) => entry.id === 'drawer:nav')
  assert.deepEqual(drawer.states, ['open'])
  assert.deepEqual(Object.keys(drawer.expected_effect), ['open'])
})

test('state exclusions refuse unknown states and missing or empty reasons', () => {
  const unknown = clone(overrides)
  unknown.overrides['action:fit'].exclude_states = ['unknown-state']
  assert.throws(() => buildFeatureMap({ overrides: unknown }), /action:fit.*unknown state: unknown-state/)
  for (const reason of [undefined, '', '   ', null]) {
    const config = clone(overrides)
    config.overrides['action:fit'].reason = reason
    assert.throws(() => buildFeatureMap({ overrides: config }), /state exclusion action:fit requires a non-empty reason/)
  }
})

test('state exclusions refuse malformed lists and exclusions that remove every state', () => {
  for (const states of ['no-drawing', [], [''], [null], ['no-drawing', 'no-drawing']]) {
    const config = clone(overrides)
    config.overrides['action:fit'].exclude_states = states
    assert.throws(() => buildFeatureMap({ overrides: config }), /exclude_states requires a non-empty array of unique states/)
  }
  const config = clone(overrides)
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
  assert.equal(entryFor('action:modify-move').expected_effect['read-only-entity'].reason, MODIFY_REASONS.readOnlyKind)
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
    assert.equal(model.certify, 'unsupported_local')
    assert.equal(model.certify_reason, PROFILE_RIBBON_TABS[profile].find((tab) => tab.id === 'model').reason)
  }
  for (const drawer of STUDIO_DRAWERS) {
    assert.ok(entryFor(featureId('drawer', drawer)).viewports.includes('phone'))
  }
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
