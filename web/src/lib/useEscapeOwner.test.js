// @vitest-environment jsdom
// S27: the one Escape owner stack (motion standard section 7). Three claims:
// STACK ORDER (the semantic layer first, then the newest activation), NESTING
// (an owner inside another owner's scope closes first, whatever order their
// effects ran in), and UNMOUNT (a closed or unmounted owner leaves the stack
// and the window listener goes with the last one).
import { createElement, useLayoutEffect, useRef, useState } from 'react'
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import useEscapeOwner, {
  ESCAPE_LAYERS,
  closeTopEscapeOwner,
  escapeOwnerStack,
  registerEscapeOwner,
  topEscapeOwnerId,
} from './useEscapeOwner.js'

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

const h = createElement

function Owner({ id, open = true, layer, onEscape, children = null, when, scoped = false }) {
  const scope = useRef(null)
  useEscapeOwner(id, open, onEscape || (() => {}), { layer, scope, when, scoped })
  return h('div', { ref: scope, 'data-testid': id }, children)
}

const press = (target = window) => fireEvent.keyDown(target, { key: 'Escape' })

describe('registerEscapeOwner', () => {
  it('registers an active owner and unregisters harmlessly twice', () => {
    const fn = vi.fn()
    const unregister = registerEscapeOwner('t', fn, { layer: 'menu' })
    try {
      expect(escapeOwnerStack()).toEqual(['t'])
      expect(topEscapeOwnerId()).toBe('t')
      expect(closeTopEscapeOwner()).toBe('t')
      expect(fn).toHaveBeenCalledTimes(1)
    } finally {
      unregister()
    }
    expect(escapeOwnerStack()).toEqual([])
    unregister()
    expect(escapeOwnerStack()).toEqual([])
    expect(topEscapeOwnerId()).toBe('')
  })

  it('rejects an unknown layer without registering an owner', () => {
    expect(() => registerEscapeOwner('t', vi.fn(), { layer: 'unknown' })).toThrow(/unknown layer/)
    expect(escapeOwnerStack()).toEqual([])
  })
})

