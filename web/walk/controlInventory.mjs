import { readFileSync } from 'node:fs'
import { ACTIONS } from '../src/lib/actionRegistry.js'
import { PROFILE_RIBBON_TABS } from '../src/lib/ribbonTabs.data.js'

const text = (value) => typeof value === 'string' && value.trim().length > 0
export function controlNameAttributes(name) {
  const raw_name = String(name || '')
  const match = raw_name.match(/\s*\(unavailable:\s*([\s\S]*)\)\s*$/)
  const label = match ? raw_name.slice(0, match.index) : raw_name
  return { raw_name, name_key: label.replace(/\d+/g, '{n}').replace(/\s+/g, ' ').trim(),
    disabled_reason: match ? match[1].trim() : '' }
}
export const controlKey = ({ scope, role, name }) => JSON.stringify([scope, role, controlNameAttributes(name).name_key])

// The shell reuses registry actions with a few presentation labels. These
// aliases are scoped; unrelated buttons with the same name are never covered.
const shellActionLabels = {
  View: { fit: ['Fit drawing to view'], 'zoom-in': [], 'zoom-out': [] },
  'Quick access': { undo: ['Undo version'], redo: ['Redo version'], 'rail-expand': [] },
}
export function registryCensusMappings(controls, map, { profile, viewport = 'desktop' } = {}) {
  const derived = []
  for (const control of controls) {
    const name = controlNameAttributes(control.name).name_key
    for (const entry of map.entries) {
      if (!entry.viewports?.includes(viewport)) continue
      if (entry.kind === 'tab' && entry.profile === profile && control.role === 'tab'
        && control.scope.endsWith('tablist:"Ribbon"')) {
        const record = PROFILE_RIBBON_TABS[profile]?.find((tab) => tab.id === entry.source_id)
        if (record && name === controlNameAttributes(record.label).name_key) derived.push({ index: control.index, feature_id: entry.id })
      }
      if (entry.kind !== 'action' || control.role !== 'button') continue
      const record = ACTIONS.find((action) => action.id === entry.source_id)
      if (!record) continue
      for (const [toolbar, aliases] of Object.entries(shellActionLabels)) {
        if (!Object.hasOwn(aliases, record.id) || !control.scope.endsWith(`toolbar:${JSON.stringify(toolbar)}`)) continue
        if ([record.label, record.text, ...aliases[record.id]].some((label) => name === controlNameAttributes(label).name_key)) {
          derived.push({ index: control.index, feature_id: entry.id })
        }
      }
    }
  }
  return derived
}
const applies = (row, { state, viewport }) => (!row.states || row.states.includes(state))
  && (!row.viewports || row.viewports.includes(viewport))

export function validateControlInventory(inventory, map) {
  if (inventory?.version !== 1 || !Array.isArray(inventory.mappings) || !Array.isArray(inventory.baseline_unmapped)) {
    throw new Error('controlInventory: requires version 1, mappings and baseline_unmapped')
  }
  const known = new Set(map.entries.map((entry) => entry.id))
  const seen = new Map()
  for (const [kind, rows] of [['mapping', inventory.mappings], ['baseline', inventory.baseline_unmapped]]) {
    for (const row of rows) {
      if (!row || !text(row.scope) || !text(row.role) || typeof row.name !== 'string') {
        throw new Error('controlInventory: row requires scope, role and name')
      }
      const allowed = ['scope', 'role', 'name', 'states', 'viewports', kind === 'mapping' ? 'feature_id' : 'reason']
      if (Object.keys(row).some((key) => !allowed.includes(key))) throw new Error('controlInventory: unknown row field')
      for (const key of ['states', 'viewports']) if (row[key] !== undefined && (!Array.isArray(row[key])
        || !row[key].length || row[key].some((item) => !text(item)) || new Set(row[key]).size !== row[key].length)) {
        throw new Error(`controlInventory: ${key} requires unique non-empty values`)
      }
      if (row.viewports?.some((item) => !['desktop', 'phone'].includes(item))) throw new Error('controlInventory: unknown viewport')
      if (kind === 'mapping' && !known.has(row.feature_id)) throw new Error(`controlInventory: unknown feature id ${row.feature_id}`)
      if (kind === 'baseline' && !text(row.reason)) throw new Error('controlInventory: baseline requires non-empty reason')
      const key = controlKey(row), previous = seen.get(key) || []
      const overlaps = (a, b) => !a || !b || a.some((value) => b.includes(value))
      if (previous.some((other) => overlaps(row.states, other.states) && overlaps(row.viewports, other.viewports))) {
        throw new Error(`controlInventory: duplicate row ${key}`)
      }
      seen.set(key, [...previous, row])
    }
  }
  return inventory
}

export function readControlInventory(map) {
  return validateControlInventory(JSON.parse(readFileSync(new URL('./controlInventory.json', import.meta.url), 'utf8')), map)
}

// derivedMappings come from registry-owned scoped locators, never a name-only
// guess. Indices are observation-local and are not inventory identities.
export function resolveCensus(controls, derivedMappings, map, inventory, { state = 'ready', viewport = 'desktop' } = {}) {
  validateControlInventory(inventory, map)
  const known = new Set(map.entries.map((entry) => entry.id))
  for (const row of derivedMappings) if (!known.has(row.feature_id)) throw new Error(`controlInventory: unknown feature id ${row.feature_id}`)
  const context = { state, viewport }
  const baseline = inventory.baseline_unmapped.filter((row) => applies(row, context))
  const mappings = inventory.mappings.filter((row) => applies(row, context))
  const rendered = controls.filter((row) => row.visible).map((row) => ({ ...row, ...controlNameAttributes(row.name) }))
  const resolved = [], unmapped = [], baselined = [], used = new Set()
  for (const control of rendered) {
    const key = controlKey(control)
    const explicit = mappings.find((row) => controlKey(row) === key)
    const candidates = explicit ? [explicit.feature_id] : [...new Set(derivedMappings
      .filter((row) => row.index === control.index).map((row) => row.feature_id))]
    const row = baseline.find((row) => controlKey(row) === key)
    if (candidates.length === 1) resolved.push({ ...control, feature_id: candidates[0] })
    else if (row) { used.add(row); baselined.push({ ...control, reason: row.reason }) }
    else unmapped.push({ ...control, problem: candidates.length ? 'ambiguous registry mapping' : 'no walk mapping' })
  }
  // A newly mapped row is stale too: baseline coverage can only shrink.
  const stale = baseline.filter((row) => !used.has(row))
  return { ok: !unmapped.length && !stale.length, total: rendered.length, resolved, baselined, unmapped, stale }
}

export function censusFailure(result) {
  const describe = (row) => `scope=${JSON.stringify(row.scope)} role=${JSON.stringify(row.role)} name=${JSON.stringify(row.name)}`
  return ['control-census ratchet failed', ...result.unmapped.map((row) => `unmapped: ${describe(row)} (${row.problem})`),
    ...result.stale.map((row) => `stale baseline: ${describe(row)}`)].join('\n')
}
