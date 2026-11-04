import { locatorRecipe } from './probes.mjs'
import { readControlInventory, resolveCensus, censusFailure, controlNameAttributes, registryCensusMappings } from '../../walk/controlInventory.mjs'
import { PRODUCT_SURFACES } from '../../src/site/productSurfaces.js'

export { resolveCensus } from '../../walk/controlInventory.mjs'
// The census enumerates hundreds of controls, so DOM snapshots in its failure trace exceed the walk archive cap, while its evidence lives in the walk-evidence attachment.
export const CENSUS_TRACE = Object.freeze({ mode: 'retain-on-failure', snapshots: false, screenshots: false, sources: false })
// The studio shell wraps the same rail column in a named region, which is not part of a control's identity.
export const SHELL_WRAPPER_SCOPES = Object.freeze(['region:"Studio sidebar"'])
export const INTERACTIVE_SELECTOR = 'button,a[href],area[href],input:not([type="hidden"]),select,textarea,summary,[contenteditable]:not([contenteditable="false"]),[role],[tabindex]'
export const MAX_ENUMERATION_ATTEMPTS = 3
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
  const read = (elements, { roles, containers, shellWrapperScopes }) => elements.map((element, index) => {
    const signature = `${element.tagName.toLowerCase()}|${element.getAttribute('role') || ''}|${element.getAttribute('aria-label') || ''}`
    const tag = element.tagName.toLowerCase(), explicitRole = element.getAttribute('role')?.split(/\s+/)[0]
    const nativeRoles = { button: 'button', a: 'link', area: 'link', select: element.multiple ? 'listbox' : 'combobox',
      textarea: 'textbox', summary: 'button', main: 'main', nav: 'navigation', aside: 'complementary',
      header: 'banner', footer: 'contentinfo', section: 'region', form: 'form', fieldset: 'group',
      ul: 'list', ol: 'list', menu: 'list' }
    const inputRoles = { checkbox: 'checkbox', radio: 'radio', range: 'slider', number: 'spinbutton',
      button: 'button', submit: 'button', reset: 'button', search: 'searchbox' }
    const role = explicitRole || nativeRoles[tag] || (tag === 'input' ? inputRoles[element.type] || 'textbox'
      : element.isContentEditable ? 'textbox' : 'generic')
    if (containers.includes(role)) return { signature, control: null }
    const interactive = ['button', 'input', 'select', 'textarea', 'summary'].includes(tag)
      || (['a', 'area'].includes(tag) && element.hasAttribute('href'))
      || roles.includes(explicitRole) || element.isContentEditable || (element.hasAttribute('tabindex') && element.tabIndex >= 0)
    if (!interactive) return { signature, control: null }
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
      if (node.hidden || node.hasAttribute('inert') || style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility)) visible = false
      if (node.getAttribute('aria-disabled') === 'true' || node.hasAttribute('inert')) disabled = true
      if (node === element) continue
      const landmark = node.getAttribute('role')?.split(/\s+/)[0]
        || ({ main: 'main', nav: 'navigation', aside: 'complementary', header: 'banner', footer: 'contentinfo',
          section: 'region', form: 'form', fieldset: 'group' })[node.tagName.toLowerCase()]
      const named = node.hasAttribute('aria-label') || node.hasAttribute('aria-labelledby')
      const name = named ? label(node) : node.tagName === 'FIELDSET' ? normalize(node.querySelector('legend')?.textContent) : ''
      if (name && ['main', 'navigation', 'complementary', 'banner', 'contentinfo', 'region', 'form', 'group',
        'toolbar', 'tablist', 'dialog', 'menu', 'menubar', 'listbox', 'tree', 'grid'].includes(landmark)) {
        const scope = `${landmark}:${JSON.stringify(name)}`
        if (!shellWrapperScopes.includes(scope)) chain.unshift(scope)
      }
    }
    return { signature, control: { index, role, name: label(element), scope: chain.join(' > ') || 'document', disabled, visible } }
  })
  for (let attempt = 0; attempt < MAX_ENUMERATION_ATTEMPTS; attempt++) {
    const rows = await all.evaluateAll(read, { roles: interactiveRoles, containers: containerRoles, shellWrapperScopes: SHELL_WRAPPER_SCOPES })
    const controls = rows.map(({ control }) => control).filter(Boolean)
    for (const control of controls) {
      if (control.visible) Object.assign(control, snapshotIdentity(await all.nth(control.index).ariaSnapshot(), control))
      Object.assign(control, controlNameAttributes(control.name))
      if (control.disabled_reason) control.disabled = true
    }
    const signatures = await all.evaluateAll(elements => elements.map(element =>
      `${element.tagName.toLowerCase()}|${element.getAttribute('role') || ''}|${element.getAttribute('aria-label') || ''}`))
    if (signatures.length === rows.length && signatures.every((signature, index) => signature === rows[index].signature)) return controls
  }
  throw new Error('control census: the page changed during enumeration on 3 attempts')
}

const groupNames = { draw: 'Draw', modify: 'Modify', clipboard: 'Clipboard', properties: 'Properties',
  annotation: 'Annotation', block: 'Block', groups: 'Groups', 'solar-panels': 'Panels',
  view: 'View', version: 'Version', author: 'Author', rail: 'Rail' }
function recipeLocator(page, recipe) {
  let scope = recipe.scope ? recipeLocator(page, recipe.scope) : page
  if (recipe.group && recipe.role !== 'combobox') scope = scope.getByRole('group', { name: groupNames[recipe.group], exact: true })
  return scope.getByRole(recipe.role, { name: recipe.name, exact: recipe.exact !== false })
}

export async function activeToolbarProfile(page) {
  const select = page.getByRole('navigation', { name: 'Workspace controls', exact: true })
    .getByRole('combobox', { name: 'Workspace profile', exact: true })
  if (await select.count() === 1) {
    const value = await select.inputValue()
    return PRODUCT_SURFACES.find((surface) => surface.id === value)?.contract.toolbar?.profile
  }
  const selected = page.getByRole('tablist', { name: 'Workspace profile', exact: true }).getByRole('tab', { selected: true })
  const selectedName = await selected.count() === 1 ? await selected.getAttribute('aria-label') : null
  return PRODUCT_SURFACES.find((surface) => surface.label === selectedName)?.contract.toolbar?.profile
}

export async function censusControls(page, map, { state = 'ready', viewport = 'desktop', inventory = readControlInventory(map) } = {}) {
  // Enumerate first, independently of coverage, so missing registry entries
  // cannot remove controls from the census.
  const controls = await enumerateControls(page)
  const profile = await activeToolbarProfile(page)
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
