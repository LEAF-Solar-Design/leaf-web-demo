// @vitest-environment jsdom
// KEYS-b: the open phone Studio drawer is an Escape owner at the drawer layer.
// Every row runs the REAL owner stack (lib/useEscapeOwner.js) and the REAL
// hook; the owners below and above the drawer are registered with the options
// their production callers pass (quoted beside each), and the version history
// row mounts the real VersionHistory. The last rows run the real key ladder
// (actionRegistry ladderListener) behind the stack, as App mounts it.
import { createElement, useRef, useState } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import VersionHistory from '../components/VersionHistory.jsx'
import { ladderListener } from './actionRegistry.js'
import { STUDIO_DRAWERS } from './studioDrawers.js'
import useEscapeOwner, { ESCAPE_LAYERS, escapeOwnerStack, topEscapeOwnerId } from './useEscapeOwner.js'
import usePhoneDrawerEscape, { PHONE_DRAWER_ESCAPE_ID, phoneDrawerOpen } from './usePhoneDrawerEscape.js'

vi.mock('../api.js', () => ({
  config: { mockDefault: false },
  getDrawingVersions: vi.fn(),
  restoreDrawingVersion: vi.fn(),
}))

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

const h = createElement
const OPEN = ['nav', 'jobs', 'result', 'plan']
const press = (target = window) => fireEvent.keyDown(target, { key: 'Escape' })

// The shell's own slice of state: the drawer name, the two gates, the four
// tab buttons App renders in .studio-drawer-tabs, and the real hook closing
// the drawer exactly as App does (setStudioDrawer('none')).
function Shell({ initial = 'nav', studioShell = true, phoneViewport = true, onClosed = null, children = null }) {
  const [studioDrawer, setStudioDrawer] = useState(initial)
  usePhoneDrawerEscape({ studioShell, phoneViewport, studioDrawer }, () => {
    onClosed?.(studioDrawer)
    setStudioDrawer('none')
  })
  return h('div', { 'data-testid': 'shell', 'data-drawer': studioDrawer },
    h('div', { role: 'group', 'aria-label': 'Workspace panels' },
      OPEN.map((name) => h('button', {
        key: name, type: 'button', 'aria-expanded': studioDrawer === name,
        onClick: () => setStudioDrawer((current) => (current === name ? 'none' : name)),
      }, name))),
    children)
}

// A stand-in owner with a production caller's options.
function Owner({ id, layer, onEscape, when = null, open = true, withScope = false }) {
  const scope = useRef(null)
  useEscapeOwner(id, open, onEscape, withScope ? { layer, scope, when } : { layer, when })
  return h('div', { ref: scope, 'data-testid': id })
}

// Mounts its children only after its button is pressed, so an owner can be
// activated AFTER the drawer (the stack breaks a same-layer tie by activation
// order, so both orders are what proves the layer decides).
function Late({ label, children }) {
  const [on, setOn] = useState(false)
  return h('div', null, h('button', { type: 'button', onClick: () => setOn(true) }, label), on ? children : null)
}

const drawer = () => screen.getByTestId('shell').getAttribute('data-drawer')

