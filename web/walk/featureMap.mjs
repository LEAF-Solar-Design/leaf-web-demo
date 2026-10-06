import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import { ACTIONS, ESCAPE_RUNGS, RETRY_RUNGS, REASONS, reasonCode, escapeRung, retryRung } from '../src/lib/actionRegistry.js'
import { PRODUCT_SURFACES, productSurfaceStates } from '../src/site/productSurfaces.js'
import { PROFILE_RIBBON_TABS } from '../src/lib/ribbonTabs.data.js'
import { STUDIO_DRAWERS } from '../src/lib/studioDrawers.js'
import { PROMPTS } from '../src/cadedit/promptKeys.js'
import { isWriteTool, toolMcpSource, toolPlacementTab } from '../src/lib/toolRecord.js'
import { familiesForSurface } from '../src/lib/surfaceRails.js'
import { solarFormKeys, solarView } from '../src/solar/solarView.js'
import { canOpenSolarSettingsForm } from '../src/solar/solarSettingsWire.js'

const readJson = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'))
// The product module imports JSX consumers. Evaluate only its pure exported
// vocabulary and gate, so Node walks reuse the product sentences and predicates.
const ribbonSource = readFileSync(new URL('../src/lib/ribbonClusters.js', import.meta.url), 'utf8')
const solarReasonStart = ribbonSource.indexOf('export const SOLAR_REFUSAL_REASONS =')
const solarReasonEnd = ribbonSource.indexOf('// A Solar run refusal', solarReasonStart)
if (solarReasonStart < 0 || solarReasonEnd < 0) throw new Error('featureMap: Solar reason gate source not found')
const solarRailReason = runInNewContext(ribbonSource.slice(solarReasonStart, solarReasonEnd)
  .replace(/^export /gm, '') + '\nsolarRailReason', {}, { timeout: 1000 })

export function solarRailAvailabilityEffect(tool) {
  if (tool.availability === undefined || solarView(tool).state === 'absent') return null
  // The frozen unscoped catalog asks for a drawing. Resolve that gate after
  // drawing and graph setup, rather than freezing it into the ready oracle.
  if (tool.availability?.refusal_reasons?.includes('drawing_context_required')) return null
  const reason = solarRailReason(tool.availability, {
    openTypedForm: canOpenSolarSettingsForm(tool.name, tool.availability),
  })
  if (!reason) return null
  const codes = tool.availability?.refusal_reasons
  const validAvailability = tool.availability && typeof tool.availability === 'object'
    && !Array.isArray(tool.availability)
  return disabled(reason, Array.isArray(codes) && codes.length
    ? codes.join('; ') : validAvailability ? 'capability_not_ready' : 'capability_availability_unavailable')
}
export const DEFAULT_REGISTRIES = Object.freeze({
  actions: ACTIONS, surfaces: PRODUCT_SURFACES, drawers: STUDIO_DRAWERS,
  tabs: PROFILE_RIBBON_TABS, surfaceStates: productSurfaceStates,
})
export const ID_GRAMMAR = /^(?:(?:action|surface|drawer|tool|control):[a-z0-9]+(?:-[a-z0-9]+)*|tab:[a-z0-9]+(?:-[a-z0-9]+)*:[a-z0-9]+(?:-[a-z0-9]+)*)$/
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

