import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

let APPROVALS = []

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))
vi.mock('../converse.js', () => ({
  openStream: vi.fn(() => ({ close: vi.fn() })),
  postMessage: vi.fn(),
  resolveApproval: vi.fn(async () => ({ turn_id: 'turn-2', status: 'started' })),
  cancelTurn: vi.fn(),
  classifyAgentError: vi.fn(() => 'unreachable'),
  listPendingApprovals: vi.fn(() => Promise.resolve(APPROVALS)),
}))

import { resolveApproval } from '../converse.js'
import { REASONS } from '../lib/actionRegistry.js'
import ConversePanel from './ConversePanel.jsx'

const WRITE_APPROVAL = {
  confirmation_id: 'c1',
  session_id: 'session-1',
  tool: 'delete-marked-panel',
  capability: 'drawing.write',
  params: {},
  rationale: 'remove the marked panel',
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  APPROVALS = []
})

const setup = async (props = {}) => {
  render(<ConversePanel sessionId="session-1" onDismiss={vi.fn()} {...props} />)
  await screen.findByText(/delete-marked-panel/)
}

describe('assistant write approvals honour the one-head rule', () => {
  it('holds a write with the unsaved-edits sentence and leaves Deny enabled', async () => {
    APPROVALS = [WRITE_APPROVAL]
    await setup({ engineDirty: true })

    const approve = screen.getByRole('button', { name: 'Unsaved browser edits' })
    expect(approve).toBeDisabled()
    expect(approve).toHaveAttribute('title', REASONS.unsavedEngineEdits)
    fireEvent.click(approve)
    expect(resolveApproval).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Deny' })).toBeEnabled()
  })

  it('approves and resolves a write when the browser engine is clean', async () => {
    APPROVALS = [WRITE_APPROVAL]
    await setup({ engineDirty: false })

    const approve = screen.getByRole('button', { name: 'Approve' })
    expect(approve).toBeEnabled()
    fireEvent.click(approve)
    await waitFor(() => expect(resolveApproval).toHaveBeenCalledTimes(1))
    expect(resolveApproval).toHaveBeenCalledWith('c1', 'session-1', true, false)
  })

  it('never holds a read approval for unsaved browser edits', async () => {
    APPROVALS = [{ ...WRITE_APPROVAL, confirmation_id: 'c2', capability: 'drawing.read' }]
    await setup({ engineDirty: true })

    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled()
  })

  it('names the checkout lock first when browser edits are also unsaved', async () => {
    APPROVALS = [WRITE_APPROVAL]
    await setup({ engineDirty: true, writeLocked: true })

    expect(screen.getByRole('button', { name: 'Editing locked' })).toBeDisabled()
  })
})
