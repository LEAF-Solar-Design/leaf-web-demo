import { readFileSync } from 'node:fs'

const text = (value) => typeof value === 'string' && value.trim().length > 0
const unique = (values) => Array.isArray(values) && values.length > 0 && new Set(values).size === values.length
const kinds = new Set(['opens', 'toggles', 'navigates', 'submits', 'disabled_with_reason', 'renders'])

export function validateControlInventory(inventory) {
  if (inventory?.version !== 1 || !Array.isArray(inventory.entries) || !Array.isArray(inventory.presentations)) {
    throw new Error('controlInventory: requires version 1, entries and presentations')
  }
  const ids = new Set(), selectors = new Set()
  for (const row of inventory.entries) {
    if (!/^control:[a-z0-9]+(?:-[a-z0-9]+)*$/.test(row.id) || ids.has(row.id)) throw new Error('controlInventory: invalid or duplicate id')
    ids.add(row.id)
    if (!text(row.title) || !unique(row.states) || row.states.some((state) => !text(state))) throw new Error(`controlInventory: ${row.id} needs title and states`)
    if (!unique(row.sources) || row.sources.some((source) => !text(source) || !/^[A-Za-z0-9_./-]+$/.test(source) || source.includes('\\')
      || source.startsWith('/') || /^[A-Za-z]:/.test(source) || source.split('/').some((part) => ['..', '.', ''].includes(part)))) {
      throw new Error(`controlInventory: ${row.id} needs static repo-relative sources`)
    }
    if (!unique(row.viewports) || !row.viewports.includes('desktop') || row.viewports.some((value) => !['desktop', 'phone'].includes(value))) throw new Error(`controlInventory: ${row.id} invalid viewports`)
    if (!['local', 'staging', 'both', 'unsupported_local'].includes(row.certify)
      || (row.certify !== 'both' && !text(row.certify_reason))) throw new Error(`controlInventory: ${row.id} invalid certification`)
    if (!text(row.locator?.selector) || !text(row.locator.role) || !text(row.locator.name) || !text(row.locator.trigger)) throw new Error(`controlInventory: ${row.id} needs a locator recipe`)
    if (selectors.has(row.locator.selector)) throw new Error('controlInventory: duplicate selector assignment')
    selectors.add(row.locator.selector)
    if (Object.keys(row.expected_effect || {}).length !== row.states.length
      || Object.keys(row.fixture_recipe || {}).length !== row.states.length) throw new Error(`controlInventory: ${row.id} missing state mapping`)
    for (const state of row.states) {
      const effect = row.expected_effect[state], fixture = row.fixture_recipe[state]
      if (!effect || !kinds.has(effect.kind) || (effect.kind === 'disabled_with_reason'
        ? !text(effect.reason) || !text(effect.reason_code) : !text(effect.target))) throw new Error(`controlInventory: ${row.id}/${state} needs typed effect`)
      if (!Array.isArray(fixture?.steps) || !fixture.steps.length || fixture.steps.some((step) => !text(step.kind))) throw new Error(`controlInventory: ${row.id}/${state} needs fixture recipe`)
    }
  }
  for (const row of inventory.presentations) {
    if (!/^(?:action|surface|drawer|tool|control|tab):[a-z0-9:-]+$/.test(row.feature_id)
      || !text(row.selector) || !text(row.state) || !unique(row.viewports)
      || row.viewports.some((value) => !['desktop', 'phone'].includes(value))) throw new Error('controlInventory: malformed presentation')
    if (selectors.has(row.selector)) throw new Error('controlInventory: duplicate selector assignment')
    selectors.add(row.selector)
  }
  return inventory
}

export function readControlInventory() {
  return validateControlInventory(JSON.parse(readFileSync(new URL('./controlInventory.json', import.meta.url), 'utf8')))
}

export function controlEntries(inventory = readControlInventory()) {
  validateControlInventory(inventory)
  return inventory.entries.map((row) => ({ ...structuredClone(row), states: [...row.states].sort(),
    sources: [...new Set([...row.sources, 'web/walk/controlInventory.json'])].sort(),
    kind: 'control', source_id: row.id.slice(8), state_contexts: {} }))
}
