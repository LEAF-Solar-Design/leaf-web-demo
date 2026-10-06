/**
 * B1b: the object snap mode menu, ONE controlled component mounted twice
 * (beside the prompt's OSNAP button and beside the live status bar OSNAP
 * button). Checked state is the caller's (`snapModes`, a snapModes.js mask);
 * only open and focus are local. A checkbox writes one absolute
 * `onSetMode(kind, enabled)` and never touches the OSNAP master, so a mode can
 * be chosen with snapping off.
 *
 * Keyboard (menu button pattern): Enter, Space or ArrowDown on the trigger
 * opens on the first item, ArrowUp on the last; inside, the arrows wrap,
 * Home/End jump, Enter/Space change the focused checkbox and keep the menu
 * open, Escape closes and returns focus to the trigger (and is consumed, so
 * it never also cancels the drawing command), Tab closes and lets focus move
 * on. A pointerdown outside closes without taking focus from its target.
 *
 * Placement is a DOM write per open and per resize or scroll, never React
 * state: below the trigger, flipped above it when the room below is short
 * (the status bar sits at the bottom), clamped to the viewport's width, and
 * held to the viewport's height with scrolling.
 */
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'

import './ObjectSnapMenu.css'
import { SNAP_MODES, snapModeEnabled } from './snapModes.js'

export const OBJECT_SNAP_MENU_NAME = 'Object snap modes'
export const SNAP_LIMITED_SENTENCE = 'Object snap limited near this point.'
export const OBJECT_SNAP_HELP = 'Nearest, intersection and perpendicular support lines, circles, arcs and polyline segments. Tangent supports circles, arcs and curved polyline segments. Perpendicular and tangent need a previous point. Insertion uses block and single-line text insertion points. Ellipses offer centre only. Multiline text is not supported.'

export const MENU_GAP = 4
export const MENU_MARGIN = 8
const finiteOr0 = (v) => (Number.isFinite(v) ? v : 0)

/**
 * Where the menu goes: `trigger` is the trigger's client rect, `size` the
 * menu's natural { width, height }, `viewport` the window's { width, height }.
 * Returns { top, left, maxHeight, placement } in CSS pixels. Below the
 * trigger unless the menu does not fit there and the room above is larger;
 * left-aligned with the trigger and clamped inside the viewport's margins;
 * maxHeight is the room on the chosen side (never negative).
 */
export function placeObjectSnapMenu(trigger, size, viewport) {
  const top0 = finiteOr0(trigger?.top), bottom = finiteOr0(trigger?.bottom), left0 = finiteOr0(trigger?.left)
  const width = Math.max(0, finiteOr0(size?.width)), height = Math.max(0, finiteOr0(size?.height))
  const vw = finiteOr0(viewport?.width), vh = finiteOr0(viewport?.height)
  const below = vh - MENU_MARGIN - (bottom + MENU_GAP)
  const above = top0 - MENU_GAP - MENU_MARGIN
  const flip = height > below && above > below
  const room = Math.max(0, flip ? above : below)
  const shown = Math.min(height, room)
  const top = flip ? top0 - MENU_GAP - shown : bottom + MENU_GAP
  const left = Math.max(MENU_MARGIN, Math.min(left0, vw - MENU_MARGIN - width))
  return { top, left, maxHeight: room, placement: flip ? 'above' : 'below' }
}

