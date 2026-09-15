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
const quota = (overrides = {}) => ({
  ...event('error', 'resume', { error: {
    error_code: 'llm_quota_exhausted', retry_after_s: 90.25,
  } }), ...overrides,
})
const prior = [
  event('turn_started', 'original'),
  event('confirmation_required', 'original', { confirmation_id: 'approved-once' }),
  event('confirmation_resolved', 'original', { confirmation_id: 'approved-once', approved: true }),
  event('turn_complete', 'original', { stop_reason: 'end_turn' }),
  event('turn_started', 'resume'),
]
const setup = async (events) => {
  const view = render(<ConversePanel sessionId="session" onDismiss={vi.fn()}
    userTurns={[{ turnId: 'resume', text: 'Continue the approved authoring request' }]} />)
  await act(async () => {
    const handlers = converse.openStream.mock.calls.at(-1)[2]
    events.forEach(handlers.onEvent)
  })
  return view
}
const assertNoMutations = () => {
  expect(converse.postMessage).not.toHaveBeenCalled()
  expect(converse.resolveApproval).not.toHaveBeenCalled()
  expect(converse.cancelTurn).not.toHaveBeenCalled()
}
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('terminal quota transcript recovery', () => {
  it('finishes only the resumed turn without turn_complete or replaying approval', async () => {
    await setup([...prior, quota()])
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).disabled).toBe(false)
    expect(screen.queryByRole('button', { name: /^Stop$/ })).toBeNull()
    expect(screen.getByText('Approved')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(screen.getByText(/retry interval of 90.25 seconds/)).toBeTruthy()
    expect(screen.getByText('2026-09-12T12:01:30.250Z').getAttribute('datetime'))
      .toBe('2026-09-12T12:01:30.250Z')
    assertNoMutations()
  })

  it('anchors retry timing to the durable event across later remounts', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-09-12T12:00:30Z'))
    const view = await setup([...prior, quota()])
    view.unmount()
    vi.setSystemTime(new Date('2026-09-13T18:00:00Z'))
    await setup([...prior, quota()])
    expect(screen.getByText('2026-09-12T12:01:30.250Z')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).disabled).toBe(false)
    assertNoMutations()
  })

  it('keeps an unrelated active turn busy', async () => {
    await setup([...prior, event('turn_started', 'other'), quota()])
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy()
    assertNoMutations()
  })

  it('does not invent an event timestamp when only the interval is present', async () => {
    await setup([...prior, quota({ ts: undefined })])
    expect(screen.getByText(/event timestamp is unavailable/)).toBeTruthy()
    expect(document.querySelector('time')).toBeNull()
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).disabled).toBe(false)
    assertNoMutations()
  })

  it('does not treat another error as terminal quota or clear another turn', async () => {
    await setup([...prior, event('error', 'resume', { error: {
      error_code: 'temporary_error', message: 'Still awaiting a terminal event',
    } })])
    expect(screen.getByRole('textbox', { name: 'Reply to the assistant' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy()
    assertNoMutations()
  })
})
