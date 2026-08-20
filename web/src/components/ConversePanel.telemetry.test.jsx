/**
 * card TEL-2b: the conversation panel's five user actions (open, send,
 * recover, truncate/clear, delete) had ZERO track() calls before this file.
 * Each spec pins the exact event name a mutation could silently drop or
 * rename — `../telemetry.js` and `../converse.js` are mocked so these specs
 * exercise ONLY the panel's own choke points, never the transport underneath.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))
vi.mock('../converse.js', () => ({
  openStream: vi.fn(() => ({ close: vi.fn() })),
  postMessage: vi.fn(),
  resolveApproval: vi.fn(),
  listPendingApprovals: vi.fn(() => Promise.resolve([])),
  cancelTurn: vi.fn(() => Promise.resolve({ turn_id: 'turn-1', status: 'cancelled' })),
  classifyAgentError: vi.fn(() => 'unreachable'),
}))

import { track } from '../telemetry.js'
import * as converse from '../converse.js'
import ConversePanel from './ConversePanel.jsx'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const setup = (props = {}) => render(
  <ConversePanel sessionId="session-1" onDismiss={vi.fn()} {...props} />,
)

describe('conversation.opened', () => {
  it('fires when the panel opens a session stream', () => {
    setup()
    expect(converse.openStream).toHaveBeenCalledWith('session-1', 0, expect.any(Object))
    expect(track).toHaveBeenCalledWith('conversation.opened')
  })
})

describe('conversation.message_sent', () => {
  it('fires once a typed message is accepted by the server', async () => {
    converse.postMessage.mockResolvedValueOnce({ turn_id: 'turn-1', status: 'started' })
    setup()

    const input = screen.getByRole('textbox', { name: /reply to the assistant/i })
    fireEvent.change(input, { target: { value: 'count panels per layer' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => expect(converse.postMessage).toHaveBeenCalled())
    expect(track).toHaveBeenCalledWith('conversation.message_sent', {
      input_kind: 'typed', text_len: 'count panels per layer'.length,
    })
  })
})

describe('conversation.recovered', () => {
  it('fires when a busy 409 is recovered via the queued retry, never a raw error', async () => {
    converse.classifyAgentError.mockReturnValue('busy')
    const busyErr = new Error('turn in progress')
    converse.postMessage
      .mockRejectedValueOnce(busyErr)
      .mockResolvedValueOnce({ status: 'queued', queued_id: 'q-1' })
    setup()

    const input = screen.getByRole('textbox', { name: /reply to the assistant/i })
    fireEvent.change(input, { target: { value: 'measure panel area' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => expect(converse.postMessage).toHaveBeenCalledTimes(2))
    expect(track).toHaveBeenCalledWith('conversation.recovered', { reason: 'busy_retry_queued' })
    // The recovered send must not also surface as a user-visible failure.
    expect(screen.queryByText(/couldn.t reach the assistant/i)).not.toBeInTheDocument()
  })
})

describe('conversation.truncated', () => {
  it('fires when Stop interrupts the in-flight turn', async () => {
    converse.postMessage.mockResolvedValueOnce({ turn_id: 'turn-1', status: 'started' })
    setup()

    const input = screen.getByRole('textbox', { name: /reply to the assistant/i })
    fireEvent.change(input, { target: { value: 'highlight panels near the edge' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    const stopButton = await screen.findByRole('button', { name: /^stop$/i })
    await act(async () => { fireEvent.click(stopButton) })

    expect(converse.cancelTurn).toHaveBeenCalledWith('session-1', 'turn-1')
    expect(track).toHaveBeenCalledWith('conversation.truncated', { reason: 'user_stop' })
  })
})

describe('conversation.deleted', () => {
  it('fires when Hide dismisses the panel, and still calls onDismiss', () => {
    const onDismiss = vi.fn()
    setup({ onDismiss })

    fireEvent.click(screen.getByRole('button', { name: /^hide$/i }))

    expect(track).toHaveBeenCalledWith('conversation.deleted', { reason: 'user_hide' })
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })
})