export default function ObjectSnapMenu({ snapModes, snapLimited, onSetMode }) {
  const id = useId()
  const menuId = `object-snap-menu-${id}`
  const helpId = `object-snap-help-${id}`
  const [open, setOpen] = useState(false)
  const [focusIndex, setFocusIndex] = useState(0)
  const rootRef = useRef(null)
  const triggerRef = useRef(null)
  const popupRef = useRef(null)
  const itemRefs = useRef([])
  const onSetModeRef = useRef(onSetMode)
  onSetModeRef.current = onSetMode

  const openAt = (index) => { setFocusIndex(index); setOpen(true) }
  const close = (restoreFocus) => {
    setOpen(false)
    if (restoreFocus) triggerRef.current?.focus()
  }
  const setMode = (kind) => {
    if (typeof onSetModeRef.current === 'function') onSetModeRef.current(kind, !snapModeEnabled(snapModes, kind))
  }

  // Roving focus: the focused item is the one Tab stop inside the menu.
  useEffect(() => {
    if (open) itemRefs.current[focusIndex]?.focus()
  }, [open, focusIndex])

  // Outside pointerdown closes and leaves focus where the pointer put it.
  useEffect(() => {
    if (!open || typeof document === 'undefined') return undefined
    const onPointerDown = (event) => {
      const root = rootRef.current
      if (root && event.target instanceof Node && root.contains(event.target)) return
      setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown, true)
    return () => document.removeEventListener('pointerdown', onPointerDown, true)
  }, [open])

  useLayoutEffect(() => {
    if (!open || typeof window === 'undefined') return undefined
    const place = () => {
      const trigger = triggerRef.current
      const popup = popupRef.current
      if (!trigger || !popup) return
      popup.style.maxHeight = ''
      const box = popup.getBoundingClientRect()
      const at = placeObjectSnapMenu(
        trigger.getBoundingClientRect(),
        { width: box.width, height: Math.max(box.height, popup.scrollHeight) },
        { width: window.innerWidth, height: window.innerHeight },
      )
      popup.style.top = `${at.top}px`
      popup.style.left = `${at.left}px`
      popup.style.maxHeight = `${at.maxHeight}px`
      popup.setAttribute('data-placement', at.placement)
    }
    place()
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open])

  const onTriggerKeyDown = (event) => {
    if (event.key === 'Enter' || event.key === ' ' || event.key === 'ArrowDown') {
      event.preventDefault()
      event.stopPropagation()
      openAt(0)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      event.stopPropagation()
      openAt(SNAP_MODES.length - 1)
    } else if (event.key === 'Escape' && open) {
      event.preventDefault()
      event.stopPropagation()
      close(true)
    }
  }

  const onMenuKeyDown = (event) => {
    const count = SNAP_MODES.length
    const at = Number.isInteger(focusIndex) ? focusIndex : 0
    let next = null
    if (event.key === 'ArrowDown') next = (at + 1) % count
    else if (event.key === 'ArrowUp') next = (at - 1 + count) % count
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = count - 1
    if (next !== null) {
      event.preventDefault()
      event.stopPropagation()
      setFocusIndex(next)
      return
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      event.stopPropagation()
      setMode(SNAP_MODES[at].kind)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      close(true)
    } else if (event.key === 'Tab') {
      setOpen(false)
    }
  }

  return (
    <span className="object-snap" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="object-snap-trigger"
        aria-label={OBJECT_SNAP_MENU_NAME}
        title={OBJECT_SNAP_MENU_NAME}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => (open ? close(false) : openAt(0))}
        onKeyDown={onTriggerKeyDown}
      >
        <span aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div ref={popupRef} className="object-snap-popup" data-escape-owner="object-snap-menu" onKeyDown={onMenuKeyDown}>
          <div role="menu" id={menuId} className="object-snap-menu" aria-label={OBJECT_SNAP_MENU_NAME} aria-describedby={helpId} data-escape-owner="object-snap-menu">
            {SNAP_MODES.map(({ kind, label }, index) => {
              const checked = snapModeEnabled(snapModes, kind)
              return (
                <div
                  key={kind}
                  ref={(el) => { itemRefs.current[index] = el }}
                  role="menuitemcheckbox"
                  className="object-snap-item"
                  data-kind={kind}
                  aria-checked={checked}
                  tabIndex={index === focusIndex ? 0 : -1}
                  onClick={() => { setFocusIndex(index); setMode(kind) }}
                >
                  <span className="object-snap-check" aria-hidden="true">{checked ? '✓' : ''}</span>
                  <span className="object-snap-label">{label}</span>
                </div>
              )
            })}
          </div>
          {snapLimited === true ? <p className="object-snap-limited">{SNAP_LIMITED_SENTENCE}</p> : null}
          <p id={helpId} className="object-snap-help">{OBJECT_SNAP_HELP}</p>
        </div>
      ) : null}
    </span>
  )
}
