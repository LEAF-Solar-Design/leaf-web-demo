import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))
vi.mock('../converse.js', () => ({
  openStream: vi.fn(() => ({ close: vi.fn() })),
  postMessage: vi.fn(),
  resolveApproval: vi.fn(),
  listPendingApprovals: vi.fn(() => Promise.resolve([])),
  cancelTurn: vi.fn(),
  classifyAgentError: vi.fn(() => 'grant'),
}))

import * as converse from '../converse.js'
import ConversePanel from './ConversePanel.jsx'

const event = (type, data = {}, turn_id = 'refused') => ({ type, turn_id, data, ts: '2026-11-25T12:00:00Z' })
const prompt = 'Please build a fake sample tool'
const emit = async (events) => {
  await act(async () => {
    const handlers = converse.openStream.mock.calls.at(-1)[2]
    events.forEach(handlers.onEvent)
  })
}
const setup = async (code, text = prompt, terminal = { stop_reason: 'error' }) => {
  const onLinkClaude = vi.fn()
  render(<ConversePanel sessionId="session" onDismiss={vi.fn()} onLinkClaude={onLinkClaude} />)
  await emit([
    event('turn_started', { text }),
    event('error', { ...terminal, error: { error_code: code, message: 'Internal provider detail', ...terminal.error } }),
  ])
  return onLinkClaude
}
afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('refused Claude grant recovery', () => {
  it.each([
    ['authentication_failed', 'Claude no longer accepts the linked account. Link it again, then resend your message.'],
    ['oauth_org_not_allowed', "The linked account's organization does not allow it here. Link a different account, then resend your message."],
  ])('keeps the prompt available after %s and never sends automatically', async (code, message) => {
    const onLinkClaude = await setup(code)
    expect(screen.getByText(message).closest('[role="status"]')).toBeInTheDocument()
    expect(screen.queryByText('Internal provider detail')).not.toBeInTheDocument()
    expect(screen.queryByText('The assistant could not finish this reply. Try again.')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Link account' }))
    expect(onLinkClaude).toHaveBeenCalledTimes(1)
    const composer = screen.getByRole('textbox', { name: 'Reply to the assistant' })
    expect(composer.readOnly).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Use my message again' }))
    expect(composer).toHaveValue(prompt)
    expect(composer).toHaveFocus()
    expect(converse.postMessage).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Use my message again' })).not.toBeInTheDocument()
  })

  it('does not overwrite an existing composer draft', async () => {
    await setup('authentication_failed')
    fireEvent.change(screen.getByRole('textbox', { name: 'Reply to the assistant' }), { target: { value: 'Another message' } })
    expect(screen.queryByRole('button', { name: 'Use my message again' })).not.toBeInTheDocument()
  })

  it('does not offer an absent prompt and respects a retryable active turn', async () => {
    await setup('authentication_failed', '', { error: { retryable: true } })
    expect(screen.queryByRole('button', { name: 'Use my message again' })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).readOnly).toBe(true)
  })

  it('honors non-retryable terminal handling and clears the banner on a new turn', async () => {
    await setup('authentication_failed', prompt, { error: { retryable: false } })
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).readOnly).toBe(false)
    await emit([event('turn_started', { text: 'New turn' }, 'new'), event('turn_complete', { stop_reason: 'end_turn' }, 'new')])
    expect(screen.queryByText(/Claude no longer accepts the linked account/)).not.toBeInTheDocument()
  })

  it('keeps the composer text when a send fails before start with a grant error', async () => {
    converse.postMessage.mockRejectedValueOnce(new Error('Grant required'))
    render(<ConversePanel sessionId="session" onDismiss={vi.fn()} onLinkClaude={vi.fn()} />)
    const composer = screen.getByRole('textbox', { name: 'Reply to the assistant' })
    fireEvent.change(composer, { target: { value: prompt } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Send$/ })) })
    expect(composer).toHaveValue(prompt)
    expect(screen.getByText('Chat needs a linked Claude account.')).toBeInTheDocument()
  })
})
