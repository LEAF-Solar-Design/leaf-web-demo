import { ACTIONS, REASONS, accessibleName, reasonCode } from '../../src/lib/actionRegistry.js'
import { PRODUCT_SURFACES } from '../../src/site/productSurfaces.js'
import { PROFILE_RIBBON_TABS } from '../../src/lib/ribbonTabs.data.js'
import { toolPlacementTab } from '../../src/lib/toolRecord.js'
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

export const CENSUS_RECIPE_CONTROLS = Object.freeze([
  'open-dxf', 'save-version', 'undo-edit', 'redo-edit', 'more-panels',
  'open-dxf-browser', 'objects', 'ribbon-script', 'choose-script', 'run-script',
].map((id) => `control:${id}`))

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
  { feature_id: 'control:properties-close', scope: 'complementary:"Properties"', role: 'button', name: 'Close the properties pane',
    states: ["ready","failed-load"], viewports: ['desktop'] },
  { feature_id: 'control:properties-drawing', scope: 'complementary:"Properties"', role: 'button', name: 'Drawing',
    states: ["ready"], viewports: ['desktop'] },
  { feature_id: 'control:properties-layers', scope: 'complementary:"Properties"', role: 'button', name: 'Layers',
    states: ["ready","failed-load"], viewports: ['desktop'] },
  { feature_id: 'control:properties-panels', scope: 'complementary:"Properties"', role: 'button', name: 'Panels {n}',
    states: ["ready"], viewports: ['desktop'] },
  { feature_id: 'control:properties-plan', scope: 'complementary:"Properties"', role: 'button', name: 'Plan',
    states: ["ready","failed-load"], viewports: ['desktop'] },
  { feature_id: 'control:properties-selection', scope: 'complementary:"Properties"', role: 'button', name: 'Selection',
    states: ["ready","failed-load"], viewports: ['desktop'] },
  { feature_id: 'control:properties-walk', scope: 'complementary:"Properties"', role: 'button', name: 'Walk {n}',
    states: ["ready"], viewports: ['desktop'] },
  { feature_id: 'control:drawing-overview-collapse', scope: 'document', role: 'button', name: 'Collapse drawing overview',
    states: ["ready"], viewports: ['desktop'] },
  { feature_id: 'control:drawing-overview', scope: 'document', role: 'button', name: 'Drawing overview',
    states: ["ready"], viewports: ['desktop'] },
  { feature_id: 'control:layer-panels', scope: 'toolbar:"Drafting tools" > group:"Layers"', role: 'button', name: 'Panels',
    states: ["ready"], viewports: ['desktop'] },
  { feature_id: 'control:layer-walk', scope: 'toolbar:"Drafting tools" > group:"Layers"', role: 'button', name: 'Walk',
    states: ["ready"], viewports: ['desktop'] },
  { feature_id: 'control:job-monitor-expand', scope: 'toolbar:"Job monitor"', role: 'button', name: 'Expand the job monitor ({n} live)',
    states: ["ready","failed-load"], viewports: ['desktop'] },
  {"feature_id":"control:scope-add","scope":"document","role":"button","name":"Add: build a new capability","states":["ready","failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:demo-return","scope":"document","role":"button","name":"Back to the demo","states":["failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:claude-accounts","scope":"document","role":"button","name":"Claude accounts not linked","states":["ready","failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:drawing-close-start","scope":"document","role":"button","name":"Close the drawing view and return to Start","states":["ready"],"viewports":["desktop"]},
  {"feature_id":"control:notification-collapse","scope":"document","role":"button","name":"Collapse the notification inbox","states":["ready","failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:session-details","scope":"document","role":"button","name":"Details","states":["ready","failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:version-history","scope":"document","role":"button","name":"History","states":["ready"],"viewports":["desktop"]},
  {"feature_id":"control:linked-services","scope":"document","role":"button","name":"Linked services {n} linked","states":["ready","failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:project-board","scope":"document","role":"button","name":"Open the project board","states":["ready","failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:prompt-run","scope":"document","role":"button","name":"Run","states":["ready","failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:prompt-scope","scope":"document","role":"button","name":"scope ▾","states":["ready","failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:sign-out","scope":"document","role":"button","name":"Sign out","states":["ready","failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:start-board","scope":"document","role":"button","name":"Start","states":["ready","failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:take-edit-lock","scope":"document","role":"button","name":"Take edit lock","states":["ready"],"viewports":["desktop"]},
  {"feature_id":"control:cost-panel","scope":"document","role":"button","name":"What Leaf costs to operate","states":["ready","failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:command-bar","scope":"document","role":"combobox","name":"Command bar","states":["ready","failed-load"],"viewports":["desktop"]},
  {"feature_id":"control:find-drawing","scope":"document","role":"combobox","name":"Find in drawing","states":["ready"],"viewports":["desktop"]},
])

export const normalizedControlKey = (row) => controlKey({ ...row,
  name: row.name.replace(/^((?:Panels|Walk) )[0-9][0-9,]*$/, '$1{n}') })
export function controlScope(context) {
  if (context.complementary) return role('complementary', context.complementary)
  if (context.group) return role('group', context.group, role('toolbar', context.toolbar))
  return context.toolbar ? role('toolbar', context.toolbar) : undefined
}
export function controlName(context) {
  if (context.namePolicy !== 'count') return context.name
  const count = context.name?.startsWith('Expand ') || context.name === 'Linked services {n} linked' ? '[0-9]+' : '[0-9][0-9,]*'
  return new RegExp('^' + context.name.split('{n}').map(escapePattern).join(count) + '$')
}

export function requireControlCensusBatch(census, { state = 'ready', viewport = 'desktop' } = {}) {
  for (const expected of CONTROL_CENSUS_BATCH) {
    if (!expected.states.includes(state) || !expected.viewports.includes(viewport)) continue
    const matches = census.resolved.filter((row) => normalizedControlKey(row) === normalizedControlKey(expected))
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
    if (entry.id === 'control:objects') return {
      ...role('group', context.name), css: 'details.drawing-objects-panel > summary', trigger: 'click',
      tooltip: context.tooltip, description: context.description,
    }
    return { ...role(context.role || 'button', controlName(context), controlScope(context)), trigger: context.interaction || 'click',
      ...(context.inputValue !== undefined ? { inputValue: context.inputValue } : {}),
      ...(context.namePolicy === 'count' ? { normalizedName: context.name } : {}),
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
    ...(effect.kind === 'disabled_with_reason' ? { unavailableName: new RegExp(`^${escapePattern(property ? action.text : action.label)}(?: \\(unavailable: .+\\))?$`) } : {}),
    ...(state === 'no-drawing' ? { availableName: property ? action.text : action.label } : {}),
    ...(action.panel || action.cluster || action.group ? { group: action.panel || action.cluster || action.group } : {}) }
  }
  if (entry.kind === 'surface') {
    const record = PRODUCT_SURFACES.find((surface) => surface.id === entry.source_id)
    if (!record) throw new Error(`No surface registry record for ${entry.id}`)
    if (['signed-out', 'no-drawing'].includes(state)) return {
      ...role('main', 'Leaf operator workspace'), trigger: 'navigate', url: `/try?surface=${record.id}`,
    }
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
    if (entry.source_id === 'jobs') return {
      ...role('button', state === 'open' ? 'Collapse the job monitor to a spine' : /^Expand the job monitor \([0-9]+ live\)$/), trigger: 'click',
      phone: { ...role('button', 'Jobs', role('group', 'Workspace panels')), trigger: 'click' },
    }
    return { ...role('button', drawerNames[entry.source_id], role('group', 'Workspace panels')),
      trigger: entry.source_id === 'none' ? 'keyboard' : 'click', key: 'Escape' }
  }
  if (entry.kind === 'tool') {
    const record = tools.find((tool) => tool.name === entry.source_id)
    if (!record) throw new Error(`No catalog registry record for ${entry.id}`)
    const family = catalog.response.families.find((family) => family.capabilities.includes(record))
    return { ...role('button', accessibleName(record.name,
      effect.kind === 'disabled_with_reason' ? effect.reason : ''), role('toolbar', 'Drafting tools')),
    panelName: family.label, panelId: `${family.family_id}${toolPlacementTab(record) ? '@' + toolPlacementTab(record) : ''}`, trigger: 'click' }
  }
  throw new Error(`Unknown feature kind: ${entry.kind}`)
}

function actionTab(action) {
  if (action.panel === 'solar-panels') return 'Solar'
  if (action.surface === 'engine') return 'Draw'
  if (['author', 'rail'].includes(action.cluster)) return 'Manage'
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
    : entry.kind === 'surface' ? entry.source_id
      : entry.kind === 'tool' && entry.source_id.startsWith('solar-') && state === 'ready' ? 'solar'
      : entry.kind === 'action' && actionRecord(entry).panel === 'solar-panels' ? 'solar' : 'cad'
  const steps = []
  if (entry.kind === 'control') {
    if (CENSUS_RECIPE_CONTROLS.includes(entry.id)) {
      const script = ['ribbon-script', 'choose-script', 'run-script'].includes(entry.source_id)
      if (state === 'engine-busy' || state === 'running') steps.push(step('prepare-engine-transport'))
      steps.push(state === 'no-target' ? step('census-download-only')
        : context.failedLoad || state === 'no-document'
          ? step('open-failed-drawing', { url: '/app?surface=cad&drawing=missing.invalid' })
          : step('open-private-drawing', { surface: 'cad' }))
      const locator = locatorRecipe(entry, state)
      if (effect.kind === 'toggles') {
        steps.push(step('census-disclosure', { control: locator, target: effect.target, expanded: context.expanded }))
      } else if (script) {
        // View owns the Script seat, including when no drawing is loaded.
        steps.push(step('ribbon-tab', { name: 'View' }))
        if (!context.failedLoad && state !== 'no-document') steps.push(step('engine-ready'))
        if (state === 'engine-busy') steps.push(step('hold-engine-edit'), step('ribbon-tab', { name: 'View' }))
        if (state === 'engine-crashed') steps.push(step('crash-engine-worker'))
        steps.push(step('census-script', { running: state === 'running',
          text: entry.source_id === 'run-script' && state !== 'empty-script' || state === 'running' ? 'line 0,0 10,10' : '' }))
      } else if (!context.failedLoad && state !== 'no-document') {
        steps.push(step('engine-ready'))
        if (state === 'ready' || state === 'no-target' || entry.source_id === 'save-version' && state === 'engine-busy') {
          steps.push(step('create-line'))
        }
        if (entry.source_id === 'redo-edit' && state === 'ready') steps.push(step('undo-edit'))
        if (state === 'nothing-to-undo' || state === 'nothing-to-redo' || state === 'nothing-edited') steps.push(step('fresh-history'))
        if (state === 'engine-busy') steps.push(step('hold-engine-edit'))
      }
      return { context, steps }
    }
    if (effect.target === 'signed-out-session') steps.push(step('fresh-sign-out-page'))
    steps.push(context.failedLoad
      ? step('open-failed-drawing', { url: '/app?surface=cad&drawing=missing.invalid' })
      : step('open-private-drawing', { surface: 'cad' }))
    const locator = locatorRecipe(entry, state)
    if (["control:scope-add","control:demo-return","control:claude-accounts","control:drawing-close-start","control:notification-collapse","control:session-details","control:version-history","control:linked-services","control:project-board","control:prompt-run","control:prompt-scope","control:sign-out","control:start-board","control:take-edit-lock","control:cost-panel","control:command-bar","control:find-drawing"].includes(entry.id)) {
      steps.push(step('baseline-three-state', { sourceId: entry.source_id, target: effect.target, control: locator, expanded: context.expanded }))
    }
    if (/^engine-mode:(ortho|osnap)$/.test(effect.target)) steps.push(step('engine-mode-state', {
      control: locator, mode: effect.target.split(':')[1], pressed: context.pressed,
    }))
    if (effect.target === 'drafting-grid') steps.push(step('control-pressed-state', { control: locator, pressed: context.pressed }))
    if (effect.target === 'document-fullscreen') steps.push(step('fullscreen-state', { control: locator, fullscreen: context.fullscreen }))
    if (entry.source_id === 'view-back') steps.push(step(state === 'empty-history' ? 'empty-view-history' : 'previous-view-history'))
    if (entry.source_id === 'view-up') steps.push(step('whole-drawing-view'))
    if (context.complementary || effect.target?.startsWith('layer-')) steps.push(step('properties-state', { open: true }))
    if (/^properties-(drawing|layers|plan|selection)-section$/.test(effect.target)) {
      if (effect.target === 'properties-selection-section' && !context.failedLoad) {
        steps.push(step('select-entity', { type: 'LINE', viewerOnly: true }))
      }
      steps.push(step('properties-section-state', { name: context.name, expanded: context.expanded }))
    }
    if (entry.source_id === 'properties-close') steps.push(step('properties-close-state'))
    if (effect.target?.startsWith('layer-')) steps.push(step('layer-visible-state', {
      name: effect.target === 'layer-panels-visible' ? 'Panels' : 'Walk', visible: context.visible,
    }))
    if (effect.target === 'job-monitor') steps.push(step('job-monitor-collapsed'))
    if (effect.target === 'viewer-overview-pan') steps.push(step('overview-pan-state'))
    if (effect.target === 'drawing-overview-expanded') steps.push(step('overview-expanded-state'))
    return { context, steps }
  }
  if (surface === 'sheets') return { context, steps: [step('navigate', { url: '/sheets' })] }
  if (state === 'no-drawing' && entry.kind === 'action') {
    return { context, steps: [
      step('open-failed-drawing', { url: `/app?surface=${surface}&drawing=missing.invalid` }),
      step('failed-drawing-ribbon-tab', { name: actionTab(actionRecord(entry)) }),
      ...(surface === 'solar' ? [step('require-solar-document')] : []),
    ] }
  }
  if (state === 'engine-busy') steps.push(step('prepare-engine-transport'))
  if (state === 'engine-not-parsed') steps.push(step('hold-engine-boot'))
  if (state === 'solar-not-ready') steps.push(step('hold-solar-projection'))
  const empty = ['no-drawing', 'signed-out'].includes(state)
  steps.push(step(empty ? 'open-empty-workspace' : 'open-private-drawing', { surface, signedOut: state === 'signed-out' }))
  if (entry.kind === 'action') {
    const action = actionRecord(entry)
    if (action.surface === 'slash') steps.push(step('slash-menu', { command: action.label }))
    else if (action.surface !== 'bar') steps.push(step('ribbon-tab', { name: actionTab(action) }))
    if (action.group === 'modify' || action.group === 'clipboard') {
      if (context.session?.engineParsed) steps.push(step('engine-ready'))
      const needsClipboard = action.op === 'pasteClip' && context.session?.clipboard && context.session?.engineParsed
      if (needsClipboard) steps.push(step('select-entity', { type: 'LINE', editable: true }), step('copy-selection'), step('clear-selection'))
      if (context.session?.engineParsed && context.session?.selected) steps.push(step('select-entity', {
        type: action.op === 'explode' && state === 'ready' ? 'LWPOLYLINE' : context.session.selected.type, editable: context.session.selected.editable,
        multiple: context.session.selectedIds?.length > 1,
      }))
    } else if (action.op && context.session?.engineParsed) steps.push(step('engine-ready'))
  }
  if (entry.kind === 'tool') {
    if (entry.source_id.startsWith('solar-')) steps.push(step('seed-solar-graph'))
    steps.push(step('catalog-tool', { name: entry.source_id }))
  }
  if (state === 'no-versioned-drawing') steps.push(step('require-versionless-drawing'))
  if (entry.kind === 'drawer') {
    const name = drawerNames[entry.source_id]
    steps.push(step(entry.source_id === 'nav' ? 'tool-rail-state' : entry.source_id === 'jobs' ? 'job-rail-state' : 'drawer-state', {
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
    if (effect.target?.startsWith('drawing-version-')) steps.push(step('saved-version-history', { redo: entry.source_id === 'redo' }))
    else steps.push(step('engine-ready'), step('create-line'), step('create-line'), step('undo-edit'))
  }
  if (state === 'engine-not-parsed') steps.push(step('require-engine-state', { state: 'unparsed' }))
  if (state === 'engine-busy') steps.push(step('hold-engine-edit'))
  if (state === 'engine-crashed') steps.push(step('crash-engine-worker'))
  if (state === 'job-running') steps.push(step('start-pending-run'))
  if (state === 'authoring-off') steps.push(step('require-authoring-off'))
  const faultKind = entry.kind === 'action' && (
    ['history', 'undo', 'redo'].includes(entry.source_id) && state === 'version-changing' ? 'hold-version-change'
      : ['undo', 'redo'].includes(entry.source_id) && state === 'mutations-blocked' ? 'fault-restored-head'
        : entry.source_id === 'bar:retry' && state === 'retry-history' ? 'fault-history' : null)
  if (faultKind) steps.push(step(faultKind, { redo: entry.source_id === 'redo' }))
  else if (['version-changing', 'mutations-blocked',
    'read-only-entity', 'project-open', 'errors-present', 'result-owns-retry'].includes(state)
    || state.startsWith('retry-')) {
    const routeError = entry.kind === 'action' && (
      entry.source_id === 'bar:escape' && state === 'errors-present'
      || entry.source_id === 'bar:retry' && state === 'retry-route')
    steps.push(step('require-local-state', { state, context, ...(routeError ? {
      reason: 'route errors are absorbed into local suggestions by api.js nlPrompt (transport failures and non-401 HTTP errors); routeErr is not reachable through a real fault',
    } : entry.kind === 'action' && entry.source_id === 'bar:escape' && state === 'project-open' ? {
      reason: 'the isolated walk stack runs without Postgres, so the platform router (/api/orgs, /api/projects) is not mounted; project-open needs a platform-enabled stack',
    } : {}) }))
  }
  if (entry.kind === 'surface') steps.push(step('require-surface-context', { surface, context }))
  if (effect.target === 'viewer-home') steps.push(step('zoom-before-fit', {
    control: role('button', 'Zoom in', role('toolbar', 'View')),
  }))
  if (effect.target === 'viewer-zoom-out') steps.push(step('zoom-inside-extents'))
  // LINE commands and policy reloads can close overflow or change the tab.
  if (entry.kind === 'tool') steps.push(step('catalog-tool', { name: entry.source_id }))
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
