import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

let APPROVALS = []
let streamHandlers = null
let deferred = { promise: Promise.resolve() }
let useRealResolution = false

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))
vi.mock('../api.js', () => ({ config: { apiBase: '', tenant: 'demo' }, authHeaders: () => ({}), noteUnauthorized: (res) => res }))
vi.mock('../converse.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
  openStream: vi.fn((sessionId, afterSeq, handlers) => {
    streamHandlers = handlers
    return { close: vi.fn() }
  }),
  postMessage: vi.fn(),
  resolveApproval: vi.fn(async (id, sid, ok, recorded, hooks) => {
    if (useRealResolution) return actual.resolveApproval(id, sid, ok, recorded, hooks)
    await deferred.promise
    const allowed = hooks?.beforeResume ? await hooks.beforeResume() : true
    return allowed ? { turn_id: 'turn-2', status: 'started' } : { held: true, recorded: true }
  }),
  cancelTurn: vi.fn(),
  classifyAgentError: vi.fn(() => 'unreachable'),
  listPendingApprovals: vi.fn(() => Promise.resolve(APPROVALS)),
  }
})

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
  useRealResolution = false
  vi.unstubAllGlobals()
})

describe('canonical project approval custody using the actual resume transport', () => {
  const projectContext = { project_id: 'project-1', input_version_id: 'version-1' }
  const start = async (prepareApprovedProjectRun, props = {}, decision = 'Approve', approval = WRITE_APPROVAL) => {
    useRealResolution = true
    APPROVALS = [approval]
    const calls = []
    vi.stubGlobal('fetch', vi.fn(async (path, options) => {
      calls.push({ path, body: JSON.parse(options.body) })
      return { ok: true, status: 200, json: async () => ({ turn_id: 'resumed', status: 'started' }) }
    }))
    const view = render(<ConversePanel sessionId="session-1" projectContext={projectContext}
      prepareApprovedProjectRun={prepareApprovedProjectRun} {...props} />)
    await screen.findByText(approval.tool)
    fireEvent.click(screen.getByRole('button', { name: decision }))
    return { ...view, calls }
  }

  it('records the decision, waits for custody, then sends exactly one resume', async () => {
    let issue
    const custody = new Promise((resolve) => { issue = resolve })
    const prepare = vi.fn(() => custody)
    const { calls } = await start(prepare)
    await waitFor(() => expect(prepare).toHaveBeenCalledTimes(1))
    expect(calls).toHaveLength(1)
    expect(calls[0].body).toEqual({ approved: true })
    expect(prepare).toHaveBeenCalledWith({ confirmationId: 'c1', sessionId: 'session-1', projectContext })
    const revoke = vi.fn()
    await act(async () => issue({ isCurrent: () => true, revoke }))
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(calls[1].body).toEqual({ confirm: { confirmationId: 'c1', approved: true } })
    expect(revoke).not.toHaveBeenCalled()
  })

  const solveApproval = { ...WRITE_APPROVAL, tool: 'solar-solve-proposal', capability: 'solve' }
  const solveProps = { canonicalProjectWriteTools: new Set(['solar-solve-proposal']) }

  it('canonical solve waits for private custody before its actual resume', async () => {
    let issue
    const pending = new Promise(resolve => { issue = resolve })
    const prepare = vi.fn(() => pending)
    const { calls } = await start(prepare, solveProps, 'Approve', solveApproval)
    await waitFor(() => expect(prepare).toHaveBeenCalledTimes(1))
    expect(calls).toHaveLength(1)
    const revoke = vi.fn()
    await act(async () => issue({ isCurrent: () => true, revoke }))
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(calls[1].body).toEqual({ confirm: { confirmationId: 'c1', approved: true } })
    expect(revoke).not.toHaveBeenCalled()
  })

  it('canonical solve denial never exchanges checkout custody', async () => {
    const prepare = vi.fn()
    const { calls } = await start(prepare, solveProps, 'Deny', solveApproval)
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(calls[0].body).toEqual({ approved: false })
    expect(prepare).not.toHaveBeenCalled()
  })

  it('canonical solve revokes a stale custody result without resuming', async () => {
    let issue
    const pending = new Promise(resolve => { issue = resolve })
    const prepare = vi.fn(() => pending)
    const { calls, rerender } = await start(prepare, solveProps, 'Approve', solveApproval)
    await waitFor(() => expect(prepare).toHaveBeenCalledTimes(1))
    rerender(<ConversePanel sessionId="session-2" projectContext={projectContext}
      prepareApprovedProjectRun={prepare} {...solveProps} />)
    const revoke = vi.fn()
    await act(async () => issue({ isCurrent: () => true, revoke }))
    await waitFor(() => expect(revoke).toHaveBeenCalledTimes(1))
    expect(calls).toHaveLength(1)
  })

  it.each(['dirty', 'version', 'session', 'auth'])('holds and revokes custody after %s changes during exchange', async (change) => {
    let issue
    const custody = new Promise((resolve) => { issue = resolve })
    const prepare = vi.fn(() => custody)
    const { calls, rerender } = await start(prepare)
    await waitFor(() => expect(prepare).toHaveBeenCalledTimes(1))
    APPROVALS = [{ ...WRITE_APPROVAL, approved: true, resume_required: true }]
    rerender(<ConversePanel sessionId={change === 'session' ? 'session-2' : 'session-1'}
      projectContext={change === 'version' ? { ...projectContext, input_version_id: 'version-2' } : projectContext}
      engineDirty={change === 'dirty'} prepareApprovedProjectRun={prepare} />)
    const revoke = vi.fn()
    await act(async () => issue({ isCurrent: () => change !== 'auth', revoke }))
    await waitFor(() => expect(revoke).toHaveBeenCalledTimes(1))
    expect(calls).toHaveLength(1)
  })

  it('retains an approved resume row when custody is refused', async () => {
    const prepare = vi.fn(async () => {
      APPROVALS = [{ ...WRITE_APPROVAL, approved: true, resume_required: true }]
      return null
    })
    const { calls } = await start(prepare)
    await screen.findByRole('button', { name: 'Resume approved request' })
    expect(calls).toHaveLength(1)
  })

  it('does not exchange custody for an unbound legacy write', async () => {
    const prepare = vi.fn()
    const { calls } = await start(prepare, { projectContext: null })
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(prepare).not.toHaveBeenCalled()
  })

  it('does not exchange custody for denial', async () => {
    const prepare = vi.fn()
    const { calls } = await start(prepare, {}, 'Deny')
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(calls[0].body).toEqual({ approved: false })
    expect(prepare).not.toHaveBeenCalled()
  })

  it('preserves noncanonical write approval without an exchange', async () => {
    const prepare = vi.fn()
    const { calls } = await start(prepare, { canonicalProjectWriteTools: new Set(['solar-settings']) })
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(prepare).not.toHaveBeenCalled()
  })
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
  it.each([false, true])('closes Start before sending a write approval (resume: %s)', async (resume) => {
    APPROVALS = [{ ...WRITE_APPROVAL, ...(resume ? { resume_required: true, approved: true } : {}) }]
    const onBeforeWriteApproval = vi.fn()
    await setup({ onBeforeWriteApproval })
    fireEvent.click(screen.getByRole('button', { name: resume ? 'Resume approved request' : 'Approve' }))
    await waitFor(() => expect(resolveApproval).toHaveBeenCalledTimes(1))
    expect(onBeforeWriteApproval).toHaveBeenCalledTimes(1)
    expect(onBeforeWriteApproval.mock.invocationCallOrder[0]).toBeLessThan(resolveApproval.mock.invocationCallOrder[0])
  })

  it.each([
    ['denial', {}, 'Deny', true],
    ['read', { engineDirty: true }, 'Approve', true],
    ['dirty write', { engineDirty: true }, 'Unsaved browser edits', false],
    ['locked write', { writeLocked: true }, 'Editing locked', false],
  ])('does not close Start for %s', async (kind, props, label, sends) => {
    APPROVALS = [{ ...WRITE_APPROVAL, capability: kind === 'read' ? 'drawing.read' : 'drawing.write' }]
    const onBeforeWriteApproval = vi.fn()
    await setup({ ...props, onBeforeWriteApproval })
    fireEvent.click(screen.getByRole('button', { name: label }))
    if (sends) await waitFor(() => expect(resolveApproval).toHaveBeenCalledTimes(1))
    else expect(resolveApproval).not.toHaveBeenCalled()
    expect(onBeforeWriteApproval).not.toHaveBeenCalled()
  })

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
