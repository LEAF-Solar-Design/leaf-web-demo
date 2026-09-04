// @vitest-environment jsdom
//
// Slice 10b/10c: the act-scope palette and the find-scope search resolver.
// No new modal — Ctrl/Cmd+K still just focuses the ONE bar (untouched here);
// these tests cover what picking the existing find/act scope chip rows now
// resolves to.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PromptBox from './PromptBox.jsx'

const noop = () => {}

function jsonResponse(body, ok = true, status = 200) {
  return Promise.resolve({ ok, status, json: () => Promise.resolve(body), clone: function () { return this } })
}

function mount(props = {}) {
  return render(
    <div data-testid="host">
      <PromptBox value={props.value ?? ''} onChange={props.onChange ?? noop} onDispatch={props.onDispatch ?? noop}
        projectName="cat-panels" {...props} />
    </div>,
  )
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn((url) => {
    const u = String(url)
    if (u.includes('/api/drawings/')) {
      return jsonResponse({ drawing_id: 'demo', head: 2, latest: 2, versions: [
        { v: 1, parent: null, tool: 'drawing.ingest', note: 'first' },
        { v: 2, parent: 1, tool: 'drawing.write', note: 'panel move' },
      ] })
    }
    if (u.includes('/api/operator/sessions')) {
      return jsonResponse({ sessions: [{ session_id: 'opsess-1', profile: 'default', environment: 'staging', status: 'idle' }] })
    }
    if (u.includes('/api/search')) {
      return jsonResponse({ query: 'panel', results: [{ kind: 'tool', id: 'tool:panel-cut', label: 'panel-cut', description: 'cuts panels' }] })
    }
    return jsonResponse(null, false, 404)
  }))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const ACTIONS = [
  { id: 'fit', label: 'fit', icon: 'fit', kbd: null, disabled: false, reason: '', onSelect: vi.fn() },
  { id: 'undo', label: 'undo', icon: 'undo', kbd: null, disabled: true, reason: 'nothing to undo', onSelect: vi.fn() },
]

function openScope(container, label) {
  fireEvent.click(container.querySelector('.bar-scope'))
  fireEvent.click(screen.getByText(label))
}

describe('act scope: the resolver lists registry actions with honest reasons', () => {
  it('a click on the "act" row opens the palette, filters as you type, and a disabled row shows its reason', async () => {
    const onChange = vi.fn()
    const { container } = mount({ paletteActions: ACTIONS, onChange })
    openScope(container, 'act')
    expect(container.querySelector('.act-palette')).toBeTruthy()
    expect(screen.getByText('fit')).toBeTruthy()
    expect(screen.getByText('undo')).toBeTruthy()
    expect(screen.getByText(/unavailable: nothing to undo/)).toBeTruthy()
  })

  it('arrow down then Enter runs the highlighted action and clears the well', async () => {
    const onChange = vi.fn()
    const { container, rerender } = mount({ paletteActions: ACTIONS, onChange })
    openScope(container, 'act')
    const input = container.querySelector('.bar-field')
    fireEvent.change(input, { target: { value: 'fit' } })
    rerender(<div data-testid="host"><PromptBox value="fit" onChange={onChange} onDispatch={noop} paletteActions={ACTIONS} projectName="cat-panels" /></div>)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(ACTIONS[0].onSelect).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith('')
  })

  it('lists the tools artifact index (already-held prop, no extra fetch) alongside actions', async () => {
    const { container } = mount({ paletteActions: [], tools: [{ name: 'panel-cut', description: 'cuts panels' }] })
    openScope(container, 'act')
    expect(screen.getByText('panel-cut')).toBeTruthy()
  })

  it('fetches the versions and operator-sessions artifact indexes once the scope opens', async () => {
    const { container } = mount({ paletteActions: [], drawingId: 'demo' })
    openScope(container, 'act')
    await waitFor(() => expect(screen.getByText('v2')).toBeTruthy())
    expect(screen.getByText('opsess-1')).toBeTruthy()
  })
})

describe('find scope: consumes GET /api/search', () => {
  it('typing after opening "find" queries the search index and renders a result row', async () => {
    const onChange = vi.fn()
    const { container, rerender } = mount({ onChange })
    openScope(container, 'find')
    const input = container.querySelector('.bar-field')
    fireEvent.change(input, { target: { value: 'panel' } })
    rerender(<div data-testid="host"><PromptBox value="panel" onChange={onChange} onDispatch={noop} projectName="cat-panels" /></div>)
    await waitFor(() => expect(screen.getByText('panel-cut')).toBeTruthy(), { timeout: 2000 })
    expect(screen.getByText(/cuts panels/)).toBeTruthy()
  })
})

describe('the shortcut sheet row (slice 10b)', () => {
  it('a "Keyboard shortcuts" action row carries its real Shift+? cap, invented nowhere else', () => {
    const shortcuts = { id: 'bar:shortcuts', label: 'Keyboard shortcuts', icon: '', kbd: 'Shift+?', disabled: false, reason: '', onSelect: vi.fn() }
    const { container } = mount({ paletteActions: [shortcuts] })
    openScope(container, 'act')
    expect(screen.getByText('Keyboard shortcuts')).toBeTruthy()
    expect(screen.getByText('Shift+?')).toBeTruthy()
  })
})
