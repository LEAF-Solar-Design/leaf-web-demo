// KEYS-b: the open phone Studio drawer is an Escape owner.
//
// On a phone the Studio shell shows one drawer at a time (Catalog, Jobs,
// Result, Plan). Until this hook the drawer closed only through the shell's
// own Escape rung, which runs AFTER the owner stack: any registered owner
// below the drawer on the section 7 ladder (an armed command, the version
// history, a running turn) took the key first and the drawer stayed open.
//
// The drawer registers at the drawer layer while it is open, so it closes
// before every lower layer and still yields to a menu or a sheet above it.
// It passes no scope: the App root as a scope would adopt unrelated foreign
// markers as its own. Contract: no listener of its own, no allocation per
// key; the stack reads the close handler through a ref.

import { useLayoutEffect, useRef } from 'react'
import { STUDIO_DRAWERS } from './studioDrawers.js'
import useEscapeOwner from './useEscapeOwner.js'

/** The owner id the drawer registers under (diagnostics and tests). */
export const PHONE_DRAWER_ESCAPE_ID = 'studio-drawer'

/**
 * True while a phone Studio drawer is open. Fails closed: anything but the
 * Studio shell, the phone viewport and a known drawer name answers false.
 */
export function phoneDrawerOpen(state) {
  if (!state || state.studioShell !== true || state.phoneViewport !== true) return false
  const name = state.studioDrawer
  return typeof name === 'string' && name !== 'none' && STUDIO_DRAWERS.includes(name)
}

/**
 * Register the open phone Studio drawer with the one Escape owner stack.
 *
 *   state    { studioShell, phoneViewport, studioDrawer } from the shell.
 *   onClose  closes the drawer (the shell's own setter); read through a ref.
 */
export default function usePhoneDrawerEscape(state, onClose) {
  const open = phoneDrawerOpen(state)
  const name = state?.studioDrawer
  const restoreFocus = useRef(null)
  useLayoutEffect(() => {
    if (!open) return undefined
    const shell = document.querySelector('.app[data-studio-shell="cockpit"]')
    if (!shell) return undefined
    const selectors = {
      nav: ':scope > aside.nav',
      jobs: ':scope > .rail-stack > aside.rail',
      result: '.result-block',
      plan: '.studio-profile-info, main.center-scroll > section.ent-panel',
    }
    const panel = shell.querySelector(selectors[name])
    const trigger = shell.querySelectorAll('.studio-drawer-tabs button')[STUDIO_DRAWERS.indexOf(name)]
    if (!panel || !trigger) return undefined
    const target = panel.querySelector('h1, h2, h3, h4, [role="heading"], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]') || panel
    const previousTabIndex = target.getAttribute('tabindex')
    if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1')
    target.focus({ preventScroll: true })
    const restore = () => {
      if (panel.contains(document.activeElement) && trigger.isConnected) trigger.focus({ preventScroll: true })
    }
    restoreFocus.current = restore
    return () => {
      restore()
      restoreFocus.current = null
      if (previousTabIndex === null) target.removeAttribute('tabindex')
    }
  }, [open, name])
  useEscapeOwner(PHONE_DRAWER_ESCAPE_ID, open, () => {
    // Hand focus back before the state update hides the focused content.
    restoreFocus.current?.()
    onClose?.()
  }, { layer: 'drawer' })
}
