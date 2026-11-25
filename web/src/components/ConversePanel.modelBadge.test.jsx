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

const ts = '2026-11-20T08:00:00.000Z'
const event = (type, turn_id, data = {}) => ({ type, turn_id, data, ts })
const turn = (id, usage) => [
  event('turn_started', id, { text: `ask ${id}` }),
  event('text_delta', id, { text: `answer ${id}` }),
  event('turn_usage', id, usage),
  event('turn_complete', id, { stop_reason: 'end_turn' }),
]
const setup = async (events) => {
  render(<ConversePanel sessionId="session" onDismiss={vi.fn()} userTurns={[]} />)
  await act(async () => {
    const handlers = converse.openStream.mock.calls.at(-1)[2]
    events.forEach(handlers.onEvent)
  })
}
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('per-turn model badge', () => {
  it('names the model and where it ran, from each turn’s own usage', async () => {
    await setup([
      ...turn('local', { models: ['qwen3.8-27b'], provider: 'openai_compatible', provider_label: 'local on Mac mini', cost_tokens: 40 }),
      ...turn('claude', { models: ['claude-sonnet-5-5'], cost_tokens: 90 }),
    ])
    const badges = screen.getAllByTestId('converse-model-badge')
    expect(badges.map((b) => b.textContent)).toEqual(['Qwen3.8-27B · local on Mac mini', 'Claude Sonnet 5.5'])
    expect(badges[0].className).toContain('local')
    expect(badges[1].className).not.toContain('local')
  })

  it('shows no badge for a turn that reported no model', async () => {
    await setup(turn('bare', { cost_tokens: 12 }))
    expect(screen.queryByTestId('converse-model-badge')).toBeNull()
  })
})