describe('KEYS-b the phone Studio drawer gate', () => {
  it('KEYS-B01 answers true only for an open drawer on the phone Studio shell', () => {
    for (const name of OPEN) {
      expect(phoneDrawerOpen({ studioShell: true, phoneViewport: true, studioDrawer: name })).toBe(true)
      // Desktop rails and a non-Studio shell stay outside the owner stack.
      expect(phoneDrawerOpen({ studioShell: true, phoneViewport: false, studioDrawer: name })).toBe(false)
      expect(phoneDrawerOpen({ studioShell: false, phoneViewport: true, studioDrawer: name })).toBe(false)
    }
    expect(phoneDrawerOpen({ studioShell: true, phoneViewport: true, studioDrawer: 'none' })).toBe(false)
    // Fails closed on anything it does not recognise.
    for (const studioDrawer of ['', 'details', 'constructor', null, undefined, 1, true, ['nav']]) {
      expect(phoneDrawerOpen({ studioShell: true, phoneViewport: true, studioDrawer })).toBe(false)
    }
    for (const gate of [1, 'true', null, undefined]) {
      expect(phoneDrawerOpen({ studioShell: gate, phoneViewport: true, studioDrawer: 'nav' })).toBe(false)
      expect(phoneDrawerOpen({ studioShell: true, phoneViewport: gate, studioDrawer: 'nav' })).toBe(false)
    }
    for (const state of [null, undefined, 0, '']) expect(phoneDrawerOpen(state)).toBe(false)
    // The gate knows exactly the shell's own drawer names.
    expect(STUDIO_DRAWERS.filter((name) => name !== 'none')).toEqual(OPEN)
  })

  it('KEYS-B02 registers one drawer-layer owner while a drawer is open and none otherwise', () => {
    expect(escapeOwnerStack()).toEqual([])
    const { unmount } = render(h(Shell, { initial: 'none' }))
    expect(escapeOwnerStack()).toEqual([])
    for (const name of OPEN) {
      fireEvent.click(screen.getByRole('button', { name }))
      expect(drawer()).toBe(name)
      expect(escapeOwnerStack()).toEqual([PHONE_DRAWER_ESCAPE_ID])
      expect(topEscapeOwnerId()).toBe(PHONE_DRAWER_ESCAPE_ID)
      // Closing through the tab leaves the stack empty again.
      fireEvent.click(screen.getByRole('button', { name }))
      expect(drawer()).toBe('none')
      expect(escapeOwnerStack()).toEqual([])
    }
    fireEvent.click(screen.getByRole('button', { name: 'plan' }))
    unmount()
    expect(escapeOwnerStack()).toEqual([])
    // Desktop and a non-Studio shell never register, drawer name or not.
    render(h(Shell, { initial: 'nav', phoneViewport: false }))
    expect(escapeOwnerStack()).toEqual([])
    cleanup()
    render(h(Shell, { initial: 'jobs', studioShell: false }))
    expect(escapeOwnerStack()).toEqual([])
    expect(ESCAPE_LAYERS.drawer).toBe(80)
  })
})

