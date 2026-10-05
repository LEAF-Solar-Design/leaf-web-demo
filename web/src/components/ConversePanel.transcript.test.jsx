/**
 * S10 agent transcript: each tool step is one ledger row with its state as a
 * WORD (not only a dot colour), reasoning between steps collapses to one pulse
 * plus a verb, a "Jump to latest" chip appears only when the reader scrolls
 * up past the 48 px bottom lock, and a machine summary the payload marks
 * carries a quiet "Written by Claude" chip. `../converse.js` is mocked so the
 * stream is driven event by event through the panel's own onEvent handler.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))
vi.mock('../converse.js', () => ({
  openStream: vi.fn(() => ({ close: vi.fn() })),
  postMessage: vi.fn(),
  resolveApproval: vi.fn(),
  listPendingApprovals: vi.fn(() => Promise.resolve([])),
  cancelTurn: vi.fn(),
  classifyAgentError: vi.fn(() => 'unreachable'),
}))

import * as converse from '../converse.js'
import ConversePanel from './ConversePanel.jsx'

const event = (type, turn_id, data = {}) => ({ type, turn_id, data })

const push = async (...events) => {
  await act(async () => {
    const handlers = converse.openStream.mock.calls.at(-1)[2]
    events.forEach(handlers.onEvent)
  })
}

const setup = async (events = []) => {
  const view = render(<ConversePanel sessionId="session-1" onDismiss={vi.fn()} />)
  await push(...events)
  return view
}

const steps = () => [...document.querySelectorAll('.converse-step')]
const stateWord = (row) => row.querySelector('.converse-step-state')?.textContent
const reasoningRows = () => screen.queryAllByTestId('converse-reasoning')
const jumpChip = () => screen.queryByRole('button', { name: /jump to latest/i })
const log = () => screen.getByRole('log', { name: 'Assistant conversation' })

// jsdom lays nothing out, so the log's scroll geometry is set by hand.
const geometry = (el, { scrollHeight, clientHeight, scrollTop }) => {
  Object.defineProperty(el, 'scrollHeight', { configurable: true, value: scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, value: clientHeight })
  Object.defineProperty(el, 'scrollTop', { configurable: true, writable: true, value: scrollTop })
}
const scrollTo = (el, scrollTop) => {
  el.scrollTop = scrollTop
  fireEvent.scroll(el)
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('tool steps render as ledger rows with a state word', () => {
  it('reads Running, then Done, with the verb and its object', async () => {
    await setup([
      event('turn_started', 't1'),
      event('tool_call', 't1', { tool: 'count_panels', args_summary: 'layer roofline' }),
    ])
    let [row] = steps()
    expect(steps()).toHaveLength(1)
    expect(row).toHaveAttribute('data-state', 'running')
    expect(stateWord(row)).toBe('Running')
    expect(row.querySelector('.dot.live.pulse')).not.toBeNull()
    expect(row.querySelector('.converse-step-verb')).toHaveTextContent('count panels')
    expect(row.querySelector('.converse-step-object')).toHaveTextContent('layer roofline')

    await push(event('tool_result', 't1', { tool: 'count_panels', ok: true, summary: '42 panels' }))
    ;[row] = steps()
    expect(steps()).toHaveLength(1)
    expect(row).toHaveAttribute('data-state', 'done')
    expect(stateWord(row)).toBe('Done')
    expect(row.querySelector('.dot.live')).toBeNull()
    expect(row).toHaveTextContent('layer roofline')
    expect(row).toHaveTextContent('42 panels')
  })

  it('reads Failed for a step whose result is not ok', async () => {
    await setup([
      event('turn_started', 't1'),
      event('tool_call', 't1', { tool: 'read-layers', args_summary: 'all' }),
      event('tool_result', 't1', { tool: 'read-layers', ok: false, summary: 'drawing unreadable' }),
    ])
    const [row] = steps()
    expect(row).toHaveAttribute('data-state', 'failed')
    expect(stateWord(row)).toBe('Failed')
    expect(row.querySelector('.dot.red')).not.toBeNull()
    expect(row.querySelector('.converse-step-verb')).toHaveTextContent('read layers')
    expect(row).toHaveTextContent('drawing unreadable')
  })

  it('keeps one row per step, in order', async () => {
    await setup([
      event('turn_started', 't1'),
      event('tool_call', 't1', { tool: 'count_panels', args_summary: 'roofline' }),
      event('tool_result', 't1', { tool: 'count_panels', ok: true, summary: '42' }),
      event('tool_call', 't1', { tool: 'measure_roof', args_summary: 'north face' }),
    ])
    expect(steps().map(stateWord)).toEqual(['Done', 'Running'])
  })
})

describe('reasoning collapses to one pulse plus a verb', () => {
  it('shows only between visible steps of the active turn', async () => {
    await setup([event('turn_started', 't1')])
    expect(reasoningRows()).toHaveLength(1)
    expect(reasoningRows()[0]).toHaveTextContent('thinking…')
    expect(reasoningRows()[0].querySelector('.dot.live.pulse')).not.toBeNull()

    // A running step carries its own pulse, so the reasoning row folds away.
    await push(event('tool_call', 't1', { tool: 'count_panels', args_summary: 'roofline' }))
    expect(reasoningRows()).toHaveLength(0)

    // The step settled; the agent is reasoning again: still ONE row.
    await push(event('tool_result', 't1', { tool: 'count_panels', ok: true, summary: '42' }))
    expect(reasoningRows()).toHaveLength(1)

    // Streaming prose is its own evidence.
    await push(event('text_delta', 't1', { text: 'There are 42 panels.' }))
    expect(reasoningRows()).toHaveLength(0)

    await push(event('turn_complete', 't1', { stop_reason: 'end_turn' }))
    expect(reasoningRows()).toHaveLength(0)
  })

  it('never shows for a finished turn', async () => {
    await setup([
      event('turn_started', 't1'),
      event('tool_call', 't1', { tool: 'count_panels' }),
      event('tool_result', 't1', { tool: 'count_panels', ok: true }),
      event('turn_complete', 't1', { stop_reason: 'end_turn' }),
    ])
    expect(reasoningRows()).toHaveLength(0)
  })
})

describe('Jump to latest', () => {
  const finished = [
    event('turn_started', 't1'),
    event('text_delta', 't1', { text: 'First answer.' }),
    event('turn_complete', 't1', { stop_reason: 'end_turn' }),
  ]

  it('appears only when the reader scrolls past the 48 px lock', async () => {
    await setup(finished)
    const el = log()
    geometry(el, { scrollHeight: 1000, clientHeight: 200, scrollTop: 800 })
    expect(jumpChip()).toBeNull()

    scrollTo(el, 790) // 10 px from the end
    expect(jumpChip()).toBeNull()
    scrollTo(el, 753) // 47 px: still inside the lock
    expect(jumpChip()).toBeNull()
    scrollTo(el, 752) // 48 px: scrolled away
    expect(jumpChip()).not.toBeNull()
    expect(jumpChip().querySelector('kbd.key')).toHaveTextContent('End')

    // Scrolling back by hand retires it.
    scrollTo(el, 800)
    expect(jumpChip()).toBeNull()
  })

  it('does not yank a reader who scrolled away, and the chip jumps them back', async () => {
    await setup(finished)
    const el = log()
    geometry(el, { scrollHeight: 1000, clientHeight: 200, scrollTop: 800 })
    scrollTo(el, 300)
    expect(jumpChip()).not.toBeNull()

    await push(event('turn_started', 't2'), event('text_delta', 't2', { text: 'More.' }))
    expect(el.scrollTop).toBe(300)
    expect(jumpChip()).not.toBeNull()

    fireEvent.click(jumpChip())
    expect(el.scrollTop).toBe(1000)
    expect(jumpChip()).toBeNull()
  })

  it('its End keycap is live, but never inside the reply field', async () => {
    await setup(finished)
    const el = log()
    geometry(el, { scrollHeight: 1000, clientHeight: 200, scrollTop: 800 })
    scrollTo(el, 100)

    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Reply to the assistant' }), { key: 'End' })
    expect(el.scrollTop).toBe(100)
    expect(jumpChip()).not.toBeNull()

    fireEvent.keyDown(document.body, { key: 'End' })
    expect(el.scrollTop).toBe(1000)
    expect(jumpChip()).toBeNull()
  })
})

describe('Written by Claude', () => {
  it('attributes exactly the marked machine summary, never plain prose', async () => {
    await setup([
      event('turn_started', 't1'),
      event('text_delta', 't1', { text: 'Plain prose from the turn.' }),
      event('text_delta', 't1', { text: 'Summary: 42 panels on the roofline.', written_by: 'claude' }),
      event('turn_complete', 't1', { stop_reason: 'end_turn' }),
    ])
    const chips = screen.getAllByTestId('converse-written-by')
    expect(chips).toHaveLength(1)
    expect(chips[0]).toHaveTextContent('Written by Claude')
    const marked = chips[0].closest('.converse-msg')
    expect(marked).toHaveTextContent('Summary: 42 panels on the roofline.')
    expect(marked).not.toHaveTextContent('Plain prose from the turn.')
    const plain = screen.getByText('Plain prose from the turn.').closest('.converse-msg')
    expect(plain.querySelector('[data-testid="converse-written-by"]')).toBeNull()
  })

  it('attributes a marked step result and leaves unmarked ones alone', async () => {
    await setup([
      event('turn_started', 't1'),
      event('tool_call', 't1', { tool: 'summarize_layout', args_summary: 'roof' }),
      event('tool_result', 't1', { tool: 'summarize_layout', ok: true, summary: 'three rows', machine_summary: true }),
      event('tool_call', 't1', { tool: 'count_panels', args_summary: 'roof' }),
      event('tool_result', 't1', { tool: 'count_panels', ok: true, summary: '42', written_by: 'operator' }),
      event('turn_complete', 't1', { stop_reason: 'end_turn' }),
    ])
    const [summarized, counted] = steps()
    expect(summarized.querySelector('[data-testid="converse-written-by"]')).toHaveTextContent('Written by Claude')
    expect(counted.querySelector('[data-testid="converse-written-by"]')).toBeNull()
    expect(screen.getAllByTestId('converse-written-by')).toHaveLength(1)
  })
})
