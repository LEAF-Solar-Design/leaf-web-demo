import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

let APPROVALS = []
let streamHandlers = null
let deferred = { promise: Promise.resolve() }

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))
vi.mock('../converse.js', () => ({
  openStream: vi.fn((sessionId, afterSeq, handlers) => {
    streamHandlers = handlers
    return { close: vi.fn() }
  }),
  postMessage: vi.fn(),
  resolveApproval: vi.fn(async (id, sid, ok, recorded, hooks) => {
    await deferred.promise
    const allowed = hooks?.beforeResume ? hooks.beforeResume() : true
    return allowed ? { turn_id: 'turn-2', status: 'started' } : { held: true, recorded: true }
  }),
  cancelTurn: vi.fn(),
  classifyAgentError: vi.fn(() => 'unreachable'),
  listPendingApprovals: vi.fn(() => Promise.resolve(APPROVALS)),
}))

import { listPendingApprovals, resolveApproval } from '../converse.js'
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
  streamHandlers = null
  deferred = { promise: Promise.resolve() }
})

const setup = async (props = {}) => {
  render(<ConversePanel sessionId="session-1" onDismiss={vi.fn()} {...props} />)
  await screen.findByText(/delete-marked-panel/)
}

const setupStream = async (props = {}) => {
  render(<ConversePanel sessionId="session-1" onDismiss={vi.fn()} {...props} />)
  act(() => {
    streamHandlers.onEvent({
      type: 'proposed_run', turn_id: 't1', seq: 1,
      data: {
        confirmation_id: 'c9', tool: 'delete-marked-panel', params: {},
        capability: 'drawing.write', rationale: 'remove the marked panel',
      },
    })
    streamHandlers.onEvent({
      type: 'turn_complete', turn_id: 't1', seq: 2,
      data: { stop_reason: 'end_turn' },
    })
  })
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
    expect(resolveApproval).toHaveBeenCalledWith('c1', 'session-1', true, false, {
      beforeResume: expect.any(Function),
    })
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

  it("holds the transcript's own proposal card the same way", async () => {
    APPROVALS = []
    await setupStream({ engineDirty: true })

    const approve = screen.getByRole('button', { name: 'Unsaved browser edits' })
    expect(approve).toBeDisabled()
    expect(approve).toHaveAttribute('title', REASONS.unsavedEngineEdits)
    fireEvent.click(approve)
    expect(resolveApproval).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Deny' })).toBeEnabled()
  })

  it('approves a stream-delivered write once when the browser engine is clean', async () => {
    APPROVALS = []
    await setupStream({ engineDirty: false })

    const approve = screen.getByRole('button', { name: 'Approve' })
    expect(approve).toBeEnabled()
    fireEvent.click(approve)
    await waitFor(() => expect(resolveApproval).toHaveBeenCalledTimes(1))
    expect(resolveApproval).toHaveBeenCalledWith('c9', 'session-1', true, false, {
      beforeResume: expect.any(Function),
    })
  })

  it('names a held approved resume request with the held label', async () => {
    APPROVALS = [{ ...WRITE_APPROVAL, resume_required: true, approved: true }]
    await setup({ engineDirty: true })

    const resume = screen.getByRole('button', { name: 'Unsaved browser edits' })
    expect(resume).toBeDisabled()
    expect(resume).toHaveAttribute('title', REASONS.unsavedEngineEdits)
    fireEvent.click(resume)
    expect(resolveApproval).not.toHaveBeenCalled()
  })

  it('an edit that lands during the approval record holds the resume', async () => {
    let finishApproval
    deferred = { promise: new Promise((resolve) => { finishApproval = resolve }) }
    APPROVALS = [WRITE_APPROVAL]
    const onDismiss = vi.fn()
    const { rerender } = render(
      <ConversePanel sessionId="session-1" onDismiss={onDismiss} engineDirty={false} />,
    )
    await screen.findByText(/delete-marked-panel/)
    expect(listPendingApprovals).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(resolveApproval).toHaveBeenCalledTimes(1)
    const resolution = resolveApproval.mock.results[0].value
    rerender(<ConversePanel sessionId="session-1" onDismiss={onDismiss} engineDirty />)
    APPROVALS = [{ ...WRITE_APPROVAL, resume_required: true, approved: true }]
    await act(async () => {
      finishApproval()
      await resolution
    })

    await expect(resolution).resolves.toEqual({ held: true, recorded: true })
    await waitFor(() => expect(listPendingApprovals).toHaveBeenCalledTimes(2))
    const resume = screen.getByRole('button', { name: 'Unsaved browser edits' })
    expect(resume).toBeDisabled()
    expect(resume).toHaveAttribute('title', REASONS.unsavedEngineEdits)
    expect(screen.queryByRole('button', { name: 'Deny' })).not.toBeInTheDocument()
    expect(screen.queryByText('thinking…')).not.toBeInTheDocument()
  })
})
