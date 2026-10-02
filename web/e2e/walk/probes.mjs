import { ACTIONS, REASONS, accessibleName, reasonCode } from '../../src/lib/actionRegistry.js'
import { PRODUCT_SURFACES } from '../../src/site/productSurfaces.js'
import { PROFILE_RIBBON_TABS } from '../../src/lib/ribbonTabs.data.js'
import { STUDIO_DRAWERS } from '../../src/lib/studioDrawers.js'
import { PROMPTS } from '../../src/cadedit/promptKeys.js'
import { readFileSync } from 'node:fs'
import { controlKey } from '../../walk/controlInventory.mjs'

const catalog = JSON.parse(readFileSync(new URL('../../walk/fixtures/capabilities.snapshot.json', import.meta.url), 'utf8'))
const tools = catalog.response.families.flatMap((family) => family.capabilities)
const drawerNames = Object.freeze({ nav: 'Catalog', jobs: 'Jobs', result: 'Result', plan: 'Plan', none: 'Catalog' })
const profileSurfaces = Object.freeze({ drafting: 'cad', solar: 'solar', project: 'browser', ship: 'ios' })
const effects = new Set(['opens', 'toggles', 'navigates', 'submits', 'disabled_with_reason', 'renders'])
const step = (kind, properties = {}) => ({ kind, ...properties })
const role = (name, accessible, scope) => ({ role: name, name: accessible, exact: true, ...(scope ? { scope } : {}) })
const escapePattern = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

// Independent presence obligations: retiring a baseline must not bless a control that disappeared.
export const CONTROL_CENSUS_BATCH = Object.freeze([
  { feature_id: 'control:grid-display', scope: 'toolbar:"Drafting settings"', role: 'button', name: 'Grid display',
    states: ["ready","failed-load"], viewports: ['desktop'] },
  { feature_id: 'control:object-snap', scope: 'toolbar:"Drafting settings"', role: 'button', name: 'Object snap',
    states: ["ready","failed-load"], viewports: ['desktop'] },
  { feature_id: 'control:ortho-mode', scope: 'toolbar:"Drafting settings"', role: 'button', name: 'Ortho mode',
    states: ["ready","failed-load"], viewports: ['desktop'] },
  { feature_id: 'control:polar-tracking', scope: 'toolbar:"Drafting settings"', role: 'button', name: 'Polar tracking',
    states: ["ready","failed-load"], viewports: ['desktop'] },
  { feature_id: 'control:snap-mode', scope: 'toolbar:"Drafting settings"', role: 'button', name: 'Snap mode',
    states: ["ready","failed-load"], viewports: ['desktop'] },
  { feature_id: 'control:fullscreen', scope: 'toolbar:"Drafting settings"', role: 'button', name: 'Toggle fullscreen',
    states: ["ready","failed-load"], viewports: ['desktop'] },
  { feature_id: 'control:view-back', scope: 'toolbar:"View"', role: 'button', name: 'Back to the previous view',
    states: ["ready"], viewports: ['desktop'] },
  { feature_id: 'control:view-up', scope: 'toolbar:"View"', role: 'button', name: 'Up one level',
    states: ["ready"], viewports: ['desktop'] },
  { feature_id: 'control:new-drawing', scope: 'toolbar:"Quick access"', role: 'button', name: 'New drawing',
    states: ["ready","failed-load"], viewports: ['desktop'] },
  { feature_id: 'control:print', scope: 'toolbar:"Quick access"', role: 'button', name: 'Print',
    states: ["ready","failed-load"], viewports: ['desktop'] },
])

export function requireControlCensusBatch(census, { state = 'ready', viewport = 'desktop' } = {}) {
  for (const expected of CONTROL_CENSUS_BATCH) {
    if (!expected.states.includes(state) || !expected.viewports.includes(viewport)) continue
    const matches = census.resolved.filter((row) => controlKey(row) === controlKey(expected))
    if (matches.length !== 1 || matches[0].feature_id !== expected.feature_id) {
      throw new Error('control-census: required batch control missing or misresolved: ' + expected.feature_id)
    }
  }
  return true
}

