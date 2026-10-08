// @vitest-environment jsdom
// B1b: the object snap mode menu, the one controlled component the prompt and
// the status bar both mount. Checked state is the caller's; open and focus
// are the menu's own.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ObjectSnapMenu, { MENU_GAP, MENU_MARGIN, OBJECT_SNAP_HELP, SNAP_LIMITED_SENTENCE, placeObjectSnapMenu } from './ObjectSnapMenu.jsx'
import { ALL_SNAP_MODES, DEFAULT_SNAP_MODES, SNAP_MODES } from './snapModes.js'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

const KINDS = SNAP_MODES.map((mode) => mode.kind)
const trigger = () => screen.getByRole('button', { name: 'Object snap modes' })
const items = () => screen.getAllByRole('menuitemcheckbox')
const menu = () => screen.queryByRole('menu')
const checkedKinds = () => items().filter((el) => el.getAttribute('aria-checked') === 'true').map((el) => el.dataset.kind)
const key = (el, k) => fireEvent.keyDown(el, { key: k })

function mount({ snapModes = DEFAULT_SNAP_MODES, snapLimited = false, onSetMode = vi.fn() } = {}) {
  const utils = render(<ObjectSnapMenu snapModes={snapModes} snapLimited={snapLimited} onSetMode={onSetMode} />)
  return { ...utils, onSetMode }
}

