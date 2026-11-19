import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'

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

const ts = '2026-09-12T12:00:00.000Z'
const event = (type, turn_id, data = {}) => ({ type, turn_id, data, ts })
const error = {
  error_code: 'BROKER_UNREACHABLE', message: 'harness returned HTTP 500',
}
const broken = [
  event('turn_started', 'broken'),
  event('error', 'broken', { error, stop_reason: 'error' }),
]
const setup = async (events, userTurns) => {
  const view = render(<ConversePanel sessionId="session" onDismiss={vi.fn()}
    userTurns={userTurns} />)
  await act(async () => {
    const handlers = converse.openStream.mock.calls.at(-1)[2]
    events.forEach(handlers.onEvent)
  })
  return view
}
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('terminal error transcript recovery', () => {
  it.each([
    ['oauth_org_not_allowed', "The assistant's Claude account was refused, so it could not answer. Your tools still work. An admin must link an allowed Claude account."],
    ['authentication_failed', "The assistant's Claude account could not sign in, so it could not answer. Your tools still work. An admin must relink the Claude account."],
    ['billing_error', "The assistant's Claude account has a billing problem, so it could not answer. Your tools still work. An admin must check that account's plan."],
  ])('explains %s and shows the idle reconnection banner after the turn ends', async (code, message) => {
    await setup([
      event('turn_started', 'broken'),
      event('error', 'broken', {
        error: { error_code: code, message: 'Agent service terminal failure', retryable: false },
      }),
    ], [{ turnId: 'broken', text: 'hello' }])
    expect(screen.getByText(message)).toBeTruthy()
    expect(screen.getByText(code)).toBeTruthy()
    expect(screen.queryByText('Agent service terminal failure')).toBeNull()
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).readOnly).toBe(false)

    act(() => converse.openStream.mock.calls.at(-1)[2].onConnectionState('retrying'))
    expect(screen.getByText('The live connection to the assistant dropped. Reconnecting.')).toBeTruthy()
    expect(screen.queryByText(/You can stop the current reply/)).toBeNull()
  })

  it('shows the option to stop a running reply while reconnecting', async () => {
    await setup([event('turn_started', 'live')], [{ turnId: 'live', text: 'hello' }])
    act(() => converse.openStream.mock.calls.at(-1)[2].onConnectionState('retrying'))
    expect(screen.getByText('The conversation connection was interrupted. Retrying. You can stop the current reply.')).toBeTruthy()
  })

  it('unlocks replies and question choices after a later completed turn', async () => {
    await setup([
      ...broken,
      event('turn_started', 'next'),
      event('question_required', 'next', {
        question_id: 'q1', question: 'Is it on?',
        options: [{ label: 'Done' }, { label: 'Not yet' }],
      }),
      event('turn_complete', 'next', { stop_reason: 'end_turn' }),
    ], [{ turnId: 'next', text: 'hello' }])
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).readOnly).toBe(false)
    expect(screen.getByRole('button', { name: 'Done' }).disabled).toBe(false)
  })

  it('finishes the errored turn without turn_complete', async () => {
    await setup(broken, [{ turnId: 'broken', text: 'hello' }])
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).readOnly).toBe(false)
    expect(screen.queryByRole('button', { name: /^Stop$/ })).toBeNull()
  })

  it('keeps an error without stop_reason active', async () => {
    await setup([
      event('turn_started', 'broken'),
      event('error', 'broken', { error }),
    ], [{ turnId: 'broken', text: 'hello' }])
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).readOnly).toBe(true)
  })

  it('unlocks replies after a non-retryable error without stop_reason', async () => {
    await setup([
      event('turn_started', 'broken'),
      event('error', 'broken', {
        error: { error_code: 'internal', message: 'Agent service failed', retryable: false },
      }),
    ], [{ turnId: 'broken', text: 'hello' }])
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).readOnly).toBe(false)
    expect(screen.queryByRole('button', { name: /^Stop$/ })).toBeNull()
  })

  it('supersedes an older retryable error when a new turn starts', async () => {
    await setup([
      event('turn_started', 'old'),
      event('error', 'old', { error: { ...error, retryable: true } }),
      event('turn_started', 'new'),
      event('turn_complete', 'new', { stop_reason: 'end_turn' }),
    ], [{ turnId: 'old', text: 'hello' }, { turnId: 'new', text: 'try again' }])
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).readOnly).toBe(false)
  })

  it('keeps the latest turn active after a retryable error without stop_reason', async () => {
    await setup([
      event('turn_started', 'live'),
      event('error', 'live', { error: { ...error, retryable: true } }),
    ], [{ turnId: 'live', text: 'hello' }])
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).readOnly).toBe(true)
  })
})