function actionRecord(entry) {
  const record = ACTIONS.find((action) => action.id === entry.source_id)
  if (!record) throw new Error(`No action registry record for ${entry.id}`)
  return record
}

export function locatorRecipe(entry, state) {
  const effect = entry.expected_effect[state]
  if (entry.kind === 'control') {
    const context = entry.state_contexts[state]
    return { ...role('button', context.name, role('toolbar', context.toolbar)), trigger: 'click',
      tooltip: context.tooltip, description: context.description }
  }
  if (entry.kind === 'action') {
    const action = actionRecord(entry)
    const why = effect.kind === 'disabled_with_reason' ? effect.reason : ''
    if (action.surface === 'bar') {
      // The registry declares keyboard-only actions. Their reachable control
      // is the command bar; do not invent a clickable Escape or Retry button.
      return { ...role('combobox', 'Command bar'), trigger: 'keyboard', key: action.kbd, actionLabel: action.label }
    }
    if (action.surface === 'slash') return { ...role('option', new RegExp(`^/${action.label}(?:\\s|$)`)), trigger: 'click' }
    const property = ['setColor', 'setLinetype', 'setLineweight'].includes(action.op)
    if (state === 'no-drawing') {
      const labels = [...new Set([action.label, action.text].filter(Boolean))]
      const disabledVariants = [{ name: accessibleName(property ? action.text : action.label, why), reason: why }]
      if (action.surface === 'engine') {
        disabledVariants.push({ name: accessibleName(action.text, why), reason: why })
        // referencePanels uses this reason for these registry panels and Text.
        // Keep its JSX consumers out of the plain-Node probe module.
        const fallback = ['block', 'properties', 'groups', 'clipboard'].includes(action.panel)
          || (action.panel === 'annotation' && action.op === 'createText')
        if (fallback) {
          disabledVariants.push({ name: accessibleName(action.text, REASONS.notInEngine), reason: REASONS.notInEngine })
        }
      }
      return { ...role(property ? 'combobox' : 'button',
        new RegExp(`^(?:${disabledVariants.map((variant) => escapePattern(variant.name)).join('|')})$`),
        role('toolbar', 'Drafting tools')), trigger: property ? 'select' : 'click',
      availableName: property ? action.text : action.label,
      unavailableName: new RegExp(`^(?:${labels.map(escapePattern).join('|')})(?: \\(unavailable: .+\\))?$`),
      disabledVariants: disabledVariants.map((variant) => ({ ...variant, reason_code: reasonCode(variant.reason) })),
      ...(action.panel || action.cluster || action.group ? { group: action.panel || action.cluster || action.group } : {}) }
    }
    return { ...role(property ? 'combobox' : 'button', accessibleName(property ? action.text : action.label, why),
      role('toolbar', 'Drafting tools')), trigger: property ? 'select' : 'click',
    ...(state === 'no-drawing' ? { availableName: property ? action.text : action.label } : {}),
    ...(action.panel || action.cluster || action.group ? { group: action.panel || action.cluster || action.group } : {}) }
  }
  if (entry.kind === 'surface') {
    const record = PRODUCT_SURFACES.find((surface) => surface.id === entry.source_id)
    if (!record) throw new Error(`No surface registry record for ${entry.id}`)
    // Sheets declares no studio tab. Its accessible page landmark is the
    // locator; the setup recipe follows the public /sheets route.
    return record.contract.chrome.tab ? { ...role('tab', record.label, role('tablist', 'Workspace profile')), trigger: 'click' }
      : { ...role('main', ''), trigger: 'navigate', url: '/sheets' }
  }
  if (entry.kind === 'tab') {
    const record = PROFILE_RIBBON_TABS[entry.profile]?.find((tab) => tab.id === entry.source_id)
    if (!record) throw new Error(`No ribbon registry record for ${entry.id}`)
    return { ...role('tab', accessibleName(record.label, effect.kind === 'disabled_with_reason' ? effect.reason : ''),
      role('tablist', 'Ribbon')), trigger: 'click' }
  }
  if (entry.kind === 'drawer') {
    if (!STUDIO_DRAWERS.includes(entry.source_id)) throw new Error(`No drawer registry record for ${entry.id}`)
    if (entry.source_id === 'nav') return {
      ...role('button', state === 'open' ? 'Collapse the tool rail to a spine' : 'Tool rail'), trigger: 'click',
      phone: { ...role('button', 'Tool rail'), trigger: 'click' },
    }
    return { ...role('button', drawerNames[entry.source_id], role('group', 'Workspace panels')),
      trigger: entry.source_id === 'none' ? 'keyboard' : 'click', key: 'Escape' }
  }
  if (entry.kind === 'tool') {
    const record = tools.find((tool) => tool.name === entry.source_id)
    if (!record) throw new Error(`No catalog registry record for ${entry.id}`)
    return { ...role('button', accessibleName(record.name,
      effect.kind === 'disabled_with_reason' ? effect.reason : ''), role('toolbar', 'Drafting tools')), trigger: 'click' }
  }
  throw new Error(`Unknown feature kind: ${entry.kind}`)
}

