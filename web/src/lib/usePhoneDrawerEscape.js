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
  useEscapeOwner(PHONE_DRAWER_ESCAPE_ID, phoneDrawerOpen(state), onClose, { layer: 'drawer' })
}