describe('ObjectSnapMenu', () => {
  it('B1B-M01 controlled checkbox semantics', () => {
    const { onSetMode, rerender } = mount()
    expect(trigger().getAttribute('type')).toBe('button')
    expect(trigger().getAttribute('aria-haspopup')).toBe('menu')
    expect(trigger().getAttribute('aria-expanded')).toBe('false')
    expect(menu()).toBeNull()
    fireEvent.click(trigger())
    expect(trigger().getAttribute('aria-expanded')).toBe('true')
    expect(menu().getAttribute('aria-label')).toBe('Object snap modes')
    expect(menu().hasAttribute('data-escape-owner')).toBe(true)
    expect(trigger().getAttribute('aria-controls')).toBe(menu().id)
    // Nine checkboxes in SNAP_MODES order, checked from the mask alone.
    expect(items().map((el) => el.dataset.kind)).toEqual(KINDS)
    expect(items().map((el) => el.querySelector('.object-snap-label').textContent)).toEqual(SNAP_MODES.map((mode) => mode.label))
    // The check glyph is decoration; the accessible state is aria-checked.
    expect(items()[0].querySelector('.object-snap-check').getAttribute('aria-hidden')).toBe('true')
    expect(checkedKinds()).toEqual(['endpoint', 'midpoint', 'centre', 'quadrant'])
    // Enter, Space and a click each write one absolute value; the menu stays
    // open and nothing changes until the caller passes the new mask.
    key(items()[0], 'Enter')
    expect(onSetMode).toHaveBeenLastCalledWith('endpoint', false)
    key(items()[0], 'ArrowDown')
    key(items()[1], ' ')
    expect(onSetMode).toHaveBeenLastCalledWith('midpoint', false)
    fireEvent.click(items()[4])
    expect(onSetMode).toHaveBeenLastCalledWith('intersection', true)
    expect(onSetMode).toHaveBeenCalledTimes(3)
    expect(menu()).not.toBeNull()
    expect(checkedKinds()).toEqual(['endpoint', 'midpoint', 'centre', 'quadrant'])
    rerender(<ObjectSnapMenu snapModes={DEFAULT_SNAP_MODES | 32} snapLimited={false} onSetMode={onSetMode} />)
    expect(checkedKinds()).toEqual(['endpoint', 'midpoint', 'centre', 'quadrant', 'intersection'])
    expect(items()[4].getAttribute('aria-checked')).toBe('true')
    // A malformed mask checks nothing rather than guessing.
    rerender(<ObjectSnapMenu snapModes={2 ** 40} snapLimited={false} onSetMode={onSetMode} />)
    expect(checkedKinds()).toEqual([])
    // Two instances have distinct menu ids.
    render(<ObjectSnapMenu snapModes={0} snapLimited={false} onSetMode={onSetMode} />)
    const triggers = screen.getAllByRole('button', { name: 'Object snap modes' })
    expect(new Set(triggers.map((el) => el.getAttribute('aria-controls'))).size).toBe(2)
  })

  it('B1B-M02 keyboard navigation', () => {
    mount()
    for (const opener of ['Enter', ' ', 'ArrowDown']) {
      act(() => trigger().focus())
      expect(key(trigger(), opener)).toBe(false)
      expect(menu()).not.toBeNull()
      expect(document.activeElement).toBe(items()[0])
      key(items()[0], 'Escape')
      expect(menu()).toBeNull()
    }
    key(trigger(), 'ArrowUp')
    expect(document.activeElement).toBe(items()[8])
    // Roving focus: the focused item is the only tab stop.
    expect(items().map((el) => el.tabIndex)).toEqual([-1, -1, -1, -1, -1, -1, -1, -1, 0])
    key(items()[8], 'ArrowDown')
    expect(document.activeElement).toBe(items()[0])
    key(items()[0], 'ArrowUp')
    expect(document.activeElement).toBe(items()[8])
    key(items()[8], 'ArrowUp')
    expect(document.activeElement).toBe(items()[7])
    key(items()[7], 'Home')
    expect(document.activeElement).toBe(items()[0])
    key(items()[0], 'End')
    expect(document.activeElement).toBe(items()[8])
    expect(items().map((el) => el.tabIndex)).toEqual([-1, -1, -1, -1, -1, -1, -1, -1, 0])
    // A click on the open trigger closes it.
    fireEvent.click(trigger())
    expect(menu()).toBeNull()
  })

  it('B1B-M03 Escape owns only menu', () => {
    const owner = vi.fn()
    const windowKeys = vi.fn()
    window.addEventListener('keydown', windowKeys)
    try {
      render(
        <div onKeyDown={(event) => owner(event.key)}>
          <ObjectSnapMenu snapModes={DEFAULT_SNAP_MODES} snapLimited={false} onSetMode={() => {}} />
        </div>,
      )
      fireEvent.click(trigger())
      key(items()[0], 'ArrowDown')
      const notPrevented = key(items()[1], 'Escape')
      expect(notPrevented).toBe(false)
      expect(menu()).toBeNull()
      expect(document.activeElement).toBe(trigger())
      // Neither the surrounding prompt nor the window ladder saw this Esc.
      expect(owner).not.toHaveBeenCalledWith('Escape')
      expect(windowKeys).not.toHaveBeenCalled()
      // Enter and the arrows inside the menu stay inside it too.
      fireEvent.click(trigger())
      key(items()[0], 'Enter')
      key(items()[0], 'ArrowDown')
      expect(owner).not.toHaveBeenCalled()
      key(items()[1], 'Escape')
      // With the menu closed, Esc on the trigger is the surroundings' again.
      expect(key(trigger(), 'Escape')).toBe(true)
      expect(owner).toHaveBeenCalledWith('Escape')
    } finally {
      window.removeEventListener('keydown', windowKeys)
    }
  })

  it('B1B-M04 outside and Tab close', () => {
    const add = vi.spyOn(document, 'addEventListener')
    const remove = vi.spyOn(document, 'removeEventListener')
    const outside = document.createElement('button')
    outside.textContent = 'elsewhere'
    document.body.appendChild(outside)
    try {
      const { unmount } = mount()
      fireEvent.click(trigger())
      // A pointerdown inside the menu keeps it.
      fireEvent.pointerDown(items()[2])
      expect(menu()).not.toBeNull()
      // Outside: closes, and focus stays where the pointer put it.
      act(() => outside.focus())
      fireEvent.pointerDown(outside)
      expect(menu()).toBeNull()
      expect(document.activeElement).toBe(outside)
      expect(trigger().getAttribute('aria-expanded')).toBe('false')
      // Tab closes without being consumed, so focus moves on normally.
      fireEvent.click(trigger())
      expect(key(items()[0], 'Tab')).toBe(true)
      expect(menu()).toBeNull()
      // Unmounting an open menu removes its document listener.
      fireEvent.click(trigger())
      const listener = add.mock.calls.filter(([type]) => type === 'pointerdown').at(-1)[1]
      unmount()
      expect(remove).toHaveBeenCalledWith('pointerdown', listener, true)
    } finally {
      outside.remove()
    }
  })

  it('B1B-M05 viewport placement', () => {
    const viewport = { width: 1000, height: 800 }
    // Room below: under the trigger, clamped to the left margin.
    expect(placeObjectSnapMenu({ top: 10, bottom: 30, left: -50 }, { width: 200, height: 300 }, viewport))
      .toEqual({ top: 30 + MENU_GAP, left: MENU_MARGIN, maxHeight: 800 - MENU_MARGIN - 30 - MENU_GAP, placement: 'below' })
    // A status bar trigger: flipped above, clamped to the right margin.
    expect(placeObjectSnapMenu({ top: 770, bottom: 790, left: 900 }, { width: 200, height: 300 }, viewport))
      .toEqual({ top: 770 - MENU_GAP - 300, left: 1000 - MENU_MARGIN - 200, maxHeight: 770 - MENU_GAP - MENU_MARGIN, placement: 'above' })
    // Taller than the room above: held to it, scrolling, never off screen.
    const tall = placeObjectSnapMenu({ top: 370, bottom: 390, left: 100 }, { width: 200, height: 900 }, { width: 1000, height: 400 })
    expect(tall).toEqual({ top: MENU_MARGIN, left: 100, maxHeight: 370 - MENU_GAP - MENU_MARGIN, placement: 'above' })
    // Neither side fits and below is larger: stays below with its room.
    expect(placeObjectSnapMenu({ top: 100, bottom: 120, left: 100 }, { width: 200, height: 900 }, { width: 1000, height: 400 }))
      .toEqual({ top: 120 + MENU_GAP, left: 100, maxHeight: 400 - MENU_MARGIN - 120 - MENU_GAP, placement: 'below' })
    // Wider than the viewport: pinned to the left margin.
    expect(placeObjectSnapMenu({ top: 10, bottom: 30, left: 400 }, { width: 2000, height: 10 }, viewport).left).toBe(MENU_MARGIN)
    // Bad input never yields a non-finite position.
    const bad = placeObjectSnapMenu(null, null, null)
    expect(Object.values(bad).slice(0, 3).every(Number.isFinite)).toBe(true)

    // Rendered: the popup is written from the trigger's rect on open and resize.
    let rect = { top: window.innerHeight - 28, bottom: window.innerHeight - 8, left: window.innerWidth - 24, right: window.innerWidth - 4, width: 20, height: 20 }
    const zero = { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function box() {
      if (this.classList.contains('object-snap-trigger')) return rect
      if (this.classList.contains('object-snap-popup')) return { ...zero, width: 200, height: 300 }
      return zero
    })
    mount()
    fireEvent.click(trigger())
    const popup = menu().parentElement
    const view = { width: window.innerWidth, height: window.innerHeight }
    const expected = placeObjectSnapMenu(rect, { width: 200, height: 300 }, view)
    expect(expected.placement).toBe('above')
    expect(popup.style.top).toBe(`${expected.top}px`)
    expect(popup.style.left).toBe(`${expected.left}px`)
    expect(popup.style.maxHeight).toBe(`${expected.maxHeight}px`)
    expect(popup.getAttribute('data-placement')).toBe('above')
    rect = { top: 40, bottom: 60, left: 20, right: 40, width: 20, height: 20 }
    act(() => { window.dispatchEvent(new Event('resize')) })
    const moved = placeObjectSnapMenu(rect, { width: 200, height: 300 }, view)
    expect(moved.placement).toBe('below')
    expect(popup.style.top).toBe(`${moved.top}px`)
    expect(popup.style.left).toBe(`${moved.left}px`)
    expect(popup.getAttribute('data-placement')).toBe('below')
  })

  it('B1B-M06 master-independent selection and help', () => {
    function Host() {
      const [mask, setMask] = useState(0)
      return (
        <ObjectSnapMenu
          snapModes={mask}
          snapLimited={false}
          onSetMode={(kind, enabled) => {
            const bit = SNAP_MODES.find((mode) => mode.kind === kind).bit
            setMask((current) => (enabled ? current | bit : current & ~bit))
          }}
        />
      )
    }
    render(<Host />)
    fireEvent.click(trigger())
    // Zero selected modes is a real state.
    expect(checkedKinds()).toEqual([])
    for (const el of items()) fireEvent.click(el)
    expect(checkedKinds()).toEqual(KINDS)
    expect(SNAP_MODES.reduce((mask, mode) => mask | mode.bit, 0)).toBe(ALL_SNAP_MODES)
    fireEvent.click(items()[8])
    expect(checkedKinds()).toEqual(KINDS.slice(0, 8))
    // The help sentence describes the menu; the limitation line shows only when limited.
    const help = document.getElementById(menu().getAttribute('aria-describedby'))
    expect(help.textContent).toBe(OBJECT_SNAP_HELP)
    expect(OBJECT_SNAP_HELP).toBe('Nearest, intersection and perpendicular support lines, circles, arcs and polyline segments. Tangent supports circles, arcs and curved polyline segments. Perpendicular and tangent need a previous point. Insertion uses block and single-line text insertion points. Ellipses offer centre only. Multiline text is not supported.')
    expect(screen.queryByText(SNAP_LIMITED_SENTENCE)).toBeNull()
    cleanup()
    mount({ snapLimited: true })
    fireEvent.click(trigger())
    expect(SNAP_LIMITED_SENTENCE).toBe('Object snap limited near this point.')
    expect(screen.getByText(SNAP_LIMITED_SENTENCE)).not.toBeNull()
    // The menu has no master control of its own: every write is a mode.
    cleanup()
    const { onSetMode } = mount({ snapModes: 0 })
    fireEvent.click(trigger())
    fireEvent.click(items()[KINDS.indexOf('tangent')])
    expect(onSetMode.mock.calls).toEqual([['tangent', true]])
    expect(menu()).not.toBeNull()
  })
})
