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

const ts = '2026-11-23T08:00:00.000Z'
const event = (type, turn_id, data = {}) => ({ type, turn_id, data, ts })
const setup = async (props, events = []) => {
  render(<ConversePanel sessionId="session" onDismiss={vi.fn()} userTurns={[]} {...props} />)
  await act(async () => {
    const handlers = converse.openStream.mock.calls.at(-1)[2]
    events.forEach(handlers.onEvent)
  })
}
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('New conversation in the assistant head', () => {
  it('shows beside Hide and calls the one App handler', async () => {
    const onNewConversation = vi.fn()
    await setup({ onNewConversation })
    const button = screen.getByRole('button', { name: 'New conversation' })
    const hide = screen.getByRole('button', { name: 'Hide' })
    expect(button.closest('.converse-head')).toBe(hide.closest('.converse-head'))
    expect(button.nextElementSibling).toBe(hide)
    fireEvent.click(button)
    expect(onNewConversation).toHaveBeenCalledTimes(1)
  })

  it('is hidden while the handler is null (the ribbon guards apply)', async () => {
    await setup({ onNewConversation: null })
    expect(screen.queryByRole('button', { name: 'New conversation' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Hide' })).toBeTruthy()
  })
})

describe('failed tool chip', () => {
  it('drops the stacked error prefixes from a failed result', async () => {
    await setup({}, [
      event('turn_started', 't1', { text: 'build it' }),
      event('tool_call', 't1', { id: 'c1', tool: 'author_tool', args_summary: 'mode=build' }),
      event('tool_result', 't1', { id: 'c1', tool: 'author_tool', ok: false, summary: 'error: tool error: Protected tool authoring could not complete.' }),
    ])
    const chip = document.querySelector('.converse-tool-chip[data-state="failed"]')
    expect(chip).toBeTruthy()
    expect(chip.textContent).toContain('Protected tool authoring could not complete.')
    expect(chip.textContent).not.toMatch(/error:/i)
  })
})