function inventory(registries, snapshot, controls = []) {
  return [
    ...controls.map((record) => ({ id: record.id, kind: 'control', record })),
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
  if (Object.keys(config).some((key) => !['version', 'controls', 'state_cases', 'overrides', 'exemptions'].includes(key))) {
    throw new Error('featureMap: unknown configuration field')
  }
  validateControls(config.controls === undefined ? [] : config.controls)
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
    const allowed = new Set(['title', 'effect', 'sources', 'viewports', 'state_viewports', 'certify', 'certify_reason', 'exclude_states', 'reason'])
    for (const key of Object.keys(override)) {
      if (!allowed.has(key)) throw new Error(`featureMap: unknown override field ${id}.${key}`)
    }
    if (override.exclude_states !== undefined) {
      if (!Array.isArray(override.exclude_states) || !override.exclude_states.length
          || override.exclude_states.some((state) => !nonempty(state))
          || new Set(override.exclude_states).size !== override.exclude_states.length) {
        throw new Error(`featureMap: ${id} exclude_states requires a non-empty array of unique states`)
      }
      if (!nonempty(override.reason)) throw new Error(`featureMap: state exclusion ${id} requires a non-empty reason`)
    } else if (override.reason !== undefined) {
      throw new Error(`featureMap: ${id} reason requires exclude_states`)
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

const BASELINE_THREE_CONTROLS = Object.freeze({
  "scope-add": {
    "kind": "opens",
    "target": "scope-build-picker",
    "states": [
      "ready",
      "failed-load"
    ],
    "role": "button",
    "interaction": "click"
  },
  "demo-return": {
    "kind": "renders",
    "target": "guided-demo",
    "states": [
      "failed-load"
    ],
    "role": "button",
    "interaction": "click"
  },
  "claude-accounts": {
    "kind": "opens",
    "target": "claude-accounts-panel",
    "states": [
      "ready",
      "failed-load"
    ],
    "role": "button",
    "interaction": "click",
    "expanded": false
  },
  "drawing-close-start": {
    "kind": "opens",
    "target": "project-board",
    "states": [
      "ready"
    ],
    "role": "button",
    "interaction": "click"
  },
  "notification-collapse": {
    "kind": "renders",
    "target": "notification-inbox-collapsed",
    "states": [
      "ready",
      "failed-load"
    ],
    "role": "button",
    "interaction": "click",
    "expanded": true
  },
  "session-details": {
    "kind": "opens",
    "target": "session-provenance",
    "states": [
      "ready",
      "failed-load"
    ],
    "role": "button",
    "interaction": "click"
  },
  "version-history": {
    "kind": "opens",
    "target": "version-history",
    "states": [
      "ready"
    ],
    "role": "button",
    "interaction": "click",
    "expanded": false
  },
  "linked-services": {
    "kind": "opens",
    "target": "linked-services-panel",
    "states": [
      "ready",
      "failed-load"
    ],
    "role": "button",
    "interaction": "click",
    "expanded": false
  },
  "project-board": {
    "kind": "opens",
    "target": "project-board",
    "states": [
      "ready",
      "failed-load"
    ],
    "role": "button",
    "interaction": "click"
  },
  "prompt-run": {
    "kind": "submits",
    "target": "unknown-tool-resolver",
    "states": [
      "ready",
      "failed-load"
    ],
    "role": "button",
    "interaction": "click"
  },
  "prompt-scope": {
    "kind": "opens",
    "target": "scope-picker",
    "states": [
      "ready",
      "failed-load"
    ],
    "role": "button",
    "interaction": "click",
    "expanded": false
  },
  "sign-out": {
    "kind": "renders",
    "target": "signed-out-session",
    "states": [
      "ready",
      "failed-load"
    ],
    "role": "button",
    "interaction": "click"
  },
  "start-board": {
    "kind": "opens",
    "target": "project-board",
    "states": [
      "ready",
      "failed-load"
    ],
    "role": "button",
    "interaction": "click"
  },
  "take-edit-lock": {
    "kind": "renders",
    "target": "edit-lock-held",
    "states": [
      "ready"
    ],
    "role": "button",
    "interaction": "click"
  },
  "cost-panel": {
    "kind": "opens",
    "target": "cost-panel",
    "states": [
      "ready",
      "failed-load"
    ],
    "role": "button",
    "interaction": "click",
    "expanded": false
  },
  "command-bar": {
    "kind": "opens",
    "target": "tool-commands",
    "states": [
      "ready",
      "failed-load"
    ],
    "role": "combobox",
    "interaction": "type",
    "inputValue": "/",
    "expanded": false
  },
  "find-drawing": {
    "kind": "renders",
    "target": "drawing-find-no-match",
    "states": [
      "ready"
    ],
    "role": "combobox",
    "interaction": "fill-enter",
    "inputValue": "w1k-absent-object-7f942",
    "expanded": false
  }
})

function validateControls(controls) {
  if (!Array.isArray(controls)) throw new Error('featureMap: controls must be an array')
  const seen = new Set()
  const allowed = new Set(['id', 'source_id', 'title', 'sources', 'states', 'state_contexts', 'expected_effect', 'viewports', 'certify', 'certify_reason'])
  for (const record of controls) {
    if (!record || typeof record !== 'object' || Array.isArray(record)
        || Object.keys(record).some((key) => !allowed.has(key))) throw new Error('featureMap: unknown or malformed control declaration')
    if (!nonempty(record.source_id) || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(record.source_id)
        || record.id !== featureId('control', record.source_id)
        || !ID_GRAMMAR.test(record.id)) throw new Error('featureMap: control requires an exact id and source_id')
    if (seen.has(record.id)) throw new Error('featureMap: duplicate control id ' + record.id)
    seen.add(record.id)
    validateFeatureMap({ entries: [{ ...record, kind: 'control' }] })
    if (!record.state_contexts || Object.keys(record.state_contexts).length !== record.states.length
        || record.states.some((state) => !record.state_contexts[state]
          || typeof record.state_contexts[state] !== 'object' || Array.isArray(record.state_contexts[state]))) {
      throw new Error('featureMap: ' + record.id + ' context/state mismatch')
    }
    for (const state of record.states) {
      const effect = record.expected_effect[state]
      const context = record.state_contexts[state]
      const contextFields = ['toolbar', 'complementary', 'group', 'document', 'name', 'namePolicy',
        'tooltip', 'description', 'failedLoad', 'pressed', 'fullscreen', 'expanded', 'visible',
        'role', 'interaction', 'inputValue']
      const scopes = [context.toolbar !== undefined, context.complementary !== undefined, context.document !== undefined]
      if (scopes.filter(Boolean).length !== 1
          || (context.toolbar !== undefined && !nonempty(context.toolbar))
          || (context.complementary !== undefined && context.complementary !== 'Properties')
          || (context.document !== undefined && context.document !== true)
          || (context.group !== undefined && (context.toolbar !== 'Drafting tools' || !['Layers', 'Script'].includes(context.group)))
          || !nonempty(context.name)
          || Object.keys(context).some((key) => !contextFields.includes(key))
          || ['failedLoad', 'pressed', 'fullscreen', 'expanded', 'visible'].some((key) => context[key] !== undefined && typeof context[key] !== 'boolean')
          || (context.namePolicy !== undefined && (context.namePolicy !== 'count'
            || !['Panels {n}', 'Walk {n}', 'Expand the job monitor ({n} live)', 'Linked services {n} linked'].includes(context.name)))
          || (context.name.includes('{n}') && context.namePolicy !== 'count')) {
        throw new Error('featureMap: invalid control context ' + record.id + '/' + state)
      }
      const contract = Object.hasOwn(BASELINE_THREE_CONTROLS, record.source_id) ? BASELINE_THREE_CONTROLS[record.source_id] : undefined
      if (contract) {
        if (record.states.length !== contract.states.length || record.states.some((value) => !contract.states.includes(value))
            || ['role', 'interaction'].some((field) => context[field] !== undefined && !nonempty(context[field]))
            || context.document !== true || context.failedLoad !== (state === 'failed-load')
            || (context.role || 'button') !== contract.role || (context.interaction || 'click') !== contract.interaction
            || context.inputValue !== contract.inputValue || context.expanded !== contract.expanded
            || effect.kind !== contract.kind || effect.target !== contract.target || effect.value !== undefined
            || context.namePolicy !== (record.source_id === 'linked-services' ? 'count' : undefined)) {
          throw new Error('featureMap: invalid baseline-three control contract ' + record.id + '/' + state)
        }
      } else if (['ribbon-script', 'choose-script', 'run-script'].includes(record.source_id)) {
        const input = record.source_id === 'ribbon-script'
        if (context.toolbar !== 'Drafting tools' || context.group !== 'Script'
            || (context.role || 'button') !== (input ? 'textbox' : 'button')
            || (context.interaction || 'click') !== (input ? 'type' : 'click')
            || context.inputValue !== (input ? 'line 0,0 10,10' : undefined)
            || context.failedLoad !== (state === 'failed-load')) {
          throw new Error('featureMap: invalid Script control contract ' + record.id + '/' + state)
        }
      } else if (['role', 'interaction', 'inputValue'].some((field) => context[field] !== undefined)
          || context.name === 'Linked services {n} linked') {
        throw new Error('featureMap: unsupported control interaction ' + record.id + '/' + state)
      }
      if (effect.kind === 'disabled_with_reason') {
        const scriptLock = ['ribbon-script', 'choose-script'].includes(record.source_id) && state === 'running'
        if (scriptLock ? effect.reason !== 'a script is running' || context.name !== record.title
          || context.tooltip !== (record.source_id === 'choose-script' ? 'A script is running; wait before choosing another script.' : undefined)
          : !nonempty(context.tooltip) || (context.description !== undefined
          ? context.description !== effect.reason || context.name !== record.title
          : context.name !== record.title + ' (unavailable: ' + effect.reason + ')')) {
          throw new Error('featureMap: control disabled evidence mismatch ' + record.id + '/' + state)
        }
      } else if (context.name !== record.title) {
        throw new Error('featureMap: control accessible name mismatch ' + record.id + '/' + state)
      }
      const section = /^properties-(drawing|layers|plan|selection)-section$/.test(effect.target)
      const layer = /^layer-(panels|walk)-visible$/.test(effect.target)
      const overview = ['viewer-overview-pan', 'drawing-overview-expanded'].includes(effect.target)
      if ((section && context.complementary !== 'Properties')
          || (layer && !(context.complementary === 'Properties' && context.namePolicy === 'count'
            || context.toolbar === 'Drafting tools' && context.group === 'Layers' && context.namePolicy === undefined))
          || (overview && context.document !== true)
          || (effect.target === 'job-monitor' && (context.toolbar !== 'Job monitor'
            || context.group !== undefined || context.expanded !== false || effect.kind !== 'opens'))
          || (record.source_id === 'properties-close' && (context.complementary !== 'Properties'
            || context.visible !== true || effect.kind !== 'renders' || effect.value !== false))) {
        throw new Error('featureMap: control scope or initial effect mismatch ' + record.id + '/' + state)
      }
      if (effect.kind === 'toggles') {
        const initial = effect.target === 'drafting-grid' || /^engine-mode:(ortho|osnap)$/.test(effect.target) ? context.pressed
          : effect.target === 'document-fullscreen' ? context.fullscreen
            : /^properties-(drawing|layers|plan|selection)-section$/.test(effect.target)
              || ['drawing-overview-expanded', 'dxf-import-expanded', 'ribbon-overflow-expanded', 'drawing-objects-expanded'].includes(effect.target) ? context.expanded
              : /^layer-(panels|walk)-visible$/.test(effect.target) ? context.visible : undefined
        if (typeof initial !== 'boolean' || typeof effect.value !== 'boolean' || effect.value === initial) {
          throw new Error('featureMap: control toggle needs opposite setup and effect states ' + record.id + '/' + state)
        }
      }
      const fields = effect.kind === 'disabled_with_reason' ? ['kind', 'reason', 'reason_code'] : ['kind', 'target', 'value']
      if (Object.keys(effect).some((key) => !fields.includes(key))) throw new Error('featureMap: unknown control effect field ' + record.id)
      if (effect.kind === 'disabled_with_reason' && effect.reason_code !== record.id + ':unavailable') {
        throw new Error('featureMap: invalid control reason code ' + record.id)
      }
    }
  }
  return controls
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

function toolEffect(tool, ctx, solarEditor = false) {
  // The familyCluster run ladder, using the product's reason vocabulary and
  // write/MCP predicates. ribbonClusters.js itself imports JSX consumers.
  const why = toolMcpSource(tool) ? REASONS.mcpToolNotWired
    : ctx.running ? REASONS.running
      : ctx.previewing ? REASONS.previewing
        : isWriteTool(tool) && ctx.writeLocked ? REASONS.writeLocked
          : isWriteTool(tool) && !ctx.writeEntitled ? REASONS.writeUnentitled
            : isWriteTool(tool) && ctx.engineDirty ? REASONS.unsavedEngineEdits : ''
  if (why) return disabled(why)
  // Catalog run decisions retain their drawing-scoped unsupported_local path.
  // Editors bypass that run path, so their rail refusal is the UI oracle.
  const unavailable = solarEditor ? solarRailAvailabilityEffect(tool) : null
  if (unavailable) return unavailable
  return { kind: 'opens', target: solarEditor ? 'solar-step-editor' : 'catalog-run-decision', tool: tool.name }
}

export function solarToolReadyEffect(tool, familyId, families) {
  return toolEffect(tool, { writeEntitled: true }, solarToolEditor(tool, familyId, families))
}

function solarToolEditor(tool, familyId, families) {
  const { view } = solarView(tool)
  return !toolPlacementTab(tool)
    && !familiesForSurface(families, 'solar').some((family) => family.family_id === familyId)
    && view?.interaction.mode === 'form'
    && solarFormKeys({ ...tool, params: tool.params || tool.params_schema }, view).length > 0
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
  if (kind === 'control') {
    Object.assign(entry, structuredClone(record), { kind })
    entry.sources.push('web/walk/features.overrides.json')
  } else if (kind === 'action') {
    entry.title = override.title || record.title({})
    entry.sources = ['web/src/lib/actionRegistry.js']
    if (record.op) entry.sources.push('web/src/cadedit/promptKeys.js', 'web/src/cadedit/EngineRibbonClusters.jsx')
    if (['copyClip', 'cutClip', 'explode'].includes(record.op)) {
      entry.sources.push('web/src/cadedit/engineSession.js')
      if (record.op !== 'explode') entry.sources.push('web/src/cadedit/clipboard.js')
    }
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
    const solarEditor = solarToolEditor(record, item.family_id, snapshot.response.families)
    entry.sources.push('web/src/lib/surfaceRails.js', 'web/src/solar/solarView.js', 'web/src/solar/solarSettingsWire.js')
    addCases(entry, cases(config, kind), (ctx) => ({ effect: toolEffect(record, ctx, solarEditor) }))
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
  for (const state of override.exclude_states || []) {
    if (!entry.states.includes(state)) throw new Error(`featureMap: state exclusion ${id} names unknown state: ${state}`)
  }
  if (override.exclude_states) {
    entry.states = entry.states.filter((state) => !override.exclude_states.includes(state))
    for (const state of override.exclude_states) {
      delete entry.expected_effect[state]
      delete entry.state_contexts[state]
    }
  }
  entry.sources = [...new Set([...entry.sources, ...(override.sources || [])])].sort(compare)
  if (Object.keys(override).length || ['action', 'surface', 'tool'].includes(kind)) entry.sources.push('web/walk/features.overrides.json')
  if (override.viewports) entry.viewports = override.viewports
  if (override.state_viewports !== undefined) entry.state_viewports = structuredClone(override.state_viewports)
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
    if (!Array.isArray(entry.viewports) || !entry.viewports.length
        || entry.viewports.some((value) => !['desktop', 'phone'].includes(value))) throw new Error(`featureMap: invalid viewports for ${entry.id}`)
    if (entry.state_viewports !== undefined) {
      if (!entry.state_viewports || typeof entry.state_viewports !== 'object' || Array.isArray(entry.state_viewports)
          || Object.entries(entry.state_viewports).some(([state, viewports]) => !entry.states.includes(state)
            || !Array.isArray(viewports) || !viewports.length || new Set(viewports).size !== viewports.length
            || viewports.some((viewport) => !['desktop', 'phone'].includes(viewport)))) {
        throw new Error(`featureMap: invalid state_viewports for ${entry.id}`)
      }
    }
    if (!entry.expected_effect || typeof entry.expected_effect !== 'object' || Array.isArray(entry.expected_effect)
        || Object.keys(entry.expected_effect).length !== entry.states.length) throw new Error(`featureMap: ${entry.id} effect/state mismatch`)
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
  snapshot = readJson('./fixtures/capabilities.snapshot.json'),
  controls = readJson('./features.overrides.json').controls || []) {
  validateControls(controls)
  const expected = inventory(registries, snapshot, controls).map((item) => item.id)
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
  validateControls(overrides.controls === undefined ? [] : overrides.controls)
  const items = inventory(registries, snapshot, overrides.controls || [])
  validateOverrides(overrides, items.map((item) => item.id))
  const entries = items.map((item) => buildEntry(item, overrides, snapshot, registries)).sort((a, b) => compare(a.id, b.id))
  const map = canonical({ schema_version: 1, catalog_version: snapshot.catalog_version,
    entries, exemptions: [...overrides.exemptions].sort((a, b) => compare(a.id, b.id)) })
  validateFeatureMap(map)
  checkCompleteness(map, registries, snapshot, overrides.controls || [])
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