function actionTab(action) {
  if (['annotation', 'block'].includes(action.panel)) return action.panel === 'annotation' ? 'Annotate' : 'Insert'
  if (['view', 'version'].includes(action.cluster)) return 'View'
  return 'Draw'
}

// These are recipes, not projected UI state. The executor must perform and
// verify each step against the isolated product; it never assigns React state.
export function stateRecipe(entry, state) {
  const context = structuredClone(entry.state_contexts?.[state] || {})
  const effect = entry.expected_effect[state]
  const surface = entry.kind === 'tab' ? profileSurfaces[entry.profile]
    : entry.kind === 'surface' ? entry.source_id : 'cad'
  const steps = []
  if (entry.kind === 'control') {
    steps.push(context.failedLoad
      ? step('open-failed-drawing', { url: '/app?surface=cad&drawing=missing.invalid' })
      : step('open-private-drawing', { surface: 'cad' }))
    const locator = locatorRecipe(entry, state)
    if (effect.target === 'drafting-grid') steps.push(step('control-pressed-state', { control: locator, pressed: context.pressed }))
    if (effect.target === 'document-fullscreen') steps.push(step('fullscreen-state', { control: locator, fullscreen: context.fullscreen }))
    if (entry.source_id === 'view-back') steps.push(step(state === 'empty-history' ? 'empty-view-history' : 'previous-view-history'))
    if (entry.source_id === 'view-up') steps.push(step('whole-drawing-view'))
    return { context, steps }
  }
  if (surface === 'sheets') return { context, steps: [step('navigate', { url: '/sheets' })] }
  if (state === 'no-drawing' && entry.kind === 'action') {
    return { context, steps: [
      step('open-failed-drawing', { url: '/app?surface=cad&drawing=missing.invalid' }),
      step('failed-drawing-ribbon-tab', { name: actionTab(actionRecord(entry)) }),
    ] }
  }
  if (state === 'engine-busy') steps.push(step('prepare-engine-transport'))
  if (state === 'engine-not-parsed') steps.push(step('hold-engine-boot'))
  const empty = ['no-drawing', 'signed-out', 'no-versioned-drawing'].includes(state)
  steps.push(step(empty ? 'open-empty-workspace' : 'open-private-drawing', { surface, signedOut: state === 'signed-out' }))
  if (entry.kind === 'action') {
    const action = actionRecord(entry)
    if (action.surface === 'slash') steps.push(step('slash-menu', { command: action.label }))
    else if (action.surface !== 'bar') steps.push(step('ribbon-tab', { name: actionTab(action) }))
    if (action.group === 'modify' || action.group === 'clipboard') {
      if (context.session?.engineParsed) steps.push(step('engine-ready'))
      if (context.session?.engineParsed && context.session?.selected) steps.push(step('select-entity', {
        type: context.session.selected.type, editable: context.session.selected.editable,
        multiple: context.session.selectedIds?.length > 1,
      }))
      if (action.group === 'clipboard' && context.session?.clipboard) steps.push(step('copy-selection'))
    } else if (action.op && context.session?.engineParsed) steps.push(step('engine-ready'))
  }
  if (entry.kind === 'tool') steps.push(step('catalog-tool', { name: entry.source_id }))
  if (entry.kind === 'drawer') {
    const name = drawerNames[entry.source_id]
    steps.push(step(entry.source_id === 'nav' ? 'tool-rail-state' : 'drawer-state', {
      name, open: state === 'open' || state === 'drawer-open',
    }))
  }
  if (state === 'pane-open') steps.push(step('properties-state', { open: true }))
  if (state === 'no-selection' || state === 'empty-clipboard') steps.push(step('clear-selection'))
  if (state === 'drawer-open' && entry.kind !== 'drawer') steps.push(step('drawer-state', { name: 'Catalog', open: true }))
  if (state === 'start-open') steps.push(step('open-start'))
  if (state === 'history-open') steps.push(step('open-history'))
  if (state === 'selection-present') steps.push(step('engine-ready'), step('select-entity', { type: 'LINE', editable: true }))
  if (state === 'route-open') steps.push(step('open-route', { tool: context.route.tool }))
  if (state === 'read-only') steps.push(step('preview-version'))
  if (state === 'write-locked') steps.push(step('foreign-checkout'))
  if (state === 'unentitled' || state === 'write-unentitled') steps.push(step('private-policy', {
    capability: state === 'unentitled' ? 'build' : 'run_write', allowed: false,
  }))
  if (state === 'unsaved-engine-edits') steps.push(step('create-line'))
  if (state === 'nothing-to-undo' || state === 'nothing-to-redo') steps.push(step('engine-ready'), step('fresh-history'))
  if (entry.kind === 'action' && ['undo', 'redo'].includes(entry.source_id) && state === 'ready') {
    steps.push(step('engine-ready'), step('create-line'), step('create-line'), step('undo-edit'))
  }
  if (state === 'engine-not-parsed') steps.push(step('require-engine-state', { state: 'unparsed' }))
  if (state === 'engine-busy') steps.push(step('hold-engine-edit'))
  if (state === 'engine-crashed') steps.push(step('crash-engine-worker'))
  if (state === 'job-running') steps.push(step('start-pending-run'))
  if (state === 'authoring-off') steps.push(step('require-authoring-off'))
  if (['version-changing', 'mutations-blocked',
    'read-only-entity', 'project-open', 'errors-present', 'result-owns-retry'].includes(state)
    || state.startsWith('retry-')) {
    steps.push(step('require-local-state', { state, context }))
  }
  if (entry.kind === 'surface') steps.push(step('require-surface-context', { context }))
  if (effect.target === 'viewer-home') steps.push(step('zoom-before-fit', {
    control: role('button', 'Zoom in', role('toolbar', 'View')),
  }))
  return { context, steps }
}

