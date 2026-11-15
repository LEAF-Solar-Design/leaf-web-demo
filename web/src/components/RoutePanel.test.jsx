// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
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
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('proposal and run cards', () => {
  const proposalTool = { name: 'solar-string', title: 'String the roof', capabilities: ['drawing.read'], params: { properties: {} } }
  const proposal = { lane: 'run', confidence: .99, tool: proposalTool.name, summary: 'Assign all roof panels to strings.',
    runIntent: { tool: proposalTool.name, params: { design_min_temp: -20, max_system_v: 1000, custom_value: 12 } } }
  const callbacks = () => ({ onConfirmIntent: vi.fn(), onPickAlternative: vi.fn(), onDismiss: vi.fn() })

  it('shows a human title, sentence-case labels, units and the proposed change', () => {
    const { container } = render(<RoutePanel route={proposal} tools={[proposalTool]} running={false} {...callbacks()} />)
    expect(screen.getByText('String the roof')).toBeTruthy()
    expect(screen.getByText(proposal.summary)).toBeTruthy()
    const rows = container.querySelector('.proposal-fields').textContent
    expect(rows).toContain('Design min temp-20 °C')
    expect(rows).toContain('Max system V1000 V')
    expect(rows).toContain('Custom value12')
    expect(screen.queryByRole('button', { name: 'Check' })).toBeNull()
    expect(container.querySelector('[aria-current="step"]').textContent).toBe('Proposal')
  })

  it('offers Check only with a real validation callback', () => {
    const validate = vi.fn()
    render(<RoutePanel route={proposal} tools={[proposalTool]} running={false} onValidate={validate} {...callbacks()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Check' }))
    expect(validate).toHaveBeenCalledWith(proposal.runIntent, proposalTool, proposal.runIntent.params)
  })

  it('omits absent and default-only metrics and preserves a proposed zero', () => {
    const tool = { ...proposalTool, params: { properties: { module: { default: 'Schema default' } } } }
    const nextRoute = { ...proposal, runIntent: { ...proposal.runIntent, params: { panels_in_scope: 0 } },
      metrics: { module: null, design_min_temp_c: -18, max_system_voltage_v: 1000 } }
    const { container } = render(<RoutePanel route={nextRoute} tools={[tool]} running={false} {...callbacks()} />)
    const metrics = container.querySelector('.proposal-metrics')
    expect(metrics.textContent).toBe('Panels in scope0Design min temp-18 °CMax system V1000 V')
    expect(Array.from(metrics.querySelectorAll('dt')).map((node) => node.textContent)).not.toContain('Module')
  })

  it('turns a low-confidence confirmed choice into the run card immediately', () => {
    const handlers = callbacks()
    const { container } = render(<RoutePanel route={{ ...proposal, confidence: .6 }} tools={[proposalTool]} running={false} {...handlers} />)
    fireEvent.click(screen.getByRole('option'))
    expect(screen.getByText('Planning · 0 s')).toBeTruthy()
    expect(container.querySelector('.proposal-card.is-running')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Run solar-string' }).disabled).toBe(true)
    expect(handlers.onConfirmIntent).toHaveBeenCalledOnce()
  })

  it('keeps the proposal in place while the caller consumes the route to start work', () => {
    const handlers = callbacks()
    const { container, rerender } = render(<RoutePanel route={proposal} tools={[proposalTool]} running={false} {...handlers} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run solar-string' }))
    rerender(<RoutePanel route={null} tools={[proposalTool]} running {...handlers} />)
    expect(screen.getByText('Planning · 0 s')).toBeTruthy()
    expect(container.querySelector('[aria-current="step"]').textContent).toBe('Run')
  })

  it('shows Planning on the Run render, advances the trail and ticks elapsed seconds', () => {
    vi.useFakeTimers()
    const handlers = callbacks()
    const { container, rerender } = render(<RoutePanel route={proposal} tools={[proposalTool]} running={false} {...handlers} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run solar-string' }))
    expect(screen.getByText('Planning · 0 s')).toBeTruthy()
    expect(container.querySelector('.proposal-card.is-running')).toBeTruthy()
    expect(container.querySelector('[aria-current="step"]').textContent).toBe('Run')
    expect(Array.from(container.querySelectorAll('.proposal-metrics dd')).every((node) => node.textContent === '—')).toBe(true)
    expect(handlers.onConfirmIntent).toHaveBeenCalledOnce()
    act(() => vi.advanceTimersByTime(2000))
    expect(screen.getByText('Planning · 2 s')).toBeTruthy()
    rerender(<RoutePanel route={proposal} tools={[proposalTool]} running {...handlers} />)
    rerender(<RoutePanel route={proposal} tools={[proposalTool]} running={false} {...handlers} />)
    expect(container.querySelector('[aria-current="step"]').textContent).toBe('Result')
  })
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
  it.each(['body', 'command bar'])('ignores repeated Enter after cooldown from the %s', (target) => {
    const confirm = mount(<textarea data-testid="command" className="bar-field" role="combobox" aria-expanded="false" />)
    const element = target === 'body' ? document.body : screen.getByTestId('command')
    element.focus()
    const event = new KeyboardEvent('keydown', { key: 'Enter', repeat: true, bubbles: true, cancelable: true })
    const stop = vi.spyOn(event, 'stopPropagation')

    fireEvent(element, event)

    expect(confirm).not.toHaveBeenCalled()
    expect(stop).not.toHaveBeenCalled()
    expect(event.defaultPrevented).toBe(false)
  })

  it('leaves IME Enter with key code 229 in the collapsed command bar', () => {
    const confirm = mount(<textarea data-testid="command" className="bar-field" role="combobox" aria-expanded="false" />)
    const command = screen.getByTestId('command')
    command.focus()
    const event = new KeyboardEvent('keydown', {
      key: 'Enter', keyCode: 229, isComposing: false, bubbles: true, cancelable: true,
    })
    const stop = vi.spyOn(event, 'stopPropagation')

    fireEvent(command, event)

    expect(confirm).not.toHaveBeenCalled()
    expect(stop).not.toHaveBeenCalled()
    expect(event.defaultPrevented).toBe(false)
  })

  it('confirms a fresh command bar Enter after cooldown', () => {
    const confirm = mount(<textarea data-testid="command" className="bar-field" role="combobox" aria-expanded="false" />)
    const command = screen.getByTestId('command')
    command.focus()

    fireEvent.keyDown(command, { key: 'Enter', repeat: true })
    expect(confirm).not.toHaveBeenCalled()

    fireEvent.keyDown(command, { key: 'Enter', repeat: false })

    expect(confirm).toHaveBeenCalledOnce()
    expect(confirm).toHaveBeenCalledWith(route.runIntent, tools[0], {})
  })

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

    // Alternatives are available during review; confirming replaces the
    // resolver with its run card, so choose the alternative before Run.
    fireEvent.click(options[1])
    expect(onPickAlternative).toHaveBeenCalledWith('solar-solve-proposal')
    onPickAlternative.mockClear()

    fireEvent.click(options[0])

    expect(onConfirmIntent).toHaveBeenCalledOnce()
    expect(onConfirmIntent).toHaveBeenCalledWith(boundRoute.runIntent, solverTools[0], { panelsPerString: 12 })
    expect(onPickAlternative).not.toHaveBeenCalled()
  })

  it('shows the bound solver parameters inside the confirm row', () => {
    mountSolve(boundRoute)

    const options = screen.getAllByRole('option')
    expect(options[0].querySelector('.label').textContent)
      .toBe('string-autofill-opt · Autofill strings · panels per string 12')
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

describe('RoutePanel arrow ownership', () => {
  const arrowTools = [
    { name: 'count-panels', description: 'Count panels', capabilities: ['drawing.read'], params: { properties: {} } },
    { name: 'list-layers', description: 'List layers', capabilities: ['drawing.read'], params: { properties: {} } },
    { name: 'measure-area', description: 'Measure area', capabilities: ['drawing.read'], params: { properties: {} } },
  ]
  // A live no-match offers every catalog tool as a pick: three resolver rows.
  const pickRoute = { lane: 'run', tool: null, confidence: 0.1, alternatives: [] }

  function mountArrows(editor, nextRoute = pickRoute) {
    const callbacks = {
      onConfirmIntent: vi.fn(),
      onPickAlternative: vi.fn(),
      onOpenAuthor: vi.fn(),
      onDismiss: vi.fn(),
    }
    render(
      <>
        {editor}
        <RoutePanel route={nextRoute} tools={arrowTools} running={false} writeLocked={false} {...callbacks} />
      </>,
    )
    return callbacks
  }
  // Only the resolver's own rows: a native select brings options of its own.
  const options = () => Array.from(screen.getByRole('listbox', { name: 'Route resolver' }).querySelectorAll('[role="option"]'))
  const selected = () => options().findIndex((option) => option.getAttribute('aria-selected') === 'true')
  const untouched = (callbacks) => Object.values(callbacks).every((fn) => fn.mock.calls.length === 0)

  // fireEvent returns false only when a handler prevented the default action.
  it.each([
    ['input', <input data-testid="editor" />],
    ['textarea', <textarea data-testid="editor" />],
    ['select', <select data-testid="editor"><option>one</option><option>two</option></select>],
    ['nested contenteditable', <div contentEditable suppressContentEditableWarning><span data-testid="editor">text</span></div>],
    ['textbox role', <div data-testid="editor" role="textbox" tabIndex={0} />],
    ['searchbox role', <div data-testid="editor" role="searchbox" tabIndex={0} />],
    ['combobox role', <div data-testid="editor" role="combobox" tabIndex={0} />],
    ['spinbutton role', <div data-testid="editor" role="spinbutton" tabIndex={0} />],
  ])('KEYS-A01 arrows from %s stay with the editor', (_name, editor) => {
    const callbacks = mountArrows(editor)
    const target = screen.getByTestId('editor')
    target.focus()
    const focused = document.activeElement

    expect(selected()).toBe(0)
    expect(fireEvent.keyDown(target, { key: 'ArrowDown' })).toBe(true)
    expect(selected()).toBe(0)

    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    expect(selected()).toBe(1)
    expect(fireEvent.keyDown(target, { key: 'ArrowUp' })).toBe(true)
    expect(selected()).toBe(1)

    expect(document.activeElement).toBe(focused)
    expect(untouched(callbacks)).toBe(true)
  })

  it('KEYS-A02 arrows outside an editor still move the active row and clamp', () => {
    const callbacks = mountArrows(null)

    expect(selected()).toBe(0)
    expect(fireEvent.keyDown(document.body, { key: 'ArrowUp' })).toBe(false)
    expect(selected()).toBe(0)
    expect(fireEvent.keyDown(document.body, { key: 'ArrowDown' })).toBe(false)
    expect(selected()).toBe(1)
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    expect(selected()).toBe(2)
    expect(fireEvent.keyDown(document.body, { key: 'ArrowDown' })).toBe(false)
    expect(selected()).toBe(2)
    fireEvent.keyDown(document.body, { key: 'ArrowUp' })
    expect(selected()).toBe(1)
    expect(untouched(callbacks)).toBe(true)
  })

  it('KEYS-A03 an arrow from a focused row moves focus with the selection', () => {
    mountArrows(null)
    options()[0].focus()

    expect(fireEvent.keyDown(options()[0], { key: 'ArrowDown' })).toBe(false)
    expect(selected()).toBe(1)
    expect(document.activeElement).toBe(options()[1])

    fireEvent.keyDown(options()[1], { key: 'ArrowUp' })
    expect(selected()).toBe(0)
    expect(document.activeElement).toBe(options()[0])
  })

  it('KEYS-A04 an arrow from inside a row button reaches the next row', () => {
    mountArrows(null)
    const inner = options()[0].querySelector('.route-tool')

    expect(inner).not.toBeNull()
    expect(fireEvent.keyDown(inner, { key: 'ArrowDown' })).toBe(false)
    expect(selected()).toBe(1)
  })

  it('KEYS-A05 a region marked not editable does not hold the arrows', () => {
    mountArrows(<div data-testid="plain" contentEditable={false} tabIndex={0} />)

    expect(fireEvent.keyDown(screen.getByTestId('plain'), { key: 'ArrowDown' })).toBe(false)
    expect(selected()).toBe(1)
  })

  it('KEYS-A06 a decision with no rows leaves every arrow alone', () => {
    const callbacks = mountArrows(<input data-testid="editor" />, {
      lane: 'run',
      tool: 'count-panels',
      confidence: 0.99,
      runIntent: { tool: 'count-panels', params: {} },
    })

    expect(screen.queryAllByRole('option')).toHaveLength(0)
    expect(fireEvent.keyDown(document.body, { key: 'ArrowDown' })).toBe(true)
    expect(fireEvent.keyDown(screen.getByTestId('editor'), { key: 'ArrowUp' })).toBe(true)
    expect(untouched(callbacks)).toBe(true)
  })

  it('KEYS-A07 Enter acts on the row the arrows chose, never on an editor arrow', () => {
    const clock = vi.spyOn(performance, 'now').mockReturnValue(0)
    const callbacks = mountArrows(<input data-testid="editor" />)
    clock.mockReturnValue(500)

    fireEvent.keyDown(screen.getByTestId('editor'), { key: 'ArrowDown' })
    fireEvent.keyDown(screen.getByTestId('editor'), { key: 'ArrowDown' })
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    fireEvent.keyDown(document.body, { key: 'Enter' })

    expect(callbacks.onPickAlternative).toHaveBeenCalledTimes(1)
    expect(callbacks.onPickAlternative).toHaveBeenCalledWith('list-layers')
    expect(callbacks.onConfirmIntent).not.toHaveBeenCalled()
  })

  it('KEYS-A08 an arrow a control already handled stays with that control', () => {
    const callbacks = mountArrows(
      <>
        <button
          type="button"
          data-testid="handled"
          onKeyDown={(e) => { if (e.key === 'ArrowDown' || e.key === 'ArrowUp') e.preventDefault() }}
        >
          stepper
        </button>
        <button type="button" data-testid="plain">plain</button>
      </>,
    )
    const handled = screen.getByTestId('handled')
    handled.focus()

    expect(selected()).toBe(0)
    expect(fireEvent.keyDown(handled, { key: 'ArrowDown' })).toBe(false)
    expect(selected()).toBe(0)

    // The same kind of control with no handler of its own leaves the arrow to the list.
    expect(fireEvent.keyDown(screen.getByTestId('plain'), { key: 'ArrowDown' })).toBe(false)
    expect(selected()).toBe(1)
    expect(fireEvent.keyDown(handled, { key: 'ArrowUp' })).toBe(false)
    expect(selected()).toBe(1)

    expect(document.activeElement).toBe(handled)
    expect(untouched(callbacks)).toBe(true)
  })

  it('KEYS-A09 an arrow an open menu took upstream is not the list\'s', () => {
    const callbacks = mountArrows(null)
    // The shape the scope picker and the project switcher use while they are open.
    const menu = (e) => { if (e.key === 'ArrowDown' || e.key === 'ArrowUp') e.preventDefault() }
    window.addEventListener('keydown', menu, true)
    try {
      expect(fireEvent.keyDown(document.body, { key: 'ArrowDown' })).toBe(false)
      expect(selected()).toBe(0)
    } finally {
      window.removeEventListener('keydown', menu, true)
    }

    expect(fireEvent.keyDown(document.body, { key: 'ArrowDown' })).toBe(false)
    expect(selected()).toBe(1)
    expect(untouched(callbacks)).toBe(true)
  })
})
