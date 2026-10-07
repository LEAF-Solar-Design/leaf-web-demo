// S27: ONE Escape owner stack for the whole page (motion standard section 7).
//
// Every surface that closes on Escape registers here instead of binding its
// own keydown listener and guessing, by selector string, whether something
// above it is open. The stack installs ONE window capture-phase listener while
// it holds an owner, and Escape always goes to the topmost owner:
//
//   1. the semantic layer (ESCAPE_LAYERS, the section 7 ladder, C13 default):
//      a menu outranks a sheet, a sheet a drawer, and so on down to the scene;
//   2. within one layer, an owner whose scope sits INSIDE another owner's
//      scope outranks it (nesting), so a child surface closes before its host
//      whatever order their effects ran in;
//   3. otherwise the owner activated most recently wins (stack order).
//
// The winner's key is consumed (preventDefault + stopPropagation), so nothing
// below it, including App's global key ladder, ever reads the same keypress.
// A key nobody claims is left alone: the ladder (actionRegistry ESCAPE_RUNGS)
// still owns the shell's own rungs, and `escapeRung` reads this stack first.
//
// Contract: no allocation on a non-Escape key; the listener is attached only
// while at least one owner is active; an owner's handler is read through a
// ref, so a re-render never re-orders the stack.

import { useLayoutEffect, useRef } from 'react'

/** The section 7 ladder, topmost first. Higher rank closes first. */
export const ESCAPE_LAYERS = Object.freeze({
  menu: 100,      // anchored menus, pickers, resolvers, popovers
  sheet: 90,      // sheets and modal dialogs
  drawer: 80,     // drawers over the rails
  history: 70,    // the version history drawer
  edit: 60,       // a staged edit or an inline editor
  command: 50,    // an armed command or a pick in progress
  proposal: 40,   // a route proposal or a coach card
  run: 30,        // a running job or turn
  focus: 20,      // a focused object or board
  scene: 10,      // the scene itself (the marketing eject)
})

// A layer this stack does not own announces itself with this marker (the
// object snap menu, a test's stand-in layer), or is an open ARIA menu (the element
// context menu, whose library closes it on its own document listener). While
// one is visible and no registered owner's scope holds it, the stack yields so
// that layer's own handler runs.
const FOREIGN_OWNER_SELECTOR = '[data-escape-owner], [role="menu"]'

const active = []          // active owner records, unordered
const mounted = new Set()  // every mounted record, active or not
let activations = 0
let listening = false

function layerRank(layer) {
  const rank = ESCAPE_LAYERS[layer]
  if (typeof rank !== 'number') throw new TypeError(`useEscapeOwner: unknown layer "${layer}"`)
  return rank
}

function scopeOf(record) {
  const node = record.scopeRef?.current
  return node && typeof node.contains === 'function' ? node : null
}

function targetOf(event) {
  if (event && event.target) return event.target
  return typeof document !== 'undefined' ? document.activeElement : null
}

function claims(record, event) {
  if (record.scoped) {
    const scope = scopeOf(record)
    const target = targetOf(event)
    if (!scope || !(target && typeof Node !== 'undefined' && target instanceof Node) || !scope.contains(target)) return false
  }
  const when = record.whenRef.current
  return typeof when === 'function' ? when(event) !== false : true
}

/** True when `a` sits above `b` on the stack. */
function above(a, b) {
  if (a.rank !== b.rank) return a.rank > b.rank
  const sa = scopeOf(a)
  const sb = scopeOf(b)
  if (sa && sb && sa !== sb) {
    if (sb.contains(sa)) return true
    if (sa.contains(sb)) return false
  }
  return a.seq > b.seq
}

function visible(layer) {
  if (layer.hidden || layer.hasAttribute('inert') || layer.getAttribute('aria-hidden') === 'true') return false
  if (typeof HTMLDialogElement !== 'undefined' && layer instanceof HTMLDialogElement && !layer.open) return false
  const style = window.getComputedStyle(layer)
  return style.display !== 'none' && style.visibility !== 'hidden'
}

// A marker is ours when it sits inside the surface of a mounted owner. A
// scoped owner's scope is a focus REGION (a board, a form), not a surface, so
// a foreign menu inside it stays foreign.
function foreignOwnerVisible() {
  if (typeof document === 'undefined') return false
  for (const layer of document.querySelectorAll(FOREIGN_OWNER_SELECTOR)) {
    let ours = false
    for (const record of mounted) {
      if (record.scoped) continue
      const scope = scopeOf(record)
      if (scope && scope.contains(layer)) { ours = true; break }
    }
    if (!ours && visible(layer)) return true
  }
  return false
}