export function effectAssertion(entry, state) {
  const effect = entry.expected_effect?.[state]
  if (!effect || !effects.has(effect.kind)) throw new Error(`Unknown effect kind: ${effect?.kind}`)
  if (effect.kind === 'disabled_with_reason' && (!effect.reason || !effect.reason_code)) {
    throw new Error(`Disabled effect needs a user-facing reason: ${entry.id}/${state}`)
  }
  if (effect.kind !== 'disabled_with_reason' && !effect.target) throw new Error(`Effect needs a target: ${entry.id}/${state}`)
  return { ...structuredClone(effect), assertionId: `${entry.id}/${state}/${effect.kind}`,
    ...(effect.target === 'cockpit-prompt' ? { verb: PROMPTS[effect.operation]?.verb } : {}) }
}

export function resolveProbe(entry, state) {
  if (!entry.states.includes(state)) throw new Error(`Unknown state ${state} for ${entry.id}`)
  const assertion = effectAssertion(entry, state)
  return {
    featureId: entry.id, kind: entry.kind, sourceId: entry.source_id, state,
    profile: entry.profile, certify: entry.certify,
    certification: ['unsupported_local', 'staging'].includes(entry.certify)
      ? { result: entry.certify, reason: entry.certify_reason } : null,
    setup: stateRecipe(entry, state), locator: locatorRecipe(entry, state), assertion,
  }
}
