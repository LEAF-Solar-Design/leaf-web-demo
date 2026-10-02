import { locatorRecipe } from './probes.mjs'
import { readControlInventory, resolveCensus, censusFailure, controlNameAttributes, registryCensusMappings } from '../../walk/controlInventory.mjs'
import { PRODUCT_SURFACES } from '../../src/site/productSurfaces.js'

export { resolveCensus } from '../../walk/controlInventory.mjs'
export const INTERACTIVE_SELECTOR = 'button,a[href],area[href],input:not([type="hidden"]),select,textarea,summary,[contenteditable]:not([contenteditable="false"]),[role],[tabindex]'
const interactiveRoles = ['button', 'link', 'checkbox', 'radio', 'switch', 'textbox', 'searchbox', 'combobox',
  'listbox', 'option', 'slider', 'spinbutton', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'treeitem',
  'gridcell', 'columnheader', 'rowheader', 'scrollbar']
const containerRoles = ['group', 'navigation', 'region', 'main', 'complementary', 'banner', 'contentinfo',
  'form', 'toolbar', 'tablist', 'dialog', 'menu', 'menubar', 'radiogroup', 'tree', 'treegrid', 'grid',
  'row', 'list', 'listitem', 'table', 'rowgroup', 'article', 'document', 'presentation', 'none', 'status', 'alert']

// ariaSnapshot uses the browser runner's accessible-name computation (including
// associated labels and aria-labelledby). DOM fallback covers generic tabindex
// controls which have no named accessibility-tree root.
function snapshotIdentity(snapshot, fallback) {
  const match = snapshot.split('\n')[0]?.match(/^- ([a-z]+)(?: "((?:\\.|[^"\\])*)")?/)
  return match ? { role: match[1], name: match[2] === undefined ? fallback.name : JSON.parse(`"${match[2]}"`) } : fallback
}

export async function enumerateControls(page) {
  const all = page.locator(INTERACTIVE_SELECTOR)
  const controls = await all.evaluateAll((elements, { roles, containers }) => elements.map((element, index) => {
    const tag = element.tagName.toLowerCase(), explicitRole = element.getAttribute('role')?.split(/\s+/)[0]
    const nativeRoles = { button: 'button', a: 'link', area: 'link', select: element.multiple ? 'listbox' : 'combobox',
      textarea: 'textbox', summary: 'button', main: 'main', nav: 'navigation', aside: 'complementary',
      header: 'banner', footer: 'contentinfo', section: 'region', form: 'form', fieldset: 'group',
      ul: 'list', ol: 'list', menu: 'list' }
    const inputRoles = { checkbox: 'checkbox', radio: 'radio', range: 'slider', number: 'spinbutton',
      button: 'button', submit: 'button', reset: 'button', search: 'searchbox' }
    const role = explicitRole || nativeRoles[tag] || (tag === 'input' ? inputRoles[element.type] || 'textbox'
      : element.isContentEditable ? 'textbox' : 'generic')
    if (containers.includes(role)) return null
    const interactive = ['button', 'input', 'select', 'textarea', 'summary'].includes(tag)
      || (['a', 'area'].includes(tag) && element.hasAttribute('href'))
      || roles.includes(explicitRole) || element.isContentEditable || (element.hasAttribute('tabindex') && element.tabIndex >= 0)
    if (!interactive) return null
    const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim()
    const label = (node) => {
      const ids = node.getAttribute('aria-labelledby')?.trim().split(/\s+/)
      return normalize(ids?.map((id) => node.ownerDocument.getElementById(id)?.textContent || '').join(' ')
        || node.getAttribute('aria-label') || (node.labels ? [...node.labels].map((item) => item.textContent).join(' ') : '')
        || node.getAttribute('alt') || node.getAttribute('title') || node.textContent)
    }
    let visible = element.getClientRects().length > 0
    let disabled = element.matches(':disabled')
    const chain = []
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node)
      if (node.hidden || style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility)) visible = false
      if (node.getAttribute('aria-disabled') === 'true' || node.hasAttribute('inert')) disabled = true
      if (node === element) continue
      const landmark = node.getAttribute('role')?.split(/\s+/)[0]
        || ({ main: 'main', nav: 'navigation', aside: 'complementary', header: 'banner', footer: 'contentinfo',
          section: 'region', form: 'form', fieldset: 'group' })[node.tagName.toLowerCase()]
      const named = node.hasAttribute('aria-label') || node.hasAttribute('aria-labelledby')
      const name = named ? label(node) : node.tagName === 'FIELDSET' ? normalize(node.querySelector('legend')?.textContent) : ''
      if (name && ['main', 'navigation', 'complementary', 'banner', 'contentinfo', 'region', 'form', 'group',
        'toolbar', 'tablist', 'dialog', 'menu', 'menubar', 'listbox', 'tree', 'grid'].includes(landmark)) {
        chain.unshift(`${landmark}:${JSON.stringify(name)}`)
      }
    }
    return { index, role, name: label(element), scope: chain.join(' > ') || 'document', disabled, visible }
  }), { roles: interactiveRoles, containers: containerRoles })
  for (const control of controls.filter(Boolean)) {
    if (control.visible) Object.assign(control, snapshotIdentity(await all.nth(control.index).ariaSnapshot(), control))
    Object.assign(control, controlNameAttributes(control.name))
    if (control.disabled_reason) control.disabled = true
  }
  return controls.filter(Boolean)
}

