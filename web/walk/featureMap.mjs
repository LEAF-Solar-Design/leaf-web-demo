import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { ACTIONS, ESCAPE_RUNGS, RETRY_RUNGS, REASONS, reasonCode, escapeRung, retryRung } from '../src/lib/actionRegistry.js'
import { PRODUCT_SURFACES, productSurfaceStates } from '../src/site/productSurfaces.js'
import { PROFILE_RIBBON_TABS } from '../src/lib/ribbonTabs.data.js'
import { STUDIO_DRAWERS } from '../src/lib/studioDrawers.js'
import { PROMPTS } from '../src/cadedit/promptKeys.js'
import { isWriteTool, toolMcpSource } from '../src/lib/toolRecord.js'

const readJson = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'))
export const DEFAULT_REGISTRIES = Object.freeze({
  actions: ACTIONS, surfaces: PRODUCT_SURFACES, drawers: STUDIO_DRAWERS,
  tabs: PROFILE_RIBBON_TABS, surfaceStates: productSurfaceStates,
})
export const ID_GRAMMAR = /^(?:(?:action|surface|drawer|tool):[a-z0-9]+(?:-[a-z0-9]+)*|tab:[a-z0-9]+(?:-[a-z0-9]+)*:[a-z0-9]+(?:-[a-z0-9]+)*)$/
const EFFECT_KINDS = new Set(['opens', 'toggles', 'navigates', 'submits', 'disabled_with_reason', 'renders'])
const CERTIFY_CLASSES = new Set(['local', 'staging', 'both', 'unsupported_local'])
const nonempty = (value) => typeof value === 'string' && value.trim().length > 0
const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0

// Normalization preserves namespaces in the source id as kebab segments.
// Any collision is an error, never a silently overwritten inventory row.
export function featureId(kind, sourceId, profile) {
  const kebab = (value) => String(value).replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/[^a-zA-Z0-9]+/g, '-').replace(/^-|-$/g, '').toLowerCase()
  return kind === 'tab' ? `tab:${kebab(profile)}:${kebab(sourceId)}` : `${kind}:${kebab(sourceId)}`
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical)
  if (value && typeof value === 'object') return Object.fromEntries(
    Object.keys(value).sort(compare).map((key) => [key, canonical(value[key])]),
  )
  return value
}

function merge(base, patch) {
  const out = { ...base }
  for (const [key, value] of Object.entries(patch)) {
    out[key] = value && typeof value === 'object' && !Array.isArray(value)
      ? merge(base?.[key] || {}, value) : value
  }
  return out
}

function catalogTools(snapshot) {
  if (snapshot?.snapshot_version !== 1 || !nonempty(snapshot.catalog_version)) {
    throw new Error('featureMap: snapshot requires snapshot_version 1 and a catalog_version')
  }
  if (!Array.isArray(snapshot.response?.families) || !snapshot.response.families.length) {
    throw new Error('featureMap: snapshot requires response.families; an empty catalog cannot certify tools')
  }
  if (!Array.isArray(snapshot.sources) || !snapshot.sources.length || snapshot.sources.some((source) => !nonempty(source))) {
    throw new Error('featureMap: snapshot requires defining sources')
  }
  return snapshot.response.families.flatMap((family) => {
    if (!nonempty(family.family_id) || !Array.isArray(family.capabilities)) {
      throw new Error('featureMap: malformed catalog family')
    }
    return family.capabilities.map((tool) => {
      if (!nonempty(tool.name)) throw new Error('featureMap: catalog tool has no name')
      return { tool, family_id: family.family_id }
    })
  })
}

function inventory(registries, snapshot) {
  return [
    ...registries.actions.map((record) => ({ id: featureId('action', record.id), kind: 'action', record })),
    ...registries.surfaces.map((record) => ({ id: featureId('surface', record.id), kind: 'surface', record })),
    ...registries.drawers.map((record) => ({ id: featureId('drawer', record), kind: 'drawer', record })),
    ...Object.entries(registries.tabs).flatMap(([profile, tabs]) => tabs.map((record) => ({
      id: featureId('tab', record.id, profile), kind: 'tab', record, profile,
    }))),
    ...catalogTools(snapshot).map(({ tool, family_id }) => ({
      id: featureId('tool', tool.name), kind: 'tool', record: tool, family_id,
    })),
  ]
}