describe('useEscapeOwner stack order', () => {
  it('orders the section 7 layers topmost first', () => {
    const order = Object.entries(ESCAPE_LAYERS).sort((a, b) => b[1] - a[1]).map(([name]) => name)
    expect(order).toEqual(['menu', 'sheet', 'drawer', 'history', 'edit', 'command', 'proposal', 'run', 'focus', 'scene'])
  })

  it('closes the higher layer first, whatever order the owners opened in', () => {
    const seen = []
    const { rerender } = render(h('div', null,
      h(Owner, { id: 'sheet', layer: 'sheet', open: false, onEscape: () => seen.push('sheet') }),
      h(Owner, { id: 'drawer', layer: 'drawer', onEscape: () => seen.push('drawer') }),
    ))
    expect(escapeOwnerStack()).toEqual(['drawer'])
    rerender(h('div', null,
      h(Owner, { id: 'sheet', layer: 'sheet', open: true, onEscape: () => seen.push('sheet') }),
      h(Owner, { id: 'drawer', layer: 'drawer', onEscape: () => seen.push('drawer') }),
    ))
    expect(escapeOwnerStack()).toEqual(['sheet', 'drawer'])
    press()
    expect(seen).toEqual(['sheet'])
  })

  it('within one layer, the newest activation is on top', () => {
    const seen = []
    const tree = (aOpen, bOpen) => h('div', null,
      h(Owner, { id: 'a', layer: 'drawer', open: aOpen, onEscape: () => seen.push('a') }),
      h(Owner, { id: 'b', layer: 'drawer', open: bOpen, onEscape: () => seen.push('b') }),
    )
    const { rerender } = render(tree(false, true))
    rerender(tree(true, true))
    expect(topEscapeOwnerId()).toBe('a')
    press()
    expect(seen).toEqual(['a'])
  })

  it('consumes the key so no window listener below it reads the same press', () => {
    const below = vi.fn()
    window.addEventListener('keydown', below)
    try {
      render(h(Owner, { id: 'only', layer: 'menu', onEscape: () => {} }))
      const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
      document.body.dispatchEvent(event)
      expect(event.defaultPrevented).toBe(true)
      expect(below).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', below)
    }
  })

  it('a declining owner hands the key to the next one down', () => {
    const seen = []
    render(h('div', null,
      h(Owner, { id: 'menu', layer: 'menu', when: () => false, onEscape: () => seen.push('menu') }),
      h(Owner, { id: 'scene', layer: 'scene', onEscape: () => seen.push('scene') }),
    ))
    press()
    expect(seen).toEqual(['scene'])
  })

  it('a scoped owner claims only a key from inside its scope', () => {
    const seen = []
    render(h('div', null,
      h(Owner, { id: 'form', layer: 'edit', scoped: true, onEscape: () => seen.push('form') },
        h('input', { 'data-testid': 'inside' })),
      h('input', { 'data-testid': 'outside' }),
      h(Owner, { id: 'board', layer: 'focus', onEscape: () => seen.push('board') }),
    ))
    press(document.querySelector('[data-testid="outside"]'))
    press(document.querySelector('[data-testid="inside"]'))
    expect(seen).toEqual(['board', 'form'])
  })

  it('leaves Escape alone while nothing is open', () => {
    const below = vi.fn()
    window.addEventListener('keydown', below)
    try {
      render(h(Owner, { id: 'closed', open: false }))
      press()
      expect(below).toHaveBeenCalledTimes(1)
      expect(below.mock.calls[0][0].defaultPrevented).toBe(false)
      expect(topEscapeOwnerId()).toBe('')
      expect(closeTopEscapeOwner()).toBe('')
    } finally {
      window.removeEventListener('keydown', below)
    }
  })

  it('falls through to the global ladder when every mounted owner declines', () => {
    const below = vi.fn()
    const close = vi.fn()
    window.addEventListener('keydown', below)
    try {
      render(h('div', null,
        h(Owner, { id: 'menu', layer: 'menu', when: () => false, onEscape: close }),
        h(Owner, { id: 'scene', layer: 'scene', when: () => false, onEscape: close }),
      ))
      const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
      document.body.dispatchEvent(event)
      expect(event.defaultPrevented).toBe(false)
      expect(below).toHaveBeenCalledTimes(1)
      expect(close).not.toHaveBeenCalled()
      expect(topEscapeOwnerId(event)).toBe('')
    } finally {
      window.removeEventListener('keydown', below)
    }
  })

  it('returns to the scene rung on the next press after dismissing a proposal', () => {
    const exit = vi.fn()
    function Scene() {
      const [proposalOpen, setProposalOpen] = useState(true)
      return h('div', null,
        h('div', { className: 'strip-decision', role: 'status' }, 'Passive status'),
        h(Owner, { id: 'proposal', layer: 'proposal', open: proposalOpen, onEscape: () => setProposalOpen(false) }),
        h(Owner, { id: 'scene', layer: 'scene', onEscape: exit }),
      )
    }
    render(h(Scene))
    press(document.body)
    expect(exit).not.toHaveBeenCalled()
    expect(topEscapeOwnerId()).toBe('scene')
    press(document.body)
    expect(exit).toHaveBeenCalledTimes(1)
  })

  it('removes a dismissed proposal during the closing commit, before passive effects', () => {
    const exit = vi.fn()
    const observed = []
    function AfterOwners({ proposalOpen }) {
      useLayoutEffect(() => {
        if (!proposalOpen) {
          observed.push(topEscapeOwnerId())
          closeTopEscapeOwner()
        }
      }, [proposalOpen])
      return null
    }
    function Scene() {
      const [proposalOpen, setProposalOpen] = useState(true)
      return h('div', null,
        h(Owner, { id: 'proposal', layer: 'proposal', open: proposalOpen, onEscape: () => setProposalOpen(false) }),
        h(Owner, { id: 'scene', layer: 'scene', onEscape: exit }),
        h(AfterOwners, { proposalOpen }),
      )
    }
    render(h(Scene))
    press(document.body)
    expect(observed).toEqual(['scene'])
    expect(exit).toHaveBeenCalledTimes(1)
    expect(escapeOwnerStack()).toEqual(['scene'])
  })

  it('yields to a visible foreign [data-escape-owner] layer it does not hold', () => {
    const seen = []
    render(h(Owner, { id: 'drawer', layer: 'drawer', onEscape: () => seen.push('drawer') }))
    const foreign = document.createElement('div')
    foreign.setAttribute('data-escape-owner', 'foreign-menu')
    document.body.appendChild(foreign)
    press()
    expect(seen).toEqual([])
    foreign.remove()
    press()
    expect(seen).toEqual(['drawer'])
  })
})

