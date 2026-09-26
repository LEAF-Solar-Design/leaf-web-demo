// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import RoutePanel from './RoutePanel.jsx'
import { alternativeDecision } from '../controllers/catalog/catalogRouting.js'

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

  it('a catalog pick keeps the demo disclosure (W4g #125)', () => {
    const picked = alternativeDecision({
      lane: 'run',
      tool: null,
      confidence: 0,
      stub: true,
      stubKind: 'demo',
      alternatives: [{ tool: tools[0].name }],
    }, tools[0].name)

    mountRefusal(picked)

    expect(screen.getByText('This demo matches requests against a limited tool catalog.')).toBeTruthy()
    expect(screen.getByRole('button', { name: /^Run/ })).toBeTruthy()
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
    expect(screen.getByText('“inspect-live-geometry” is live-only, not in this catalog. Pick an alternative:').closest('.resolver-header')).not.toBeNull()
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

describe('RoutePanel solve lane', () => {
  const solverTools = [
    {
      name: 'string-autofill-opt',
      description: 'Autofill strings',
      capabilities: ['solve'],
      params: { properties: { panelsPerString: { type: 'integer' } } },
    },
    {
      name: 'solar-solve-proposal',
      description: 'Propose a solve',
      capabilities: ['solve'],
      params: { properties: {} },
    },
    {
      name: 'count-by-layer',
      description: 'Count',
      capabilities: ['drawing.read'],
      params: { properties: {} },
    },
  ]
  const boundRoute = {
    lane: 'run',
    routedLane: 'solve',
    tool: 'string-autofill-opt',
    confidence: 0.43,
    params: { panelsPerString: 12 },
    runIntent: { tool: 'string-autofill-opt', params: { panelsPerString: 12 } },
    alternatives: [{ tool: 'solar-solve-proposal', confidence: 0.39 }],
  }
  const unboundRoute = {
    lane: 'solve',
    tool: null,
    confidence: 0.8,
    params: { description: 'optimize the layout' },
    rationale: 'No solver matches this request; please pick one of the available solvers.',
    alternatives: [
      { tool: 'solar-solve-proposal', confidence: 0.15 },
      { tool: 'string-autofill-opt', confidence: 0.15 },
    ],
  }

  function mountSolve(nextRoute) {
    const clock = vi.spyOn(performance, 'now').mockReturnValue(0)
    const onConfirmIntent = vi.fn()
    const onPickAlternative = vi.fn()
    const onDismiss = vi.fn()
    const { container } = render(
      <RoutePanel route={nextRoute} tools={solverTools} running={false} writeLocked={false}
        onConfirmIntent={onConfirmIntent} onPickAlternative={onPickAlternative}
        onOpenAuthor={vi.fn()} onDismiss={onDismiss} />,
    )
    clock.mockReturnValue(500)
    return { container, onConfirmIntent, onPickAlternative, onDismiss }
  }

  it('offers a bound solver below the run floor as the confirm row', () => {
    const { container, onConfirmIntent, onPickAlternative } = mountSolve(boundRoute)

    expect(container.querySelector('.resolver-header').textContent).toBe('Solver match · 43% match')
    const options = screen.getAllByRole('option')
    expect(options).toHaveLength(2)
    expect(options[0].textContent).toContain('string-autofill-opt')
    expect(options[0].textContent).toContain('43%')
    expect(options[1].textContent).toContain('solar-solve-proposal')
    expect(options[1].textContent).toContain('39%')

    fireEvent.click(options[0])

    expect(onConfirmIntent).toHaveBeenCalledOnce()
    expect(onConfirmIntent).toHaveBeenCalledWith(boundRoute.runIntent, solverTools[0], { panelsPerString: 12 })
    expect(onPickAlternative).not.toHaveBeenCalled()

    fireEvent.click(options[1])

    expect(onPickAlternative).toHaveBeenCalledWith('solar-solve-proposal')
  })

  it('shows the bound solver parameters inside the confirm row', () => {
    mountSolve(boundRoute)

    const options = screen.getAllByRole('option')
    expect(options[0].querySelector('.label').textContent)
      .toBe('string-autofill-opt · Autofill strings · params {"panelsPerString":12}')
    expect(options[1].querySelector('.label').textContent)
      .toBe('solar-solve-proposal · Propose a solve')
  })

  it('offers and confirms a bound solver at exactly the solve floor', () => {
    const nextRoute = { ...boundRoute, confidence: 0.30 }
    const { container, onConfirmIntent, onPickAlternative } = mountSolve(nextRoute)

    expect(container.querySelector('.resolver-header').textContent).toBe('Solver match · 30% match')
    const options = screen.getAllByRole('option')
    expect(options).toHaveLength(2)
    expect(options[0].textContent).toContain('string-autofill-opt')
    expect(options[0].textContent).toContain('30%')
    expect(onConfirmIntent).not.toHaveBeenCalled()

    fireEvent.click(options[0])

    expect(onConfirmIntent).toHaveBeenCalledOnce()
    expect(onConfirmIntent).toHaveBeenCalledWith(nextRoute.runIntent, solverTools[0], { panelsPerString: 12 })
    expect(onPickAlternative).not.toHaveBeenCalled()
  })

  it('confirms a bound solver on Enter', () => {
    const { onConfirmIntent, onPickAlternative } = mountSolve(boundRoute)

    fireEvent.keyDown(document.body, { key: 'Enter' })

    expect(onConfirmIntent).toHaveBeenCalledOnce()
    expect(onConfirmIntent).toHaveBeenCalledWith(boundRoute.runIntent, solverTools[0], { panelsPerString: 12 })
    expect(onPickAlternative).not.toHaveBeenCalled()
  })

  it('says Solve with the tool on a confident bound solve', () => {
    const nextRoute = { ...boundRoute, confidence: 0.9 }
    const { container, onConfirmIntent } = mountSolve(nextRoute)

    expect(container.querySelector('.strip-sentence').textContent.startsWith('Solve with string-autofill-opt')).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Run string-autofill-opt' }))

    expect(onConfirmIntent).toHaveBeenCalledOnce()
    expect(onConfirmIntent).toHaveBeenCalledWith(nextRoute.runIntent, solverTools[0], { panelsPerString: 12 })
  })

  it('keeps a weak solver binding below the solve floor as picks', () => {
    const { container, onConfirmIntent, onPickAlternative } = mountSolve({
      lane: 'run',
      routedLane: 'solve',
      tool: 'string-autofill-opt',
      confidence: 0.2,
      params: {},
      runIntent: { tool: 'string-autofill-opt', params: {} },
      alternatives: [],
    })

    expect(container.querySelector('.resolver-header').textContent).toBe('Solver match · 20% match')
    const options = screen.getAllByRole('option')
    expect(options).toHaveLength(3)
    options.forEach((option, index) => {
      expect(option.textContent).toContain(solverTools[index].name)
      expect(option.textContent).not.toContain('%')
    })

    fireEvent.click(options[0])

    expect(onPickAlternative).toHaveBeenCalledWith('string-autofill-opt')
    expect(onConfirmIntent).not.toHaveBeenCalled()
  })

  it('offers each solver alternative of an unbound solve as a pick', () => {
    const { container, onConfirmIntent, onPickAlternative } = mountSolve(unboundRoute)

    expect(container.querySelector('.strip-sentence').textContent)
      .toBe('Solve: no solver matched this request. Pick one to review it before it runs.')
    const buttons = screen.getAllByRole('button')
    expect(buttons.map((button) => button.getAttribute('aria-label'))).toEqual([
      'Pick solver solar-solve-proposal',
      'Pick solver string-autofill-opt',
    ])

    fireEvent.click(buttons[1])

    expect(onPickAlternative).toHaveBeenCalledWith('string-autofill-opt')
    expect(onConfirmIntent).not.toHaveBeenCalled()
    expect(screen.queryByText(/not connected in this demo/)).toBeNull()
  })

  it('keeps an unbound solve without a solver honest', () => {
    const { container } = mountSolve({
      lane: 'solve',
      tool: null,
      confidence: 0.8,
      alternatives: [
        { tool: 'count-by-layer', confidence: 0.15 },
        { tool: 'missing-solver', confidence: 0.15 },
      ],
    })

    expect(container.querySelector('.strip-sentence').textContent)
      .toBe('Solve: no solver is available for this request. Nothing was executed.')
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('dismisses an unbound solve on Enter without picking', () => {
    const { onDismiss, onPickAlternative, onConfirmIntent } = mountSolve(unboundRoute)

    fireEvent.keyDown(document.body, { key: 'Enter' })

    expect(onDismiss).toHaveBeenCalledOnce()
    expect(onPickAlternative).not.toHaveBeenCalled()
    expect(onConfirmIntent).not.toHaveBeenCalled()
  })
})