export function validateOverrides(config, knownIds) {
  if (config?.version !== 1 || !config.overrides || !Array.isArray(config.exemptions)) {
    throw new Error('featureMap: overrides require version 1, overrides and exemptions')
  }
  const known = new Set(knownIds)
  const exactId = (id, type) => {
    if (!ID_GRAMMAR.test(id) || /[*?\[\]]/.test(id)) {
      throw new Error(`featureMap: ${type} must name exactly one id; wildcard or prefix exemption rejected: ${id}`)
    }
    if (!known.has(id)) throw new Error(`featureMap: ${type} names unknown id (prefix matches are forbidden): ${id}`)
  }
  for (const [id, override] of Object.entries(config.overrides)) {
    exactId(id, 'override')
    if (!override || typeof override !== 'object' || Array.isArray(override)) throw new Error(`featureMap: invalid override ${id}`)
    const allowed = new Set(['title', 'effect', 'sources', 'viewports', 'certify', 'certify_reason'])
    for (const key of Object.keys(override)) {
      if (!allowed.has(key)) throw new Error(`featureMap: unknown override field ${id}.${key}`)
    }
  }
  const exempted = new Set()
  for (const exemption of config.exemptions) {
    exactId(exemption?.id, 'exemption')
    if (!nonempty(exemption.reason)) throw new Error(`featureMap: exemption ${exemption.id} requires a non-empty reason`)
    if (!nonempty(exemption.owner)) throw new Error(`featureMap: exemption ${exemption.id} requires an owner`)
    if (Object.keys(exemption).some((key) => !['id', 'reason', 'owner'].includes(key))) {
      throw new Error(`featureMap: exemption ${exemption.id} supports exact ids only; prefix fields are forbidden`)
    }
    if (exempted.has(exemption.id)) throw new Error(`featureMap: duplicate exemption ${exemption.id}`)
    exempted.add(exemption.id)
  }
  return config
}

function disabled(sentence, code = reasonCode(sentence)) {
  if (!nonempty(code)) throw new Error(`featureMap: disabled reason has no registered code: ${sentence}`)
  return { kind: 'disabled_with_reason', reason_code: code, reason: sentence }
}

function cases(config, kind) {
  const set = config.state_cases?.[kind]
  if (!set?.baseline || !set.patches) throw new Error(`featureMap: missing ${kind} state cases`)
  return [['ready', set.baseline], ...Object.entries(set.patches)
    .map(([state, patch]) => [state, merge(set.baseline, patch)])]
}

function actionEffect(action, ctx, override) {
  const why = action.when(ctx)
  if (why) return disabled(why)
  if (action.id === 'bar:escape') return { kind: 'toggles', target: `escape:${escapeRung(ctx)}` }
  if (action.id === 'bar:retry') return { kind: 'submits', target: `retry:${retryRung(ctx)}` }
  if (override.effect) return override.effect
  if (action.op) return {
    kind: PROMPTS[action.op] ? 'opens' : 'submits',
    target: PROMPTS[action.op] ? 'cockpit-prompt' : `engine:${action.op}`,
    operation: action.op, group: action.group,
  }
  throw new Error(`featureMap: action ${action.id} needs an explicit effect override`)
}

function toolEffect(tool, ctx) {
  // The familyCluster run ladder, using the product's reason vocabulary and
  // write/MCP predicates. ribbonClusters.js itself imports JSX consumers.
  const why = toolMcpSource(tool) ? REASONS.mcpToolNotWired
    : ctx.running ? REASONS.running
      : ctx.previewing ? REASONS.previewing
        : isWriteTool(tool) && ctx.writeLocked ? REASONS.writeLocked
          : isWriteTool(tool) && !ctx.writeEntitled ? REASONS.writeUnentitled
            : isWriteTool(tool) && ctx.engineDirty ? REASONS.unsavedEngineEdits : ''
  return why ? disabled(why) : { kind: 'opens', target: 'catalog-run-decision', tool: tool.name }
}

function addCases(entry, candidates, evaluate, relevant = () => false) {
  const baseline = evaluate(candidates[0][1])
  const signature = JSON.stringify(canonical(baseline))
  for (const [state, context] of candidates) {
    const result = evaluate(context)
    if (state !== 'ready' && JSON.stringify(canonical(result)) === signature && !relevant(context)) continue
    entry.states.push(state)
    entry.expected_effect[state] = result.effect
    entry.state_contexts[state] = context
  }
}