describe('KEYS-b one Escape closes the phone drawer and nothing below it', () => {
  it('KEYS-B03 an armed command keeps its command while the drawer closes', () => {
    const cancel = vi.fn()
    // EngineRibbonClusters: useEscapeOwner('armed-command', !!armedOp, ..., { layer: 'command', when: armedOwnsEscape });
    // a key from a plain button outside the prompt is the armed command's.
    const armedOwnsEscape = (event) => !(event?.target instanceof HTMLElement
      && /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName))
    render(h(Shell, { initial: 'none' }, h(Owner, { id: 'armed-command', layer: 'command', onEscape: cancel, when: armedOwnsEscape })))
    // Without a drawer the armed command owns the key, as on main.
    press(screen.getByRole('button', { name: 'nav' }))
    expect(cancel).toHaveBeenCalledTimes(1)
    cancel.mockClear()
    for (const name of OPEN) {
      const tab = screen.getByRole('button', { name })
      fireEvent.click(tab)
      expect(escapeOwnerStack()).toEqual([PHONE_DRAWER_ESCAPE_ID, 'armed-command'])
      press(tab)
      expect(drawer()).toBe('none')
      expect(cancel).not.toHaveBeenCalled()
    }
    // The next press reaches the command.
    press(screen.getByRole('button', { name: 'nav' }))
    expect(cancel).toHaveBeenCalledTimes(1)
    cleanup()
    // The other order: the Catalog is open first and LINE is armed after it.
    const lateCancel = vi.fn()
    render(h(Shell, { initial: 'nav' }, h(Late, { label: 'arm line' },
      h(Owner, { id: 'armed-command', layer: 'command', onEscape: lateCancel, when: armedOwnsEscape }))))
    fireEvent.click(screen.getByRole('button', { name: 'arm line' }))
    expect(escapeOwnerStack()).toEqual([PHONE_DRAWER_ESCAPE_ID, 'armed-command'])
    press(screen.getByRole('button', { name: 'nav' }))
    expect(drawer()).toBe('none')
    expect(lateCancel).not.toHaveBeenCalled()
    press(screen.getByRole('button', { name: 'nav' }))
    expect(lateCancel).toHaveBeenCalledTimes(1)
  })

  it('KEYS-B04 the real version history stays open while the drawer closes', () => {
    const onClose = vi.fn()
    const data = { drawing_id: 'drawing-1', head: 2, latest: 2, versions: [{ v: 2 }, { v: 1 }] }
    render(h(Shell, { initial: 'none' },
      h(VersionHistory, { data, onClose, onPreview: () => {}, onRetry: () => {} })))
    expect(escapeOwnerStack()).toEqual(['version-history'])
    // The drawer opens AFTER the history and on the other side of it: the
    // layer decides, not the order.
    fireEvent.click(screen.getByRole('button', { name: 'nav' }))
    expect(escapeOwnerStack()).toEqual([PHONE_DRAWER_ESCAPE_ID, 'version-history'])
    press(screen.getByRole('button', { name: 'nav' }))
    expect(drawer()).toBe('none')
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Close version history' })).toBeInTheDocument()
    press(screen.getByRole('button', { name: 'nav' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    cleanup()
    // The other order: the drawer is open first and the history opens after it.
    const lateClose = vi.fn()
    render(h(Shell, { initial: 'plan' }, h(Late, { label: 'open history' },
      h(VersionHistory, { data, onClose: lateClose, onPreview: () => {}, onRetry: () => {} }))))
    fireEvent.click(screen.getByRole('button', { name: 'open history' }))
    expect(escapeOwnerStack()).toEqual([PHONE_DRAWER_ESCAPE_ID, 'version-history'])
    press(screen.getByRole('button', { name: 'plan' }))
    expect(drawer()).toBe('none')
    expect(lateClose).not.toHaveBeenCalled()
    press(screen.getByRole('button', { name: 'plan' }))
    expect(lateClose).toHaveBeenCalledTimes(1)
  })

  it('KEYS-B05 a running turn is not interrupted while the drawer closes', () => {
    // ConversePanel: useEscapeOwner('converse-interrupt', <busy>, ..., { layer: 'run' }).
    for (const turnLater of [false, true]) {
      const cancelTurn = vi.fn()
      const turn = h(Owner, { id: 'converse-interrupt', layer: 'run', onEscape: cancelTurn })
      render(h(Shell, { initial: 'jobs' }, turnLater ? h(Late, { label: 'start turn' }, turn) : turn))
      if (turnLater) fireEvent.click(screen.getByRole('button', { name: 'start turn' }))
      expect(escapeOwnerStack()).toEqual([PHONE_DRAWER_ESCAPE_ID, 'converse-interrupt'])
      press(screen.getByRole('button', { name: 'jobs' }))
      expect(drawer()).toBe('none')
      expect(cancelTurn).not.toHaveBeenCalled()
      press()
      expect(cancelTurn).toHaveBeenCalledTimes(1)
      cleanup()
    }
  })

  it('KEYS-B06 the drawer closes before every lower layer, whichever opened first', () => {
    for (const layer of ['history', 'edit', 'command', 'proposal', 'run', 'focus', 'scene']) {
      expect(ESCAPE_LAYERS[layer]).toBeLessThan(ESCAPE_LAYERS.drawer)
      for (const belowLater of [false, true]) {
        const below = vi.fn()
        const owner = h(Owner, { id: `below-${layer}`, layer, onEscape: below })
        render(h(Shell, { initial: belowLater ? 'result' : 'none' },
          belowLater ? h(Late, { label: 'open below' }, owner) : owner))
        fireEvent.click(screen.getByRole('button', { name: belowLater ? 'open below' : 'result' }))
        expect(escapeOwnerStack()).toEqual([PHONE_DRAWER_ESCAPE_ID, `below-${layer}`])
        press()
        expect(drawer()).toBe('none')
        expect(below).not.toHaveBeenCalled()
        press()
        expect(below).toHaveBeenCalledTimes(1)
        cleanup()
      }
    }
  })
})

describe('KEYS-b the phone drawer yields upward and to its own kind', () => {
  it('KEYS-B07 a menu or a sheet closes before the drawer, then the drawer closes', () => {
    for (const layer of ['menu', 'sheet']) {
      expect(ESCAPE_LAYERS[layer]).toBeGreaterThan(ESCAPE_LAYERS.drawer)
      function Above() {
        const [open, setOpen] = useState(true)
        // ProjectSwitcher / ShortcutSheet: a global owner with its root as scope.
        const scope = useRef(null)
        useEscapeOwner(`above-${layer}`, open, () => setOpen(false), { layer, scope })
        return h('div', { ref: scope, 'data-testid': 'above', 'data-open': String(open) })
      }
      render(h(Shell, { initial: 'plan' }, h(Above)))
      press(screen.getByRole('button', { name: 'plan' }))
      expect(screen.getByTestId('above').getAttribute('data-open')).toBe('false')
      expect(drawer()).toBe('plan')
      press(screen.getByRole('button', { name: 'plan' }))
      expect(drawer()).toBe('none')
      cleanup()
    }
  })

  it('KEYS-B08 a drawer-layer overlay opened later closes first, then the phone drawer', () => {
    const closeDetails = vi.fn()
    function Details() {
      const [open, setOpen] = useState(false)
      const scope = useRef(null)
      // DetailsDrawer: useEscapeOwner('details', open, ..., { layer: 'drawer', scope: drawerRef }).
      useEscapeOwner('details', open, () => { closeDetails(); setOpen(false) }, { layer: 'drawer', scope })
      return h('div', { ref: scope }, h('button', { type: 'button', onClick: () => setOpen(true) }, 'open details'))
    }
    render(h(Shell, { initial: 'nav' }, h(Details)))
    fireEvent.click(screen.getByRole('button', { name: 'open details' }))
    expect(escapeOwnerStack()).toEqual(['details', PHONE_DRAWER_ESCAPE_ID])
    press()
    expect(closeDetails).toHaveBeenCalledTimes(1)
    expect(drawer()).toBe('nav')
    press()
    expect(drawer()).toBe('none')
    expect(closeDetails).toHaveBeenCalledTimes(1)
  })

  it('KEYS-B09 closes the drawer that is open now, read through the newest handler', () => {
    const closed = []
    render(h(Shell, { initial: 'nav', onClosed: (name) => closed.push(name) }))
    // Switching drawers re-renders the shell; the owner stays one record.
    fireEvent.click(screen.getByRole('button', { name: 'jobs' }))
    fireEvent.click(screen.getByRole('button', { name: 'plan' }))
    expect(escapeOwnerStack()).toEqual([PHONE_DRAWER_ESCAPE_ID])
    press()
    expect(closed).toEqual(['plan'])
    expect(drawer()).toBe('none')
    // Nothing is open: the key is left alone and nothing runs.
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    act(() => { window.dispatchEvent(event) })
    expect(event.defaultPrevented).toBe(false)
    expect(closed).toEqual(['plan'])
  })
})

describe('KEYS-b the stack and the shell ladder close the drawer once', () => {
  // App mounts the ladder on window (bubble) with the shell state; the stack's
  // capture listener runs first.
  function withLadder(shell, run) {
    const onCloseDrawer = vi.fn()
    const onCloseHistory = vi.fn()
    const onInterruptRun = vi.fn()
    const ladder = ladderListener(shell, (state) => ({ ...state, onCloseDrawer, onCloseHistory, onInterruptRun }))
    window.addEventListener('keydown', ladder)
    try {
      run({ onCloseDrawer, onCloseHistory, onInterruptRun })
    } finally {
      window.removeEventListener('keydown', ladder)
    }
  }

  it('KEYS-B10 the stack consumes the key, so the ladder runs no rung for the same press', () => {
    const closed = []
    render(h(Shell, { initial: 'nav', onClosed: (name) => closed.push(name) }))
    withLadder({ phoneViewport: true, studioDrawer: 'nav', historyOpen: true, running: true }, (spies) => {
      const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
      act(() => { screen.getByRole('button', { name: 'nav' }).dispatchEvent(event) })
      expect(event.defaultPrevented).toBe(true)
      expect(closed).toEqual(['nav'])
      expect(spies.onCloseDrawer).not.toHaveBeenCalled()
      expect(spies.onCloseHistory).not.toHaveBeenCalled()
      expect(spies.onInterruptRun).not.toHaveBeenCalled()
    })
  })

  it('KEYS-B11 when the stack yields to a foreign layer, the ladder drawer rung still closes the drawer', () => {
    const closed = []
    render(h(Shell, { initial: 'jobs', onClosed: (name) => closed.push(name) }))
    // A layer the stack does not own (the object snap menu's marker).
    const foreign = document.createElement('div')
    foreign.setAttribute('data-escape-owner', 'osnap-menu')
    document.body.appendChild(foreign)
    expect(topEscapeOwnerId()).toBe('')
    withLadder({ phoneViewport: true, studioDrawer: 'jobs' }, (spies) => {
      press()
      // The owner did not run; the shell's own rung did.
      expect(closed).toEqual([])
      expect(spies.onCloseDrawer).toHaveBeenCalledTimes(1)
    })
    foreign.remove()
    expect(topEscapeOwnerId()).toBe(PHONE_DRAWER_ESCAPE_ID)
  })
})