describe('useEscapeOwner nesting', () => {
  it('closes the inner owner before its host even though the inner effect ran first', () => {
    const seen = []
    render(h(Owner, { id: 'host', layer: 'drawer', onEscape: () => seen.push('host') },
      h(Owner, { id: 'inner', layer: 'drawer', onEscape: () => seen.push('inner') })))
    expect(escapeOwnerStack()).toEqual(['inner', 'host'])
    press()
    expect(seen).toEqual(['inner'])
  })

  it('pops one rung per press: inner, then host', () => {
    const seen = []
    function Host() {
      const [innerOpen, setInnerOpen] = useState(true)
      const [hostOpen, setHostOpen] = useState(true)
      return h(Owner, { id: 'host', layer: 'drawer', open: hostOpen, onEscape: () => { seen.push('host'); setHostOpen(false) } },
        h(Owner, { id: 'inner', layer: 'drawer', open: innerOpen, onEscape: () => { seen.push('inner'); setInnerOpen(false) } }))
    }
    render(h(Host))
    act(() => { press() })
    act(() => { press() })
    act(() => { press() })
    expect(seen).toEqual(['inner', 'host'])
    expect(escapeOwnerStack()).toEqual([])
  })
})

describe('useEscapeOwner unmount', () => {
  it('drops an unmounted owner and detaches the listener with the last one', () => {
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    try {
      const seen = []
      const { rerender, unmount } = render(h('div', null,
        h(Owner, { id: 'a', layer: 'drawer', onEscape: () => seen.push('a') }),
        h(Owner, { id: 'b', layer: 'sheet', onEscape: () => seen.push('b') }),
      ))
      const installs = add.mock.calls.filter(([type, , capture]) => type === 'keydown' && capture === true)
      expect(installs).toHaveLength(1)
      rerender(h('div', null, h(Owner, { id: 'a', layer: 'drawer', onEscape: () => seen.push('a') })))
      expect(escapeOwnerStack()).toEqual(['a'])
      press()
      expect(seen).toEqual(['a'])
      unmount()
      expect(escapeOwnerStack()).toEqual([])
      const removals = remove.mock.calls.filter(([type, , capture]) => type === 'keydown' && capture === true)
      expect(removals).toHaveLength(1)
      press()
      expect(seen).toEqual(['a'])
    } finally {
      add.mockRestore()
      remove.mockRestore()
    }
  })

  it('reads the newest handler without re-ordering the stack', () => {
    const first = vi.fn()
    const second = vi.fn()
    const { rerender } = render(h('div', null,
      h(Owner, { id: 'a', layer: 'drawer', onEscape: first }),
      h(Owner, { id: 'b', layer: 'drawer', onEscape: () => {} }),
    ))
    rerender(h('div', null,
      h(Owner, { id: 'a', layer: 'drawer', onEscape: second }),
      h(Owner, { id: 'b', layer: 'drawer', onEscape: () => {} }),
    ))
    // A new handler is not a new activation: `a` keeps its place under `b`.
    expect(escapeOwnerStack()).toEqual(['b', 'a'])
    rerender(h('div', null,
      h(Owner, { id: 'a', layer: 'drawer', onEscape: second }),
      h(Owner, { id: 'b', layer: 'drawer', open: false, onEscape: () => {} }),
    ))
    expect(closeTopEscapeOwner()).toBe('a')
    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledTimes(1)
  })
})