function buildEntry(item, config, snapshot, registries) {
  const { id, kind, record, profile } = item
  const override = config.overrides[id] || {}
  const entry = {
    id, kind, title: override.title || record.label || record.name || String(record),
    source_id: record.id || record.name || record,
    sources: [], states: [], expected_effect: {}, state_contexts: {},
    viewports: record.viewports?.includes('phone') || record.phone === true ? ['desktop', 'phone'] : ['desktop'],
    certify: 'both',
  }
  if (kind === 'action') {
    entry.title = override.title || record.title({})
    entry.sources = ['web/src/lib/actionRegistry.js']
    if (record.op) entry.sources.push('web/src/cadedit/promptKeys.js', 'web/src/cadedit/EngineRibbonClusters.jsx')
    const candidates = cases(config, kind)
    if (record.id === 'bar:retry') {
      for (const target of Object.keys(RETRY_RUNGS)) candidates.push([`retry-${target}`, { ...candidates[0][1], rTarget: target }])
    }
    addCases(entry, candidates, (ctx) => ({ effect: actionEffect(record, ctx, override), title: record.title(ctx) }),
      (ctx) => ctx.session?.selected?.editable === false && !record.when(ctx)
        && !!record.when(merge(ctx, { session: { selected: { type: 'LINE' } } })))
    // Rungs are exported product data; every one must have a reachable case.
    if (record.id === 'bar:escape') {
      for (const rung of ESCAPE_RUNGS) {
        if (!Object.values(entry.expected_effect).some((effect) => effect.target === `escape:${rung.id}`)) {
          throw new Error(`featureMap: missing Escape state case for ${rung.id}`)
        }
      }
    }
  } else if (kind === 'surface') {
    entry.title = override.title || record.title
    entry.sources = ['web/src/site/productSurfaces.js', 'web/src/components/ProductSurfaceTabs.jsx']
    entry.contract = record.contract
    addCases(entry, cases(config, kind), (ctx) => {
      const status = registries.surfaceStates(ctx)[record.id]
      if (!status) throw new Error(`featureMap: no surface state projection for ${record.id}`)
      return { effect: { kind: 'renders', target: `surface:${record.id}`, ground: record.contract.ground, ...status } }
    })
  } else if (kind === 'tool') {
    entry.sources = ['web/walk/fixtures/capabilities.snapshot.json', ...snapshot.sources,
      'web/src/lib/toolRecord.js', 'web/src/lib/ribbonClusters.js']
    entry.catalog_version = snapshot.catalog_version
    entry.tool_version = record.version
    entry.family_id = item.family_id
    addCases(entry, cases(config, kind), (ctx) => ({ effect: toolEffect(record, ctx) }))
  } else if (kind === 'tab') {
    entry.profile = profile
    entry.sources = ['web/src/lib/ribbonTabs.data.js', 'web/src/site/CockpitTopBand.jsx']
    entry.states = [record.reason ? 'unavailable' : 'ready']
    entry.expected_effect[entry.states[0]] = record.reason
      ? disabled(record.reason, `${id}:unavailable`) : { kind: 'renders', target: `ribbon:${profile}:${record.id}` }
  } else {
    entry.sources = ['web/src/lib/studioDrawers.js']
    entry.states = record === 'none' ? ['drawer-open', 'drawers-closed'] : ['closed', 'open']
    for (const state of entry.states) entry.expected_effect[state] = {
      kind: 'toggles', target: id, value: record === 'none' || state === 'open' ? 'none' : record,
    }
  }
  entry.sources = [...new Set([...entry.sources, ...(override.sources || [])])].sort(compare)
  if (Object.keys(override).length || ['action', 'surface', 'tool'].includes(kind)) entry.sources.push('web/walk/features.overrides.json')
  if (override.viewports) entry.viewports = override.viewports
  if (override.certify) entry.certify = override.certify
  if (override.certify_reason !== undefined) entry.certify_reason = override.certify_reason
  if (kind === 'tab' && record.reason && entry.certify === 'both') {
    entry.certify = 'unsupported_local'
    entry.certify_reason = record.reason
  }
  entry.states.sort(compare)
  entry.sources.sort(compare)
  return entry
}

