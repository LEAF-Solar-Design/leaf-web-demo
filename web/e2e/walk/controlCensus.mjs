import { readControlInventory } from '../../walk/controlInventory.mjs'
import { locatorRecipe } from './probes.mjs'

// Enumeration is independent of the inventory, including disabled controls.
export const INTERACTIVE_SELECTOR = 'button,input:not([type="hidden"]),select,textarea,a[href],area[href],[contenteditable]:not([contenteditable="false"]),[role],[tabindex]'
const interactiveRoles = new Set(['button', 'link', 'checkbox', 'radio', 'switch', 'textbox', 'combobox',
  'listbox', 'option', 'slider', 'spinbutton', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'treeitem'])

export function resolveCensus(controls, mappings, map, { viewport = 'desktop' } = {}) {
  const entries = new Map(map.entries.map((entry) => [entry.id, entry]))
  const errors = [], resolved = [], selectors = new Set()
  for (const mapping of mappings) {
    if (selectors.has(mapping.key)) throw new Error('control-census: duplicate selector assignment')
    selectors.add(mapping.key)
  }
  for (const control of controls) {
    if (!control.visible || control.inert) continue
    const matches = mappings.filter((mapping) => mapping.indices.includes(control.index))
    const fail = (reason) => errors.push({ index: control.index, tag: control.tag,
      label: String(control.label || '').slice(0, 160), reason })
    if (!matches.length) { fail('missing inventory mapping'); continue }
    if (matches.length !== 1) { fail('ambiguous mapping'); continue }
    const mapping = matches[0], entry = entries.get(mapping.featureId)
    if (!entry) { fail('unknown feature ID'); continue }
    if (!entry.states.includes(mapping.state) || !entry.expected_effect[mapping.state]
      || !entry.viewports.includes(viewport) || !mapping.viewports.includes(viewport)) {
      fail('unsupported state or viewport mapping'); continue
    }
    const effect = entry.expected_effect[mapping.state]
    if ((effect.kind === 'disabled_with_reason') !== !!control.disabled) {
      fail('rendered disabled state does not match inventory state'); continue
    }
    if ((mapping.presentationIndices || mapping.indices).filter((index) => controls.some((row) => row.index === index && row.visible && !row.inert)).length > 1
      && !mapping.allowRepeat) { fail('repeated presentation requires explicit mapping'); continue }
    resolved.push({ index: control.index, featureId: entry.id, state: mapping.state, disabled: control.disabled })
  }
  return { ok: errors.length === 0, total: controls.filter((row) => row.visible && !row.inert).length,
    resolved, errors: errors.slice(0, 30), errorCount: errors.length,
    excluded: controls.filter((row) => !row.visible || row.inert).map(({ index, inert }) => ({ index, reason: inert ? 'inert' : 'hidden' })) }
}

function recipeLocator(page, recipe) {
  let scope = recipe.scope ? recipeLocator(page, recipe.scope) : page
  if (recipe.group && recipe.role !== 'combobox') {
    const names = { draw: 'Draw', modify: 'Modify', clipboard: 'Clipboard', properties: 'Properties',
      annotation: 'Annotation', block: 'Block', groups: 'Groups', 'solar-panels': 'Panels',
      view: 'View', version: 'Version', author: 'Author', rail: 'Rail' }
    scope = scope.getByRole('group', { name: names[recipe.group], exact: true })
  }
  if (recipe.selector) return scope.locator(recipe.selector)
  return scope.getByRole(recipe.role, { name: recipe.name, exact: recipe.exact !== false })
}

export async function censusControls(page, map, { viewport = 'desktop', states = {}, inventory = readControlInventory() } = {}) {
  // Take the full census BEFORE consulting any reviewed mapping.
  const controls = await page.locator(INTERACTIVE_SELECTOR).evaluateAll((elements, roles) => elements.map((element, index) => {
    const tag = element.tagName.toLowerCase(), role = element.getAttribute('role')
    const native = ['button', 'input', 'select', 'textarea', 'a', 'area'].includes(tag)
    const interactive = native || roles.includes(role) || element.isContentEditable
      || (element.hasAttribute('tabindex') && Number(element.getAttribute('tabindex')) >= 0)
    let visible = interactive && element.getClientRects().length > 0
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node)
      if (node.hidden || style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility)) visible = false
    }
    return { index, tag, visible, inert: !!element.closest('[inert]'),
      disabled: element.matches(':disabled') || element.getAttribute('aria-disabled') === 'true',
      label: (element.getAttribute('aria-label') || element.textContent || '').slice(0, 160),
      title: element.closest('[title]')?.getAttribute('title') || '',
      reason: element.closest('#cockpit-prompt')?.querySelector('[data-testid="cockpit-prompt-note"]')?.textContent || '' }
  }), [...interactiveRoles])
  const mappings = []
  const add = async (key, featureId, recipes, candidates, viewports, allowRepeat = false) => {
    const found = []
    for (const recipe of recipes) {
      found.push(...await recipeLocator(page, recipe).evaluateAll((elements, selector) => {
        const all = [...document.querySelectorAll(selector)]
        return elements.map((element) => all.indexOf(element)).filter((index) => index >= 0)
      }, INTERACTIVE_SELECTOR))
    }
    const indices = [...new Set(found)]
    for (const index of indices) {
      const observed = controls[index], entry = map.entries.find((row) => row.id === featureId)
      const state = states[featureId] || candidates.find((state) => {
        const effect = entry?.expected_effect[state]
        return effect && (effect.kind === 'disabled_with_reason'
          ? observed.disabled && (observed.title.includes(effect.reason) || observed.label.includes(effect.reason) || observed.reason.includes(effect.reason))
          : !observed.disabled)
      }) || candidates[0]
      mappings.push({ key: `${key}:${index}`, featureId, state, viewports, allowRepeat, indices: [index], presentationIndices: indices })
    }
  }
  for (const entry of map.entries) {
    if (!entry.viewports.includes(viewport)) continue
    // Keyboard actions share the command bar; its own explicit row owns it.
    const candidates = entry.states.includes('ready') ? ['ready', ...entry.states.filter((state) => state !== 'ready')] : entry.states
    const recipes = candidates.map((state) => locatorRecipe(entry, state))
      .map((recipe) => viewport === 'phone' && recipe.phone ? recipe.phone : recipe)
      .filter((recipe) => recipe.trigger !== 'keyboard' && recipe.trigger !== 'navigate')
    if (!recipes.length) continue
    await add(`entry:${entry.id}`, entry.id, recipes, candidates, entry.viewports, entry.allow_repeat)
  }
  for (const row of inventory.presentations) {
    await add(`presentation:${row.selector}`, row.feature_id, [{ selector: row.selector }], [row.state], row.viewports, row.allow_repeat)
  }
  const result = resolveCensus(controls, mappings, map, { viewport })
  return { ...result, controls }
}

export async function assertControlCensus(page, map, options, evidence) {
  const census = await censusControls(page, map, options)
  if (evidence) (evidence.censuses ||= []).push(census)
  if (!census.ok) throw new Error(`control-census: ${census.errorCount} unmapped or invalid controls: ${JSON.stringify(census.errors)}`)
  return census
}
