import React from 'react'
import { act, cleanup, fireEvent, render, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import CanvasToolbar from './CanvasToolbar.jsx'

const css = readFileSync(resolve(process.cwd(), 'src/site/browserAssetGround.css'), 'utf8')
let media, mediaChange, observers, height
beforeEach(() => {
  height = 101
  observers = []
  media = { matches: true, addEventListener: vi.fn((_event, callback) => { mediaChange = callback }), removeEventListener: vi.fn() }
  vi.stubGlobal('matchMedia', vi.fn(() => media))
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback) { this.callback = callback; this.observe = vi.fn(); this.disconnect = vi.fn(); observers.push(this) }
  })
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({ height }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

function setup(overrides = {}) {
  const session = Object.fromEntries(['undo', 'redo', 'save', 'discard', 'reload'].map(name => [name, vi.fn()]))
  const state = { enabled: true, busy: false, dirty: true, canUndo: true, canRedo: true, savedRevision: 7,
    validation: '', conflict: false, error: '', notice: '', status: 'Unsaved changes', ...overrides }
  const callbacks = { onEditing: vi.fn(), onOutline: vi.fn(), onSettings: vi.fn() }
  let props = { editor: { session, state }, editing: false, outline: false, settings: false, ...callbacks }
  const tree = () => <div className="browser-asset-viewport"><CanvasToolbar {...props} /><input aria-label="Inline field" /></div>
  const view = render(tree())
  const surface = view.container.firstElementChild
  const toolbar = surface.querySelector('.canvas-toolbar')
  const tools = within(toolbar)
  const button = name => tools.getByRole('button', { name, exact: true })
  const update = (changes = {}, other = {}) => {
    props = { ...props, ...other, editor: { session, state: { ...props.editor.state, ...changes } } }
    view.rerender(tree())
  }
  return { ...view, surface, toolbar, tools, button, session, callbacks, update }
}

describe('connected compact page toolbar', () => {
  it('keeps primary controls available and discloses the actual secondary handlers without saving', () => {
    const { button, tools, session, callbacks } = setup()
    expect(tools.getAllByRole('button').map(node => node.getAttribute('aria-label') || node.textContent))
      .toEqual(['View', 'Edit', 'Save page', 'More page tools', 'Page status'])
    fireEvent.click(button('Edit')); fireEvent.click(button('View'))
    expect(callbacks.onEditing.mock.calls).toEqual([[true], [false]])
    const more = button('More page tools')
    expect(more).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(more)
    expect(more).toHaveAttribute('aria-expanded', 'true')
    expect(document.getElementById(more.getAttribute('aria-controls'))).not.toHaveAttribute('hidden')
    for (const [label, handler] of [['Undo', 'undo'], ['Redo', 'redo'], ['Reload saved', 'reload']]) {
      fireEvent.click(button(label))
      expect(session[handler]).toHaveBeenCalledTimes(1)
    }
    fireEvent.click(button('Elements / Outline')); fireEvent.click(button('Settings'))
    expect(callbacks.onOutline).toHaveBeenCalledWith(true)
    expect(callbacks.onSettings).toHaveBeenCalledWith(true)
    fireEvent.click(more)
    expect(tools.queryByRole('button', { name: 'Undo', exact: true })).toBeNull()
    expect(session.save).not.toHaveBeenCalled()
    expect(session.discard).not.toHaveBeenCalled()
    fireEvent.click(button('Save page'))
    expect(session.save).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['locked', { enabled: false }, ['Undo', 'Redo', 'Save page', 'Discard unsaved', 'Reload saved']],
    ['busy', { busy: 'saving' }, ['Undo', 'Redo', 'Save page', 'Discard unsaved', 'Reload saved']],
    ['clean', { dirty: false }, ['Save page', 'Discard unsaved']],
    ['validation', { validation: 'Headline is required' }, ['Save page']],
    ['conflict', { conflict: true, error: 'Saved page changed. Reload saved before saving.' }, ['Save page']],
    ['no history', { canUndo: false, canRedo: false }, ['Undo', 'Redo']],
    ['no revision', { savedRevision: null }, ['Discard unsaved']],
  ])('retains disabled conditions for %s', (_label, state, disabled) => {
    const { button, session } = setup(state)
    fireEvent.click(button('More page tools'))
    for (const name of ['Undo', 'Redo', 'Save page', 'Discard unsaved', 'Reload saved']) {
      expect(button(name).disabled).toBe(disabled.includes(name))
      if (disabled.includes(name)) fireEvent.click(button(name))
    }
    for (const callback of Object.values(session)) expect(callback).not.toHaveBeenCalled()
    expect(button('View')).toBeEnabled()
    expect(button('Edit')).toBeEnabled()
    expect(button('Settings')).toBeEnabled()
  })

  it('keeps the five-second, two-step discard confirmation', () => {
    const clock = vi.spyOn(Date, 'now').mockReturnValue(10000)
    const { button, session } = setup()
    fireEvent.click(button('More page tools'))
    fireEvent.click(button('Discard unsaved'))
    expect(session.discard).not.toHaveBeenCalled()
    clock.mockReturnValue(16000)
    fireEvent.click(button('Confirm discard unsaved'))
    expect(session.discard).not.toHaveBeenCalled()
    fireEvent.click(button('Confirm discard unsaved'))
    expect(session.discard).toHaveBeenCalledTimes(1)
    expect(button('Discard unsaved')).toBeEnabled()
  })

  it.each(['status', 'notice', 'validation', 'error'])('exposes the full %s through a named control and updates it while open', field => {
    const message = 'Full page message: ' + 'A long explanation with recovery instructions. '.repeat(12)
    const { button, tools, session, update } = setup({ [field]: message })
    expect(tools.getByRole('status').textContent).toBe(message)
    fireEvent.click(button('Page status'))
    expect(button('Page status')).toHaveAttribute('aria-expanded', 'true')
    const region = tools.getByRole('region', { name: 'Full page status' })
    expect(region.querySelector('p').textContent).toBe(message)
    expect(region.id).toBe(button('Page status').getAttribute('aria-controls'))
    update({ [field]: message + ' Updated.' })
    expect(region.querySelector('p').textContent).toBe(message + ' Updated.')
    button('Close page status').focus()
    fireEvent.click(button('Close page status'))
    expect(button('Page status')).toHaveFocus()
    expect(tools.queryByRole('region', { name: 'Full page status' })).toBeNull()
    for (const callback of Object.values(session)) expect(callback).not.toHaveBeenCalled()
  })

  it('keeps expanded native buttons focusable and returns focus when disclosures close', () => {
    const { button, tools, update } = setup()
    fireEvent.click(button('More page tools'))
    for (const name of ['Undo', 'Redo', 'Discard unsaved', 'Reload saved', 'Elements / Outline', 'Settings']) {
      const control = button(name)
      expect(control.tagName).toBe('BUTTON')
      expect(control).toHaveAttribute('type', 'button')
      expect(control.tabIndex).toBe(0)
      control.focus()
      expect(control).toHaveFocus()
      expect(fireEvent.keyDown(control, { key: 'Tab' })).toBe(true)
    }
    // Native detail-zero activation also takes the real button handler.
    act(() => button('Settings').click())
    fireEvent.keyDown(button('Settings'), { key: 'Escape' })
    expect(button('More page tools')).toHaveFocus()
    expect(button('More page tools')).toHaveAttribute('aria-expanded', 'false')
    expect(tools.queryByRole('button', { name: 'Settings' })).toBeNull()
    fireEvent.click(button('Page status'))
    button('Close page status').focus()
    fireEvent.keyDown(button('Close page status'), { key: 'Escape' })
    expect(button('Page status')).toHaveFocus()
    update({}, { editing: true, outline: true, settings: true })
    expect(button('Edit')).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(button('More page tools'))
    expect(button('Elements / Outline')).toHaveAttribute('aria-expanded', 'true')
    expect(button('Settings')).toHaveAttribute('aria-expanded', 'true')
  })

  it.each(['ctrlKey', 'metaKey'])('retains %s save, undo and redo even with secondary controls collapsed', modifier => {
    const { button, surface, session, update } = setup()
    const key = (value, extra = {}, target = button('Edit')) => fireEvent.keyDown(target, { key: value, [modifier]: true, ...extra })
    expect(key('s')).toBe(false)
    expect(session.save).toHaveBeenCalledTimes(1)
    key('z'); key('Z', { shiftKey: true })
    expect(session.undo).toHaveBeenCalledTimes(1)
    expect(session.redo).toHaveBeenCalledTimes(1)
    key('s', {}, surface.querySelector('input')); key('s', { altKey: true })
    for (const changes of [{ dirty: false }, { dirty: true, validation: 'Invalid page' }, { validation: '', conflict: true },
      { conflict: false, busy: 'saving' }, { busy: false, enabled: false }]) {
      update(changes); key('s')
    }
    key('z'); key('z', { shiftKey: true })
    expect(session.save).toHaveBeenCalledTimes(1)
    expect(session.undo).toHaveBeenCalledTimes(1)
    expect(session.redo).toHaveBeenCalledTimes(1)
    update({ enabled: true })
    const editButton = button('Edit')
    surface.hidden = true
    key('s', {}, editButton); key('z', {}, editButton)
    surface.hidden = false
    surface.setAttribute('inert', '')
    key('s', {}, editButton); key('z', {}, editButton)
    expect(session.save).toHaveBeenCalledTimes(1)
    expect(session.undo).toHaveBeenCalledTimes(1)
  })

  it('preserves desktop actions and moves focus out of controls hidden by a viewport change', () => {
    media.matches = false
    const { button, tools } = setup()
    expect(tools.queryByRole('button', { name: 'More page tools' })).toBeNull()
    expect(tools.queryByRole('button', { name: 'Page status' })).toBeNull()
    expect(tools.getAllByRole('button')).toHaveLength(9)
    button('Undo').focus()
    act(() => { media.matches = true; mediaChange() })
    expect(button('More page tools')).toHaveFocus()
    expect(tools.queryByRole('button', { name: 'Undo', exact: true })).toBeNull()
    act(() => { media.matches = false; mediaChange() })
    expect(button('View')).toHaveFocus()
    expect(button('Undo')).toBeEnabled()
  })

  it('keeps the measured scroll-margin contract after disclosure, message, observer and viewport changes', () => {
    const { button, surface, toolbar, update, unmount } = setup()
    const measured = () => surface.style.getPropertyValue('--canvas-toolbar-height')
    expect(measured()).toBe('101px')
    expect(observers[0].observe).toHaveBeenCalledWith(toolbar)
    height = 201
    fireEvent.click(button('More page tools'))
    expect(measured()).toBe('201px')
    height = 301
    fireEvent.click(button('Page status'))
    expect(measured()).toBe('301px')
    height = 345
    update({ error: 'A longer error message' })
    expect(measured()).toBe('345px')
    height = 400
    act(() => observers[0].callback())
    expect(measured()).toBe('400px')
    height = 101
    fireEvent(window, new Event('resize'))
    expect(measured()).toBe('101px')
    unmount()
    expect(observers[0].disconnect).toHaveBeenCalledTimes(1)
    expect(media.removeEventListener).toHaveBeenCalledWith('change', mediaChange)
  })

  it('provides scoped touch targets, compact rows, wrapping full status and visible keyboard focus', () => {
    // Source guards only; rendered 390 x 844 fit still requires staging replay.
    expect(css).toMatch(/\.canvas-toolbar button[^}]*min-height: 44px/)
    expect(css).toMatch(/\.canvas-toolbar button\s*\{[^}]*min-width: 44px/)
    expect(css).toMatch(/\.canvas-toolbar button:focus-visible\s*\{[^}]*outline:/)
    expect(css).toMatch(/\.canvas-toolbar \[hidden\]\s*\{[^}]*display: none !important/)
    expect(css).toMatch(/@media\(max-width:899px\)\s*\{\s*\.canvas-toolbar\s*\{[^}]*gap: 4px; padding: 4px/)
    expect(css).toMatch(/\.canvas-toolbar__status > span\s*\{[^}]*text-overflow: ellipsis/)
    expect(css).toMatch(/\.canvas-toolbar__status-details\s*\{[^}]*overflow-wrap: anywhere/)
    expect(css).toMatch(/scroll-margin-top: calc\(var\(--canvas-toolbar-height, 160px\) \+ 12px\)/)
  })
})
