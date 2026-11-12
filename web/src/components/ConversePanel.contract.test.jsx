import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

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
const setup = async (events, userTurns = []) => {
  const view = render(<ConversePanel sessionId="session" onDismiss={vi.fn()} userTurns={userTurns} />)
  await push(...events)
  return view
}
const expectInk = (card, state) => {
  expect(card).toHaveAttribute('data-ink', state)
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ConversePanel transcript markup contract', () => {
  it('marks transcript roles, tool chips, question and approval states, and errors', async () => {
    await setup([
      event('turn_started', 'broken'),
      event('error', 'broken', { error: { error_code: 'BROKER_UNREACHABLE' }, stop_reason: 'error' }),
      event('turn_started', 'main', { text: 'Inspect the roof.' }),
      event('tool_call', 'main', { call_id: 'count', tool: 'count_panels', args_summary: 'roof', args: { layer: 'roof' } }),
      event('tool_result', 'main', { call_id: 'count', tool: 'count_panels', ok: true, summary: '42 panels' }),
      event('tool_call', 'main', { call_id: 'measure', tool: 'measure_roof', args_summary: 'north face' }),
      event('text_delta', 'main', { text: 'I found the roof panels.' }),
      event('question_required', 'main', { question_id: 'roof-choice', question: 'Which roof?', options: [{ label: 'North roof' }] }),
      event('confirmation_required', 'main', { confirmation_id: 'roof-confirm', kind: 'Roof update' }),
      event('turn_complete', 'main', { stop_reason: 'end_turn' }),
    ])

    const user = document.querySelector('.converse-msg.user')
    expect(user).toHaveAttribute('data-converse-role', 'user')
    expect(user.textContent).toBe('Inspect the roof.')
    const assistant = screen.getByText('I found the roof panels.').closest('.converse-msg')
    expect(assistant).toHaveAttribute('data-converse-role', 'assistant')
    expect(assistant.textContent).toBe('I found the roof panels.')

    const rows = screen.getAllByRole('list', { name: 'Tools used' })
    expect(rows).toHaveLength(1)
    expect(rows[0].tagName).toBe('UL')
    expect(rows[0]).toHaveClass('converse-tool-row')
    expect(rows[0]).toHaveAttribute('data-converse-role', 'tool')
    const chips = within(rows[0]).getAllByRole('listitem')
    expect(chips).toHaveLength(2)
    chips.forEach((chip) => expect(chip).toHaveClass('converse-tool-chip', 'converse-step'))
    expect(chips[0]).toHaveAttribute('data-state', 'done')
    expect(chips[0]).toHaveTextContent('Done')
    expect(chips[0]).toHaveTextContent('42 panels')
    expect(chips[1]).toHaveAttribute('data-state', 'running')
    expect(chips[1]).toHaveTextContent('Running')
    const toggle = within(chips[0]).getByRole('button')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(chips[0].querySelector('pre')).toHaveTextContent('roof')

    const question = screen.getByText('Which roof?').closest('.converse-question')
    expect(question).toHaveAttribute('data-converse-role', 'question')
    expectInk(question, 'question')
    expect(question.textContent).toBe('Which roof?North roof')
    const decision = screen.getByRole('button', { name: 'Approve' }).closest('.converse-confirm')
    expect(decision).toHaveAttribute('data-converse-role', 'decision')
    expectInk(decision, 'needs-ok')
    expect(decision.textContent).toBe('Roof update. The assistant is asking before it proceeds.ApproveDeny')
    const error = document.querySelector('[data-converse-role="error"]')
    expect(error).toHaveClass('converse-note')
    expect(error).toHaveAttribute('role', 'alert')
    expect(screen.getByRole('alert')).toBe(error)

    converse.postMessage.mockResolvedValueOnce({ turn_id: 'answer', status: 'started' })
    fireEvent.click(screen.getByRole('button', { name: 'North roof' }))
    await waitFor(() => expect(converse.postMessage).toHaveBeenCalledWith('session', { text: 'North roof', allowSecretOnce: false }))
    await push(
      event('turn_started', 'answer', { text: 'North roof' }),
      event('turn_complete', 'answer', { stop_reason: 'end_turn' }),
    )
    expectInk(question, 'answered')
    expect(screen.getByRole('button', { name: 'North roof selected' })).toBeDisabled()

    converse.resolveApproval.mockResolvedValueOnce({ turn_id: 'approved-resume' })
    fireEvent.click(within(decision).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expectInk(decision, 'approved'))
    expect(decision.textContent).toBe('Roof update. The assistant is asking before it proceeds.Approved')
    expect(converse.resolveApproval).toHaveBeenCalledWith('roof-confirm', 'session', true, false, expect.any(Object))
    expect(within(decision).queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  it('marks failed chips and denied proposals and separates tool runs around assistant text', async () => {
    await setup([
      event('turn_started', 'main'),
      event('tool_call', 'main', { tool: 'read_layers' }),
      event('tool_result', 'main', { tool: 'read_layers', ok: false, summary: 'Drawing unreadable' }),
      event('text_delta', 'main', { text: 'Trying another tool.' }),
      event('tool_call', 'main', { tool: 'count_panels' }),
      event('proposed_run', 'main', { confirmation_id: 'proposal', tool: 'move_panel', capability: 'drawing.write' }),
      event('turn_complete', 'main', { stop_reason: 'end_turn' }),
    ])
    const rows = screen.getAllByRole('list', { name: 'Tools used' })
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByRole('listitem')).toHaveAttribute('data-state', 'failed')
    expect(within(rows[1]).getByRole('listitem')).toHaveAttribute('data-state', 'running')
    const decision = screen.getByRole('button', { name: 'Deny' }).closest('.converse-confirm')
    expect(decision).toHaveAttribute('data-converse-role', 'decision')
    expectInk(decision, 'needs-ok')
    converse.resolveApproval.mockResolvedValueOnce({ turn_id: 'denied-resume' })
    fireEvent.click(within(decision).getByRole('button', { name: 'Deny' }))
    await waitFor(() => expectInk(decision, 'denied'))
    expect(within(decision).getByText('Denied')).toBeTruthy()
    expect(converse.resolveApproval).toHaveBeenCalledWith('proposal', 'session', false, false, expect.any(Object))
  })

  it('gives a pending user prompt the same role before stream replay', async () => {
    await setup([], [{ turnId: 'pending', text: 'Check this drawing.' }])
    const user = document.querySelector('.converse-msg.user')
    expect(user).toHaveAttribute('data-converse-role', 'user')
    expect(user.textContent).toBe('Check this drawing.')
  })
})
