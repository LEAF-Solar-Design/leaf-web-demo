import { ACTIONS, accessibleName } from '../../src/lib/actionRegistry.js'
import { PRODUCT_SURFACES } from '../../src/site/productSurfaces.js'
import { PROFILE_RIBBON_TABS } from '../../src/lib/ribbonTabs.data.js'
import { STUDIO_DRAWERS } from '../../src/lib/studioDrawers.js'
import { PROMPTS } from '../../src/cadedit/promptKeys.js'
import { readFileSync } from 'node:fs'

const catalog = JSON.parse(readFileSync(new URL('../../walk/fixtures/capabilities.snapshot.json', import.meta.url), 'utf8'))
const tools = catalog.response.families.flatMap((family) => family.capabilities)
const drawerNames = Object.freeze({ nav: 'Catalog', jobs: 'Jobs', result: 'Result', plan: 'Plan', none: 'Catalog' })
const profileSurfaces = Object.freeze({ drafting: 'cad', solar: 'solar', project: 'browser', ship: 'ios' })
const effects = new Set(['opens', 'toggles', 'navigates', 'submits', 'disabled_with_reason', 'renders'])
const step = (kind, properties = {}) => ({ kind, ...properties })
const role = (name, accessible, scope) => ({ role: name, name: accessible, exact: true, ...(scope ? { scope } : {}) })

function actionRecord(entry) {
  const record = ACTIONS.find((action) => action.id === entry.source_id)
  if (!record) throw new Error(`No action registry record for ${entry.id}`)
  return record
}

export function locatorRecipe(entry, state) {
  const effect = entry.expected_effect[state]
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
    return { ...role(property ? 'combobox' : 'button', accessibleName(property ? action.text : action.label, why),
      role('toolbar', 'Drafting tools')), trigger: property ? 'select' : 'click',
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
  if (surface === 'sheets') return { context, steps: [step('navigate', { url: '/sheets' })] }
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
    steps.push(step('drawer-state', { name, open: state === 'open' || state === 'drawer-open' }))
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
  if (effect.target === 'viewer-home') steps.push(step('zoom-before-fit'))
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
