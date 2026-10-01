import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))
vi.mock('../converse.js', () => ({
  openStream: vi.fn(() => ({ close: vi.fn() })),
  postMessage: vi.fn(),
  resolveApproval: vi.fn(),
  listPendingApprovals: vi.fn(() => Promise.resolve([])),
  cancelTurn: vi.fn(),
  classifyAgentError: vi.fn(() => 'unreachable'),
}))
vi.mock('../engineChanges.js', async () => ({
  ...await vi.importActual('../engineChanges.js'),
  listEngineChanges: vi.fn(),
  getEngineChange: vi.fn(),
  markEngineChangeRead: vi.fn(),
}))

import { postMessage } from '../converse.js'
import { engineChangeDiscussText, getEngineChange, listEngineChanges, markEngineChangeRead } from '../engineChanges.js'
import ConversePanel from './ConversePanel.jsx'

const card = {
  card_id: 'change-1', title: 'Restore panel selection focus', state: 'accepted',
  feature_id: 'panel-selection', created_at: '2026-10-01T12:00:00Z', unread: true,
  summary: 'The accepted fix restores focus to the selected row.',
  change: { pr_number: 123, head_sha: 'abcdef1234567890' },
  evidence: { regression_spec: 'web/e2e/selection.spec.mjs' },
}

beforeEach(() => {
  listEngineChanges.mockResolvedValue({ kind: 'ok', cards: [card], unread_count: 1, next_cursor: null })
  getEngineChange.mockResolvedValue({ kind: 'ok', card })
  markEngineChangeRead.mockResolvedValue({ kind: 'ok', card: { ...card, unread: false } })
  postMessage.mockResolvedValue({ turn_id: 'discussion-1', status: 'started' })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('Discuss posts only the card seed and preserves an unsent text draft without attachments', async () => {
  render(<ConversePanel sessionId="session-1" />)
  const input = screen.getByRole('textbox', { name: 'Reply to the assistant' })
  const draft = '  Keep my unsent question about the roof layout.  '
  fireEvent.change(input, { target: { value: draft } })
  fireEvent.click(await screen.findByRole('tab', { name: 'Engine changes, 1 unread' }))
  fireEvent.click(await screen.findByRole('button', { name: /Restore panel selection focus/ }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Discuss', exact: true })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Discuss', exact: true }))

  await waitFor(() => expect(screen.getByRole('log')).toHaveTextContent(card.title))
  expect(postMessage).toHaveBeenCalledTimes(1)
  expect(postMessage).toHaveBeenCalledWith('session-1', {
    text: engineChangeDiscussText(card), allowSecretOnce: false,
  })
  expect(input).toHaveValue(draft)
  expect(screen.getByRole('tab', { name: 'Conversation', exact: true })).toHaveAttribute('aria-selected', 'true')
  expect(screen.queryByRole('button', { name: 'Remove image attachment' })).toBeNull()
})