export function validateFeatureMap(map) {
  const seen = new Set()
  for (const entry of map.entries) {
    if (!ID_GRAMMAR.test(entry.id) || entry.id.split(':')[0] !== entry.kind) throw new Error(`featureMap: invalid id ${entry.id}`)
    if (seen.has(entry.id)) throw new Error(`featureMap: duplicate id ${entry.id}`)
    seen.add(entry.id)
    if (!nonempty(entry.title) || !Array.isArray(entry.states) || !entry.states.length
        || new Set(entry.states).size !== entry.states.length) throw new Error(`featureMap: ${entry.id} needs a title and unique states`)
    if (!Array.isArray(entry.sources) || !entry.sources.length || entry.sources.some((source) =>
      !nonempty(source) || source.includes('\\') || source.startsWith('/') || /^[A-Za-z]:/.test(source)
      || source.split('/').includes('..'))) throw new Error(`featureMap: ${entry.id} needs repo-relative sources`)
    if (!CERTIFY_CLASSES.has(entry.certify) || (entry.certify !== 'both' && !nonempty(entry.certify_reason))) {
      throw new Error(`featureMap: ${entry.id} needs a certify class and a reason when not both`)
    }
    if (!Array.isArray(entry.viewports) || !entry.viewports.includes('desktop')
        || entry.viewports.some((value) => !['desktop', 'phone'].includes(value))) throw new Error(`featureMap: invalid viewports for ${entry.id}`)
    if (Object.keys(entry.expected_effect).length !== entry.states.length) throw new Error(`featureMap: ${entry.id} effect/state mismatch`)
    for (const state of entry.states) {
      const effect = entry.expected_effect[state]
      if (!nonempty(state) || !effect || !EFFECT_KINDS.has(effect.kind)
          || (effect.kind === 'disabled_with_reason' ? !nonempty(effect.reason_code) || !nonempty(effect.reason) : !nonempty(effect.target))) {
        throw new Error(`featureMap: ${entry.id}/${state} needs a typed expected_effect with target or reason code`)
      }
    }
  }
  return map
}

// An independent comparison seam: tests can supply fake registries, or remove
// a produced row, without changing the reference inventory being checked.
export function checkCompleteness(map, registries = DEFAULT_REGISTRIES,
  snapshot = readJson('./fixtures/capabilities.snapshot.json')) {
  const expected = inventory(registries, snapshot).map((item) => item.id)
  if (new Set(expected).size !== expected.length) throw new Error('featureMap: registry id collision')
  const actual = map.entries.map((entry) => entry.id)
  const missing = expected.filter((id) => !actual.includes(id))
  const unexpected = actual.filter((id) => !expected.includes(id))
  const duplicates = actual.filter((id, index) => actual.indexOf(id) !== index)
  if (missing.length || unexpected.length || duplicates.length) throw new Error(
    `featureMap: completeness failed; missing [${missing}]; unexpected [${unexpected}]; duplicate [${duplicates}]`,
  )
  return true
}

export function buildFeatureMap({ registries = DEFAULT_REGISTRIES,
  snapshot = readJson('./fixtures/capabilities.snapshot.json'),
  overrides = readJson('./features.overrides.json') } = {}) {
  const items = inventory(registries, snapshot)
  validateOverrides(overrides, items.map((item) => item.id))
  const entries = items.map((item) => buildEntry(item, overrides, snapshot, registries)).sort((a, b) => compare(a.id, b.id))
  const map = canonical({ schema_version: 1, catalog_version: snapshot.catalog_version,
    entries, exemptions: [...overrides.exemptions].sort((a, b) => compare(a.id, b.id)) })
  validateFeatureMap(map)
  checkCompleteness(map, registries, snapshot)
  return map
}

export function summarizeFeatureMap(map) {
  const by_kind = {}
  const by_certify = {}
  for (const entry of map.entries) {
    by_kind[entry.kind] = (by_kind[entry.kind] || 0) + 1
    by_certify[entry.certify] = (by_certify[entry.certify] || 0) + 1
  }
  return canonical({ total: map.entries.length, by_kind, by_certify, catalog_version: map.catalog_version })
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const flag = process.argv[2]
  if (process.argv.length !== 3 || !['--json', '--summary'].includes(flag)) {
    process.stderr.write('Usage: node web/walk/featureMap.mjs --json|--summary\n')
    process.exitCode = 2
  } else {
    const map = buildFeatureMap()
    process.stdout.write(`${JSON.stringify(flag === '--summary' ? summarizeFeatureMap(map) : map, null, 2)}\n`)
  }
}