const groupNames = { draw: 'Draw', modify: 'Modify', clipboard: 'Clipboard', properties: 'Properties',
  annotation: 'Annotation', block: 'Block', groups: 'Groups', 'solar-panels': 'Panels',
  view: 'View', version: 'Version', author: 'Author', rail: 'Rail' }
function recipeLocator(page, recipe) {
  let scope = recipe.scope ? recipeLocator(page, recipe.scope) : page
  if (recipe.group && recipe.role !== 'combobox') scope = scope.getByRole('group', { name: groupNames[recipe.group], exact: true })
  return scope.getByRole(recipe.role, { name: recipe.name, exact: recipe.exact !== false })
}

export async function censusControls(page, map, { state = 'ready', viewport = 'desktop', inventory = readControlInventory(map) } = {}) {
  // Enumerate first, independently of coverage, so missing registry entries
  // cannot remove controls from the census.
  const controls = await enumerateControls(page)
  const selected = page.getByRole('tablist', { name: 'Workspace profile', exact: true }).getByRole('tab', { selected: true })
  const selectedName = await selected.count() === 1 ? await selected.getAttribute('aria-label') : null
  const profile = PRODUCT_SURFACES.find((surface) => surface.label === selectedName)?.contract.toolbar?.profile
  const derived = registryCensusMappings(controls, map, { profile, viewport })
  for (const entry of map.entries) {
    if (!entry.viewports.includes(viewport)) continue
    if (entry.kind === 'tab') continue // active-profile registry mapping above
    for (const featureState of entry.states) {
      let recipe = locatorRecipe(entry, featureState)
      if (viewport === 'phone' && recipe.phone) recipe = recipe.phone
      if (['keyboard', 'navigate'].includes(recipe.trigger)) continue
      const indices = await recipeLocator(page, recipe).evaluateAll((elements, selector) => {
        const all = [...document.querySelectorAll(selector)]
        return elements.map((element) => all.indexOf(element))
      }, INTERACTIVE_SELECTOR)
      for (const index of indices) derived.push({ index, feature_id: entry.id })
    }
  }
  return { ...resolveCensus(controls, derived, map, inventory, { state, viewport }), controls }
}

export async function assertControlCensus(page, map, options, evidence) {
  const result = await censusControls(page, map, options)
  if (evidence) (evidence.censuses ||= []).push(result)
  if (!result.ok) throw new Error(censusFailure(result))
  return result
}