/**
 * The owner that would take this Escape, or null when none would. `event` may
 * be omitted (the registry asks without one); scoped owners then judge the
 * focused element.
 */
export function topEscapeOwner(event) {
  if (active.length === 0) return null
  if (foreignOwnerVisible()) return null
  let top = null
  for (const record of active) {
    if (!claims(record, event)) continue
    if (!top || above(record, top)) top = record
  }
  return top
}

/** The id of the owner that would take Escape now, or '' (actionRegistry's escapeRung reads this). */
export function topEscapeOwnerId(event) {
  return topEscapeOwner(event)?.id || ''
}

/** Run the topmost owner as if Escape were pressed. Returns its id, or '' when nothing was open. */
export function closeTopEscapeOwner(event) {
  const top = topEscapeOwner(event)
  if (!top) return ''
  top.handlerRef.current?.(event)
  return top.id
}

/** The active owners' ids, topmost first. Diagnostics and tests only. */
export function escapeOwnerStack() {
  return [...active].sort((a, b) => (above(a, b) ? -1 : above(b, a) ? 1 : 0)).map((record) => record.id)
}

function onWindowKeyDown(event) {
  if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing) return
  const top = topEscapeOwner(event)
  if (!top) return
  event.preventDefault()
  event.stopPropagation()
  top.handlerRef.current?.(event)
}

function sync() {
  if (typeof window === 'undefined') return
  if (active.length > 0 && !listening) {
    window.addEventListener('keydown', onWindowKeyDown, true)
    listening = true
  } else if (active.length === 0 && listening) {
    window.removeEventListener('keydown', onWindowKeyDown, true)
    listening = false
  }
}

function activate(record) {
  if (active.includes(record)) return
  record.seq = ++activations
  active.push(record)
  sync()
}

function deactivate(record) {
  const at = active.indexOf(record)
  if (at >= 0) active.splice(at, 1)
  sync()
}

function createOwnerRecord(id, handlerRef, whenRef, options) {
  const { layer = 'drawer', scope = null, scoped = false } = options
  return { id: String(id), rank: layerRank(layer), seq: 0, scopeRef: scope, scoped: !!scoped, handlerRef, whenRef }
}

function mountOwner(record) {
  mounted.add(record)
  return () => { mounted.delete(record); deactivate(record) }
}

/** Register an active Escape owner without React; unregister when its surface closes. */
export function registerEscapeOwner(id, onEscape, options = {}) {
  const record = createOwnerRecord(id, { current: onEscape }, { current: options.when ?? null }, options)
  const unregister = mountOwner(record)
  activate(record)
  return unregister
}

/**
 * Register a surface as an Escape owner while `open` is true.
 *
 *   id        a stable name for diagnostics and the registry ('details', ...).
 *   open      whether the surface is up; the owner is pushed when it turns true.
 *   onEscape  what Escape does (close, cancel, detach). Read through a ref.
 *   options.layer   an ESCAPE_LAYERS key (default 'drawer').
 *   options.scope   a ref to the surface's root; orders nested owners.
 *   options.scoped  claim only a key whose target is inside `scope`.
 *   options.when    (event) => false declines this key (it falls to the next owner).
 *
 * Fails closed at the call: an unknown layer throws on the first render.
 */
export default function useEscapeOwner(id, open, onEscape, options = {}) {
  const { layer = 'drawer', scope = null, scoped = false, when = null } = options
  const rank = layerRank(layer)
  const handlerRef = useRef(onEscape)
  handlerRef.current = onEscape
  const whenRef = useRef(when)
  whenRef.current = when
  const recordRef = useRef(null)
  if (!recordRef.current) {
    recordRef.current = createOwnerRecord(id, handlerRef, whenRef, options)
  }
  const record = recordRef.current
  record.id = String(id)
  record.rank = rank
  record.scopeRef = scope
  record.scoped = !!scoped

  useLayoutEffect(() => mountOwner(record), [record])

  // Match ownership to the committed UI before another Escape can arrive.
  // Passive cleanup can leave a dismissed proposal above the scene even
  // after its DOM has gone and focus has been handed back to the command bar.
  useLayoutEffect(() => {
    if (!open) return undefined
    activate(record)
    return () => deactivate(record)
  }, [open, record])
}
