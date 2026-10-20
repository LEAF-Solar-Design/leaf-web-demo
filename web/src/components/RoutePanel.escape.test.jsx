// @vitest-environment jsdom

import { useEffect, useRef, useState } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import RoutePanel from './RoutePanel.jsx'
import { ladderListener } from '../lib/actionRegistry.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { EXIT_MS } from '../useExit.js'

const tools = [{
  name: 'count-by-layer',
  capabilities: ['drawing.read', 'solve'],
  params: { properties: {} },
}]
const runRoute = {
  lane: 'run',
  tool: 'count-by-layer',
  confidence: 0.99,
  runIntent: { tool: 'count-by-layer', params: {} },
}

// Stateful caller: dismissal clears the live route, while the real useExit
// keeps the display-only surface mounted for its normal exit fade.
function Shell({ initialRoute, callbacks, children }) {
  const [route, setRoute] = useState(initialRoute)
  const dismiss = () => { callbacks.onDismiss(); setRoute(null) }
  useEffect(() => {
    const listener = ladderListener({ route, openProjectId: 'project-1' }, (state) => ({
      ...state,
      onDismissRoute: dismiss,
      onCloseProject: callbacks.onCloseProject,
    }))
    window.addEventListener('keydown', listener)
    return () => window.removeEventListener('keydown', listener)
  }, [route, callbacks])
  return <>
    <div className="bar-dock">
      <input className="bar-field" aria-label="Request" defaultValue="Count by layer" />
      <RoutePanel route={route} tools={tools} running={false} writeLocked={false}
        onDismiss={dismiss} onConfirmIntent={callbacks.onConfirmIntent}
        onPickAlternative={callbacks.onPickAlternative} onOpenAuthor={callbacks.onOpenAuthor} />
    </div>
    <button type="button">Outside control</button>
    {children}
  </>
}

// The same global owner + surface scope pattern used by menus and sheets.
function Overlay({ layer, onClose }) {
  const [open, setOpen] = useState(true)
  const scope = useRef(null)
  useEscapeOwner(`test-${layer}`, open, () => { onClose(); setOpen(false) }, { layer, scope })
  if (!open) return null
  return <div ref={scope} role={layer === 'menu' ? 'menu' : 'dialog'} aria-label="Overlay">
    <button type="button">Overlay control</button>
  </div>
}

function SceneOwner({ onEscape }) {
  useEscapeOwner('test-scene', true, onEscape, { layer: 'scene' })
  return null
}

function mount(initialRoute = runRoute, children = null) {
  const callbacks = {
    onDismiss: vi.fn(),
    onConfirmIntent: vi.fn(),
    onPickAlternative: vi.fn(),
    onOpenAuthor: vi.fn(),
    onCloseProject: vi.fn(),
  }
  const view = render(<Shell initialRoute={initialRoute} callbacks={callbacks}>{children}</Shell>)
  return { ...view, callbacks }
}

const proposal = (container) => container.querySelector('.strip-decision, .resolver')
const pressEscape = (target = document.activeElement) => fireEvent.keyDown(target, { key: 'Escape' })
const finishExit = () => act(() => { vi.advanceTimersByTime(EXIT_MS) })
function expectNoRun(callbacks) {
  expect(callbacks.onConfirmIntent).not.toHaveBeenCalled()
  expect(callbacks.onPickAlternative).not.toHaveBeenCalled()
  expect(callbacks.onOpenAuthor).not.toHaveBeenCalled()
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('RoutePanel Escape dismissal', () => {
  it.each([
    ['confident run', runRoute, 'button', 'Run count-by-layer'],
    ['resolver', { ...runRoute, confidence: 0.6 }, 'option', /count-by-layer/],
    ['build', { lane: 'build' }, 'button', 'Open author flow'],
    ['unbound solve', { lane: 'solve', alternatives: [{ tool: 'count-by-layer' }] }, 'button', 'Pick solver count-by-layer'],
  ])('dismisses a %s without running and returns focus from the proposal', (_name, route, role, name) => {
    const { container, callbacks } = mount(route)
    const control = screen.getByRole(role, { name })
    control.focus()
    expect(proposal(container)).not.toBeNull()

    expect(pressEscape(control)).toBe(false)

    expect(callbacks.onDismiss).toHaveBeenCalledOnce()
    expectNoRun(callbacks)
    expect(callbacks.onCloseProject).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Request' }))
    expect(proposal(container)).toHaveClass('exit')
    finishExit()
    expect(proposal(container)).toBeNull()
    expectNoRun(callbacks)
  })

  it.each(['Request', 'Outside control'])('preserves focus on %s outside the proposal', (name) => {
    const { container, callbacks } = mount()
    const control = screen.getByRole(name === 'Request' ? 'textbox' : 'button', { name })
    control.focus()

    pressEscape(control)
    finishExit()

    expect(proposal(container)).toBeNull()
    expect(callbacks.onDismiss).toHaveBeenCalledOnce()
    expect(document.activeElement).toBe(control)
    expectNoRun(callbacks)
  })

  it('lets the next Escape reach a lower owner during the display-only exit fade', () => {
    const closeScene = vi.fn()
    const { container, callbacks } = mount(runRoute, <SceneOwner onEscape={closeScene} />)
    screen.getByRole('textbox', { name: 'Request' }).focus()
    pressEscape()
    expect(proposal(container)).toHaveClass('exit')
    expect(closeScene).not.toHaveBeenCalled()

    pressEscape()

    expect(callbacks.onDismiss).toHaveBeenCalledOnce()
    expect(closeScene).toHaveBeenCalledOnce()
    expect(callbacks.onCloseProject).not.toHaveBeenCalled()
    expectNoRun(callbacks)
    finishExit()
    expect(proposal(container)).toBeNull()
  })

  it('keeps the existing shell Escape rung when no proposal is mounted', () => {
    const { container, callbacks } = mount(null)
    expect(proposal(container)).toBeNull()

    pressEscape(document.body)

    expect(callbacks.onCloseProject).toHaveBeenCalledOnce()
    expect(callbacks.onDismiss).not.toHaveBeenCalled()
    expectNoRun(callbacks)
  })

  it.each(['menu', 'sheet'])('closes a higher %s owner before dismissing the proposal', (layer) => {
    const closeOverlay = vi.fn()
    const { container, callbacks } = mount(runRoute, <Overlay layer={layer} onClose={closeOverlay} />)
    const request = screen.getByRole('textbox', { name: 'Request' })
    request.focus()

    pressEscape()

    expect(closeOverlay).toHaveBeenCalledOnce()
    expect(screen.queryByRole(layer === 'menu' ? 'menu' : 'dialog')).toBeNull()
    expect(proposal(container)).toHaveClass('enter')
    expect(callbacks.onDismiss).not.toHaveBeenCalled()
    expect(callbacks.onCloseProject).not.toHaveBeenCalled()
    expectNoRun(callbacks)

    pressEscape()
    finishExit()

    expect(proposal(container)).toBeNull()
    expect(callbacks.onDismiss).toHaveBeenCalledOnce()
    expect(closeOverlay).toHaveBeenCalledOnce()
    expect(callbacks.onCloseProject).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(request)
    expectNoRun(callbacks)
  })
})
