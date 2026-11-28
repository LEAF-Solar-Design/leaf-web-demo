// @vitest-environment jsdom
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
import { BUILD_TOOL_STARTER } from '../lib/starterPrompts.js'
import ConversePanel from './ConversePanel.jsx'

const event = (type, data = {}, turn_id = 'first') => ({ type, turn_id, data, ts: '2026-11-27T12:00:00Z' })
const prompt = 'Please build a fake sample tool'
const billingMessage = 'Your Anthropic API key has no credit. Add credit in Claude Console, then resend.'
const composer = () => screen.getByRole('textbox', { name: 'Reply to the assistant' })
const emit = async (events) => {
  await act(async () => {
    const handlers = converse.openStream.mock.calls.at(-1)[2]
    events.forEach(handlers.onEvent)
  })
}
const mount = () => render(<ConversePanel sessionId="session" onDismiss={vi.fn()} onLinkClaude={vi.fn()} />)
const refuseBilling = async (text = prompt) => {
  mount()
  await emit([
    event('turn_started', { text }),
    event('error', { error: { error_code: 'billing_error', message: 'Agent service terminal failure', retryable: false }, stop_reason: 'error' }),
  ])
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('first conversation recovery and starter', () => {
  it('explains missing credit and restores the message without sending it', async () => {
    await refuseBilling()
    expect(screen.getByText(billingMessage).closest('[role="status"]')).toBeInTheDocument()
    const billing = screen.getByRole('link', { name: 'Add credit in Claude Console' })
    expect(billing).toHaveAttribute('href', 'https://platform.claude.com/settings/billing')
    expect(billing).toHaveAttribute('target', '_blank')
    expect(billing).toHaveAttribute('rel', 'noreferrer')
    expect(screen.queryByRole('button', { name: 'Link account' })).not.toBeInTheDocument()
    expect(screen.queryByText('Agent service terminal failure')).not.toBeInTheDocument()
    expect(screen.queryByText('The assistant could not finish this reply. Try again.')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Use my message again' }))
    expect(composer()).toHaveValue(prompt)
    expect(composer()).toHaveFocus()
    expect(converse.postMessage).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Use my message again' })).not.toBeInTheDocument()
    await emit([event('turn_started', { text: 'A new request' }, 'next')])
    expect(screen.queryByText(billingMessage)).not.toBeInTheDocument()
  })

  it('keeps an existing draft and does not offer an absent refused prompt', async () => {
    await refuseBilling('')
    expect(screen.queryByRole('button', { name: 'Use my message again' })).not.toBeInTheDocument()
    await emit([
      event('turn_started', { text: prompt }, 'next'),
      event('error', { error: { error_code: 'billing_error', retryable: false }, stop_reason: 'error' }, 'next'),
    ])
    fireEvent.change(composer(), { target: { value: 'My draft' } })
    expect(screen.queryByRole('button', { name: 'Use my message again' })).not.toBeInTheDocument()
    expect(composer()).toHaveValue('My draft')
  })

  it('fills and selects the tool name without posting a message', () => {
    mount()
    const starter = screen.getByTestId('converse-starter')
    expect(starter).toHaveTextContent('Build an AutoCAD tool')
    fireEvent.click(starter)
    expect(composer()).toHaveValue(BUILD_TOOL_STARTER.text)
    expect(composer()).toHaveFocus()
    expect(composer().selectionStart).toBe(23)
    expect(composer().selectionEnd).toBe(27)
    expect(converse.postMessage).not.toHaveBeenCalled()
    expect(screen.queryByTestId('converse-starter')).not.toBeInTheDocument()
    fireEvent.change(composer(), { target: { value: '' } })
    expect(screen.getByTestId('converse-starter')).toBeInTheDocument()
    fireEvent.change(composer(), { target: { value: 'My own request' } })
    expect(screen.queryByTestId('converse-starter')).not.toBeInTheDocument()
  })
})
