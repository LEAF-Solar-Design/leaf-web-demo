import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { readControlInventory, validateControlInventory, controlEntries } from './controlInventory.mjs'
import { resolveCensus } from '../e2e/walk/controlCensus.mjs'
import { buildFeatureMap, checkCompleteness } from './featureMap.mjs'

const inventory = readControlInventory()
const fixture = (name) => JSON.parse(readFileSync(new URL(`./fixtures/control-census.${name}.json`, import.meta.url), 'utf8'))

test('all supplemental rows carry static, state-complete contracts', () => {
  assert.equal(validateControlInventory(inventory), inventory)
  const map = buildFeatureMap()
  for (const row of inventory.entries) {
    const entry = map.entries.find((entry) => entry.id === row.id)
    assert.ok(entry)
    assert.ok(entry.sources.includes('web/walk/controlInventory.json'))
    assert.deepEqual(Object.keys(entry.expected_effect).sort(), [...entry.states].sort())
    assert.deepEqual(Object.keys(entry.fixture_recipe).sort(), [...entry.states].sort())
    assert.throws(() => checkCompleteness({ ...map, entries: map.entries.filter((entry) => entry.id !== row.id) }), /completeness failed/)
  }
  for (const suffix of ['x', 'y', 'x2', 'y2', 'layer', 'run', 'cancel', 'ortho', 'osnap']) {
    assert.ok(inventory.entries.some((row) => row.id === `control:prompt-${suffix}`))
  }
})

test('absolute, traversal and URI sources are refused', () => {
  for (const source of ['../secret.js', 'web/../secret.js', '/tmp/file', 'C:/file', 'web\\file.js', 'https://example/file', 'web/./file.js']) {
    const copy = structuredClone(inventory)
    copy.entries[0].sources = [source]
    assert.throws(() => validateControlInventory(copy), /static repo-relative sources/)
  }
})

test('duplicate selectors, unknown effects and absent state recipes fail', () => {
  for (const mutate of [
    (copy) => { copy.entries[1].locator.selector = copy.entries[0].locator.selector },
    (copy) => { copy.presentations[0].selector = copy.entries[0].locator.selector },
    (copy) => { delete copy.entries[0].fixture_recipe.valid },
    (copy) => { delete copy.entries[0].expected_effect.valid },
    (copy) => { copy.entries[0].expected_effect.valid.kind = 'shell' },
    (copy) => { copy.entries[0].viewports = ['tablet'] },
  ]) {
    const copy = structuredClone(inventory)
    mutate(copy)
    assert.throws(() => validateControlInventory(copy), /controlInventory:/)
  }
})

test('good census includes disabled controls and explicitly records hidden and inert exclusions', () => {
  const good = fixture('good')
  const result = resolveCensus(good.controls, good.mappings, good.map)
  assert.equal(result.ok, true)
  assert.equal(result.total, 1)
  assert.equal(result.resolved[0].disabled, true)
  assert.deepEqual(result.excluded, [{ index: 1, reason: 'hidden' }, { index: 2, reason: 'inert' }])
  const bad = fixture('bad')
  assert.equal(resolveCensus(bad.controls, bad.mappings, bad.map).errors[0].reason, 'missing inventory mapping')
})

test('unknown IDs, ambiguous matches, duplicate assignments and missing states fail closed', () => {
  for (const [mutate, reason] of [
    [(copy) => { copy.mappings[0].featureId = 'control:unknown' }, 'unknown feature ID'],
    [(copy) => { copy.mappings[0].state = 'absent' }, 'unsupported state or viewport mapping'],
    [(copy) => { copy.mappings[0].viewports = ['phone'] }, 'unsupported state or viewport mapping'],
    [(copy) => { copy.mappings.push({ ...copy.mappings[0], key: '#second' }) }, 'ambiguous mapping'],
  ]) {
    const copy = fixture('good')
    mutate(copy)
    assert.equal(resolveCensus(copy.controls, copy.mappings, copy.map).errors[0].reason, reason)
  }
  const duplicate = fixture('good')
  duplicate.mappings.push(duplicate.mappings[0])
  assert.throws(() => resolveCensus(duplicate.controls, duplicate.mappings, duplicate.map), /duplicate selector assignment/)
})

test('repeated presentations require explicit permission', () => {
  const copy = fixture('good')
  copy.controls[1].visible = true
  copy.controls[1].disabled = true
  copy.mappings[0].indices.push(1)
  assert.equal(resolveCensus(copy.controls, copy.mappings, copy.map).ok, false)
  copy.mappings[0].allowRepeat = true
  assert.equal(resolveCensus(copy.controls, copy.mappings, copy.map).ok, true)
})

test('a disabled control cannot claim an enabled state', () => {
  const copy = fixture('good')
  copy.map.entries[0].expected_effect.ready = { kind: 'renders', target: 'enabled-effect' }
  const result = resolveCensus(copy.controls, copy.mappings, copy.map)
  assert.equal(result.ok, false)
  assert.equal(result.errors[0].reason, 'rendered disabled state does not match inventory state')
})

test('DOM labels cannot change trusted IDs, sources or fixture recipes', () => {
  const copy = fixture('good')
  copy.controls[0].label = 'C:/tmp/not-a-source.js; echo untrusted'
  const before = JSON.stringify(inventory)
  assert.equal(resolveCensus(copy.controls, copy.mappings, copy.map).resolved[0].featureId, 'control:known')
  assert.equal(JSON.stringify(inventory), before)
  assert.deepEqual(controlEntries(inventory), controlEntries(inventory))
})
