// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import RoutePanel from './RoutePanel.jsx'

const route = {
  lane: 'run',
  tool: 'delete-marked-panel',
  confidence: 0.99,
  runIntent: { tool: 'delete-marked-panel', params: {} },
}
const tools = [{
  name: 'delete-marked-panel',
  capabilities: ['drawing.write'],
  params: { properties: {} },
}]

function mount(editor, onConfirmIntent = vi.fn()) {
  const clock = vi.spyOn(performance, 'now').mockReturnValue(0)
  render(
    <>
      {editor}
      <RoutePanel
        route={route}
        tools={tools}
        running={false}
        writeLocked={false}
        onConfirmIntent={onConfirmIntent}
        onPickAlternative={vi.fn()}
        onOpenAuthor={vi.fn()}
        onDismiss={vi.fn()}
      />
    </>,
  )
  clock.mockReturnValue(500)
  return onConfirmIntent
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('RoutePanel demo refusals and outages', () => {
  function mountRefusal(nextRoute) {
    const onPickAlternative = vi.fn()
    const onConfirmIntent = vi.fn()
    render(<>
      <input aria-label="Request" defaultValue="Inspect unusual geometry" />
      <RoutePanel route={nextRoute} tools={tools} running={false} writeLocked={false}
        onPickAlternative={onPickAlternative} onConfirmIntent={onConfirmIntent}
        onOpenAuthor={vi.fn()} onDismiss={vi.fn()} />
    </>)
    return { onPickAlternative, onConfirmIntent }
  }

  it('shows the demo limit and preserves input until a catalog tool is picked', () => {
    const callbacks = mountRefusal({ lane: 'run', tool: null, confidence: 0, stub: true, stubKind: 'demo' })
    expect(screen.getByText('This demo matches requests against a limited tool catalog.')).toBeTruthy()
    expect(screen.getByText('No matching tool in this demo. Try another description or browse available tools.').closest('.resolver-header')).not.toBeNull()
    expect(screen.queryByText(/live-only|isn’t a tool in this catalog/)).toBeNull()
    expect(screen.getByLabelText('Request').value).toBe('Inspect unusual geometry')
    expect(screen.queryByRole('button', { name: /^Run/ })).toBeNull()
    expect(screen.queryByText(/Routing is unavailable/)).toBeNull()
    expect(callbacks.onConfirmIntent).not.toHaveBeenCalled()
    expect(callbacks.onPickAlternative).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('option'))
    expect(callbacks.onPickAlternative).toHaveBeenCalledWith(tools[0].name)
    expect(callbacks.onConfirmIntent).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Request').value).toBe('Inspect unusual geometry')
  })

  it('announces the number of repeated demo refusals', () => {
    mountRefusal({ lane: 'run', tool: null, confidence: 0, stub: true, stubKind: 'demo', repeat: 3 })
    const sentence = screen.getByText('No matching tool in this demo. Try another description or browse available tools. Still no match after 3 tries.')
    expect(sentence.tagName).toBe('SPAN')
    expect(sentence.getAttribute('aria-live')).toBe('polite')
    expect(sentence.closest('.resolver-header')).not.toBeNull()
  })

  it('keeps the first refusal sentence unchanged', () => {
    mountRefusal({ lane: 'run', tool: null, confidence: 0, stub: true, stubKind: 'demo' })
    expect(screen.getByText('No matching tool in this demo. Try another description or browse available tools.').textContent)
      .toBe('No matching tool in this demo. Try another description or browse available tools.')
    expect(screen.queryByText(/Still no match after/)).toBeNull()
  })

  it.each([null, '', '   '])('keeps the live no-match header with catalog picks for tool %s', (tool) => {
    const callbacks = mountRefusal({ lane: 'run', tool, confidence: 0.1, alternatives: [] })
    expect(screen.getByText('No matching capability. Try another description or browse available tools.').closest('.resolver-header')).not.toBeNull()
    expect(screen.queryByText(/live-only|isn’t a tool in this catalog/)).toBeNull()
    expect(screen.getAllByRole('option')).toHaveLength(tools.length)
    fireEvent.click(screen.getByRole('option'))
    expect(callbacks.onPickAlternative).toHaveBeenCalledWith(tools[0].name)
    expect(callbacks.onConfirmIntent).not.toHaveBeenCalled()
  })

  it('keeps the live-only header for a named live tool below the floor', () => {
    const callbacks = mountRefusal({ lane: 'run', tool: 'inspect-live-geometry', confidence: 0.1, alternatives: [] })
    expect(screen.getByText('“inspect-live-geometry” is live-only — not in this catalog. Pick an alternative:').closest('.resolver-header')).not.toBeNull()
    expect(screen.queryByText(/No matching capability/)).toBeNull()
    fireEvent.click(screen.getByRole('option'))
    expect(callbacks.onPickAlternative).toHaveBeenCalledWith(tools[0].name)
    expect(callbacks.onConfirmIntent).not.toHaveBeenCalled()
  })

  it('offers only catalog picks during a live outage, even for a confident fallback', () => {
    const callbacks = mountRefusal({ ...route, stub: true, stubKind: 'outage', stubReason: 'Connection lost' })
    expect(screen.getByText('Routing is unavailable right now: Connection lost')).toBeTruthy()
    expect(screen.queryByText('This demo matches requests against a limited tool catalog.')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Run/ })).toBeNull()
    fireEvent.click(screen.getByRole('option'))
    expect(callbacks.onPickAlternative).toHaveBeenCalledWith(tools[0].name)
    expect(callbacks.onConfirmIntent).not.toHaveBeenCalled()
  })

  it('does not authorize a below-floor tool supplied in a route', () => {
    const callbacks = mountRefusal({ ...route, confidence: 0.54, stubKind: 'demo' })
    expect(screen.queryByRole('button', { name: /^Run/ })).toBeNull()
    fireEvent.click(screen.getByRole('option'))
    expect(callbacks.onConfirmIntent).not.toHaveBeenCalled()
  })
})

describe('RoutePanel Enter ownership', () => {
  it.each([
    ['input', <input data-testid="editor" />],
    ['textarea', <textarea data-testid="editor" />],
    ['select', <select data-testid="editor"><option>one</option></select>],
    ['contenteditable', <div data-testid="editor" contentEditable />],
    ['textbox role', <div data-testid="editor" role="textbox" tabIndex={0} />],
  ])('leaves Enter from a focused %s with that editor', (_name, editor) => {
    const confirm = mount(editor)

    fireEvent.keyDown(screen.getByTestId('editor'), { key: 'Enter' })

    expect(confirm).not.toHaveBeenCalled()
  })

  it('keeps deliberate non-editor Enter confirmation', () => {
    const confirm = mount(null)

    fireEvent.keyDown(document.body, { key: 'Enter' })

    expect(confirm).toHaveBeenCalledOnce()
    expect(confirm).toHaveBeenCalledWith(route.runIntent, tools[0], {})
  })
})
