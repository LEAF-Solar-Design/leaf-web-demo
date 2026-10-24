import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ManualWorkflowRunPanel } from './ManualWorkflowRunPanel.jsx'

const runId = '11111111-1111-4111-8111-111111111111'
const childId = '22222222-2222-4222-8222-222222222222'
const ref = { id: 'orders', version: 1, digest: 'a'.repeat(64) }
const time = '2026-10-20T00:00:00Z'
const definition = { schema_version: 1, id: 'orders', version: 1, title: 'Order workflow', trigger: 'manual', authority_mode: 'caller_bound',
  steps: [{ id: 'send', kind: 'action', connection_id: 'orders', connector_ref: ref, operation_ref: ref, arguments: {} }] }
const snapshot = { definition, run: { schema_version: 2, run_id: runId, revision: 1, workflow_ref: ref,
  submission_digest: 'b'.repeat(64), authority_binding_digest: 'c'.repeat(64), authority_mode: 'caller_bound', created_at: time, updated_at: time, state: 'accepted' },
  steps: [{ run_id: runId, step_id: 'send', state: 'pending' }] }
const child = { run: { schema_version: 1, run_id: childId, revision: 1, connection_id: 'orders', connector_ref: ref, operation_ref: ref,
  grant_revision: 1, submission_digest: 'd'.repeat(64), authority_binding_digest: 'e'.repeat(64), created_at: time, updated_at: time,
  state: 'running', effect_outcome: 'unknown' }, receipts: [] }
const running = { ...snapshot, run: { ...snapshot.run, revision: 2, state: 'running' },
  steps: [{ run_id: runId, step_id: 'send', state: 'action', action: child }] }
function setup(initial = snapshot) {
  const controller = new AbortController()
  const client = { getManualWorkflowRunV2: vi.fn().mockResolvedValue(initial), resumeManualWorkflowRun: vi.fn().mockImplementation(async () => {
    client.getManualWorkflowRunV2.mockResolvedValueOnce(running)
    return { ...running, run: { ...running.run, schema_version: 1 } }
  }),
    reconcileManualWorkflowStepV2: vi.fn().mockResolvedValue(child) }
  const props = { client, projectId: runId, signal: controller.signal, guard: vi.fn().mockResolvedValue(), onForbidden: vi.fn() }
  return { props, client, controller }
}
const requested = (base = snapshot) => ({ ...base, run: { ...base.run, revision: base.run.revision + 1,
  state: 'cancel_requested', requested_at: time } })
async function load() {
  fireEvent.change(screen.getByLabelText('Run UUID'), { target: { value: runId } })
  fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
  await screen.findByRole('region', { name: 'Manual workflow run status' })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled())
}
afterEach(() => { cleanup(); vi.useRealTimers() })
it('keeps the cancellation callback alive through pending rerenders, excludes every other control and publishes the result', async () => {
  const { props, client } = setup(running)
  let finish
  client.cancelManualWorkflowRun = vi.fn().mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const view = render(<ManualWorkflowRunPanel {...props} />)
  await load()
  fireEvent.change(screen.getByLabelText('Persisted step'), { target: { value: 'send' } })
  const button = screen.getByRole('button', { name: 'Request cancellation' })
  button.focus()
  act(() => { fireEvent.click(button); fireEvent.click(button) })
  await waitFor(() => expect(client.cancelManualWorkflowRun).toHaveBeenCalledOnce())
  const operationSignal = client.cancelManualWorkflowRun.mock.calls[0][3].signal
  expect(client.cancelManualWorkflowRun).toHaveBeenCalledWith(runId, runId,
    { schema_version: 1, run_id: runId, expected_revision: 2 }, { signal: operationSignal })
  view.rerender(<ManualWorkflowRunPanel {...props} displayPreferences={{ timezone: 'UTC' }} />)
  expect(operationSignal.aborted).toBe(false)
  for (const name of ['Load', 'Refresh', 'Resume', 'Reconcile', 'Load history', 'Request cancellation']) {
    expect(screen.getByRole('button', { name, exact: true })).toBeDisabled()
  }
  expect(screen.getByLabelText('Run UUID')).toBeDisabled()
  expect(screen.getByLabelText('Persisted step')).toBeDisabled()
  expect(screen.getByText(/Requesting cancellation/)).toBeInTheDocument()
  await act(async () => finish({ status: 'recorded', snapshot: requested(running) }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled())
  expect(screen.getByText(/Cancellation requested. Workflow settlement/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Reconcile' })).toBeEnabled()
  expect(client.resumeManualWorkflowRun).not.toHaveBeenCalled()
  expect(operationSignal.aborted).toBe(false)
})
it('holds the shared lock during history reads, preventing Load, Resume and cancellation dispatch', async () => {
  const { props, client } = setup()
  let finish
  client.listManualWorkflowRunsV2 = vi.fn().mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  client.cancelManualWorkflowRun = vi.fn()
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  fireEvent.click(screen.getByRole('button', { name: 'Load history' }))
  await waitFor(() => expect(client.listManualWorkflowRunsV2).toHaveBeenCalledOnce())
  expect(screen.getByRole('button', { name: 'Load', exact: true })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Request cancellation' })).toBeNull()
  fireEvent.submit(screen.getByRole('button', { name: 'Load', exact: true }).closest('form'))
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledOnce()
  expect(client.cancelManualWorkflowRun).not.toHaveBeenCalled()
  await act(async () => finish({ items: [snapshot.run], next_cursor: null }))
  expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled()
})
it('aborts a superseded history read without releasing the admitted detail operation lock', async () => {
  const { props, client } = setup()
  let finishHistory, finishDetail
  client.listManualWorkflowRunsV2 = vi.fn().mockImplementation(() => new Promise((resolve) => { finishHistory = resolve }))
  client.getManualWorkflowRunV2.mockImplementationOnce(() => new Promise((resolve) => { finishDetail = resolve }))
  const view = render(<ManualWorkflowRunPanel {...props} />)
  fireEvent.click(screen.getByRole('button', { name: 'Load history' }))
  await waitFor(() => expect(client.listManualWorkflowRunsV2).toHaveBeenCalledOnce())
  const historySignal = client.listManualWorkflowRunsV2.mock.calls[0][1].signal
  view.rerender(<ManualWorkflowRunPanel {...props} requestedRunId={runId} admittedBinding={snapshot.run} />)
  await waitFor(() => expect(client.getManualWorkflowRunV2).toHaveBeenCalledOnce())
  expect(historySignal.aborted).toBe(true)
  await act(async () => finishHistory({ items: [running.run], next_cursor: null }))
  expect(screen.queryByRole('list', { name: 'Stored workflow runs' })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByRole('button', { name: 'Load', exact: true })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Refresh', exact: true })).toBeDisabled()
  fireEvent.submit(screen.getByRole('button', { name: 'Load', exact: true }).closest('form'))
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledOnce()
  await act(async () => finishDetail(snapshot))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled())
})
it('rejects regressive child reconciliation evidence before reading the V2 parent', async () => {
  const initial = { ...running, steps: [{ ...running.steps[0], action: { ...child, run: { ...child.run, revision: 2 } } }] }
  const { props, client } = setup(initial)
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  fireEvent.change(screen.getByLabelText('Persisted step'), { target: { value: 'send' } })
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile' }))
  await screen.findByText(/Outcome unknown/)
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledOnce()
  expect(screen.getByText('Workflow in progress.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Reconcile' })).toBeDisabled()
})
it('validates ordinary V1 Resume before a fresh V2 detail read and never displays the V1 result directly', async () => {
  const { props, client } = setup()
  const order = []
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  props.guard.mockImplementation(async () => { order.push('guard') })
  client.resumeManualWorkflowRun.mockImplementation(async () => {
    order.push('resume-v1')
    return { ...running, run: { ...running.run, schema_version: 1 } }
  })
  let finish
  client.getManualWorkflowRunV2.mockImplementationOnce(() => { order.push('detail-v2'); return new Promise((resolve) => { finish = resolve }) })
  fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  expect(screen.getByText('Accepted. Awaiting execution results.')).toBeInTheDocument()
  expect(order).toEqual(['guard', 'resume-v1', 'guard', 'detail-v2'])
  await act(async () => finish(running))
  expect(order).toEqual(['guard', 'resume-v1', 'guard', 'detail-v2', 'guard'])
  expect(screen.getByText('Workflow in progress.')).toBeInTheDocument()
})
it.each(['before', 'after'])('fences cancellation on fresh caller authority loss %s transport', async (phase) => {
  const { props, client } = setup()
  client.cancelManualWorkflowRun = vi.fn().mockResolvedValue({ status: 'recorded', snapshot: requested() })
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  if (phase === 'after') props.guard.mockResolvedValueOnce()
  props.guard.mockRejectedValueOnce({ status: 403 })
  fireEvent.click(screen.getByRole('button', { name: 'Request cancellation' }))
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
  expect(client.cancelManualWorkflowRun).toHaveBeenCalledTimes(phase === 'before' ? 0 : 1)
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
})
it('requires successful explicit Refresh after cancellation response loss and retains entered values', async () => {
  const { props, client } = setup(running)
  client.cancelManualWorkflowRun = vi.fn().mockRejectedValue(new Error('private loss'))
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  fireEvent.change(screen.getByLabelText('Persisted step'), { target: { value: 'send' } })
  fireEvent.click(screen.getByRole('button', { name: 'Request cancellation' }))
  await screen.findByText(/Outcome unknown/)
  expect(screen.getByLabelText('Run UUID')).toHaveValue(runId)
  expect(screen.getByLabelText('Persisted step')).toHaveValue('send')
  expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Reconcile' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Request cancellation' })).toBeNull()
  client.getManualWorkflowRunV2.mockRejectedValueOnce(new Error('lost refresh'))
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByText(/The workflow could not be verified/)
  expect(screen.getByText(/Outcome unknown/)).toBeInTheDocument()
  client.getManualWorkflowRunV2.mockResolvedValueOnce(requested(running))
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByText(/Cancellation requested. Workflow settlement/)
  expect(screen.queryByText(/Outcome unknown/)).toBeNull()
  expect(client.cancelManualWorkflowRun).toHaveBeenCalledOnce()
  expect(screen.getByRole('button', { name: 'Reconcile' })).toBeEnabled()
})
it('reconciles an existing child explicitly after cancellation is requested, then reads V2 settlement', async () => {
  const initial = requested(running)
  const { props, client } = setup(initial)
  const action = { run: { ...child.run, revision: 2, state: 'succeeded', effect_outcome: 'applied' },
    receipts: [{ schema_version: 1, receipt_id: runId, run_id: childId, sequence: 1,
      submission_digest: child.run.submission_digest, operation_ref: ref, observed_at: time, effect_outcome: 'applied' }] }
  client.reconcileManualWorkflowStepV2.mockResolvedValue(action)
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Persisted step'), { target: { value: 'send' } })
  client.getManualWorkflowRunV2.mockResolvedValueOnce({ ...initial, run: { ...initial.run, revision: 4, state: 'cancelled' },
    steps: [{ ...initial.steps[0], action }] })
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile' }))
  await screen.findByText('Workflow cancelled. Cancellation does not undo applied actions.')
  expect(client.reconcileManualWorkflowStepV2).toHaveBeenCalledExactlyOnceWith(runId, runId, 'send', {}, { signal: expect.any(AbortSignal) })
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledTimes(2)
  expect(client.resumeManualWorkflowRun).not.toHaveBeenCalled()
  expect(screen.getByText('1 of 1 step actions recorded as applied.')).toBeInTheDocument()
})
it.each([{}, { status: 'recorded', snapshot }, { status: 'recorded', snapshot: requested({ ...snapshot,
  run: { ...snapshot.run, submission_digest: 'f'.repeat(64) } }) }])('retains evidence and locks mutations on invalid cancellation evidence: %j', async (result) => {
  const { props, client } = setup()
  client.cancelManualWorkflowRun = vi.fn().mockResolvedValue(result)
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  fireEvent.click(screen.getByRole('button', { name: 'Request cancellation' }))
  await screen.findByText(/Outcome unknown/)
  expect(screen.getByText('Accepted. Awaiting execution results.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
})
it.each([401, 403])('clears cancellation evidence and calls onForbidden on authority loss %s', async (status) => {
  const { props, client } = setup()
  client.cancelManualWorkflowRun = vi.fn().mockRejectedValue({ status })
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  fireEvent.click(screen.getByRole('button', { name: 'Request cancellation' }))
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
})
it.each(['project', 'signal', 'client', 'abort', 'unmount'])('aborts pending cancellation and ignores an abort-resistant reply on %s change', async (change) => {
  const { props, client, controller } = setup()
  let finish
  client.cancelManualWorkflowRun = vi.fn().mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const view = render(<ManualWorkflowRunPanel {...props} />)
  await load()
  fireEvent.click(screen.getByRole('button', { name: 'Request cancellation' }))
  await waitFor(() => expect(client.cancelManualWorkflowRun).toHaveBeenCalledOnce())
  const pendingSignal = client.cancelManualWorkflowRun.mock.calls[0][3].signal
  if (change === 'abort') act(() => controller.abort())
  else if (change === 'unmount') view.unmount()
  else view.rerender(<ManualWorkflowRunPanel {...props} {...(change === 'project' ? { projectId: childId } :
    change === 'signal' ? { signal: new AbortController().signal } : { client: { ...client } })} />)
  expect(pendingSignal.aborted).toBe(true)
  await act(async () => finish({ status: 'recorded', snapshot: requested() }))
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
})
it('times out cancellation without resending and suppresses its late response', async () => {
  const { props, client } = setup()
  let finish
  client.cancelManualWorkflowRun = vi.fn().mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  vi.useFakeTimers()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Request cancellation' })))
  await act(async () => vi.advanceTimersByTime(45000))
  expect(client.cancelManualWorkflowRun.mock.calls[0][3].signal.aborted).toBe(true)
  expect(screen.getByText(/Outcome unknown/)).toBeInTheDocument()
  await act(async () => finish({ status: 'recorded', snapshot: requested() }))
  expect(screen.getByText('Accepted. Awaiting execution results.')).toBeInTheDocument()
  expect(client.cancelManualWorkflowRun).toHaveBeenCalledOnce()
})
it('retains applied receipts across cancellation, then rejects requested_at drift on Refresh', async () => {
  const receipt = { schema_version: 1, receipt_id: runId, run_id: childId, sequence: 1,
    submission_digest: child.run.submission_digest, operation_ref: ref, observed_at: time, effect_outcome: 'applied' }
  const applied = { ...running, steps: [{ ...running.steps[0], action: {
    run: { ...child.run, state: 'succeeded', effect_outcome: 'applied' }, receipts: [receipt] } }] }
  const { props, client } = setup(applied)
  const cancelled = { ...requested(applied), run: { ...requested(applied).run, state: 'cancelled' } }
  client.cancelManualWorkflowRun = vi.fn().mockResolvedValue({ status: 'recorded', snapshot: cancelled })
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  fireEvent.click(screen.getByRole('button', { name: 'Request cancellation' }))
  await screen.findByText('Workflow cancelled. Cancellation does not undo applied actions.')
  expect(screen.getByText('1 of 1 step actions recorded as applied.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Reconcile' })).toBeDisabled()
  client.getManualWorkflowRunV2.mockResolvedValueOnce({ ...cancelled, run: { ...cancelled.run, revision: 4,
    requested_at: '2026-10-20T00:00:01Z', updated_at: '2026-10-20T00:00:01Z' } })
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByRole('alert')
  expect(screen.getByText('Workflow cancelled. Cancellation does not undo applied actions.')).toBeInTheDocument()
})
async function history() {
  fireEvent.click(screen.getByRole('button', { name: 'Load history' }))
  await waitFor(() => expect(screen.getByRole('button', { name: `Open run details ${runId}` })).toBeEnabled())
}
it('opens a stored summary through one explicit detail read and never dispatches execution', async () => {
  const { props, client } = setup()
  client.listManualWorkflowRunsV2 = vi.fn().mockResolvedValue({ items: [snapshot.run], next_cursor: null })
  client.submitManualWorkflow = vi.fn()
  render(<ManualWorkflowRunPanel {...props} />)
  expect(client.listManualWorkflowRunsV2).not.toHaveBeenCalled()
  expect(client.getManualWorkflowRunV2).not.toHaveBeenCalled()
  await history()
  expect(client.getManualWorkflowRunV2).not.toHaveBeenCalled()
  const button = screen.getByRole('button', { name: `Open run details ${runId}` })
  act(() => { fireEvent.click(button); fireEvent.click(button) })
  await screen.findByRole('region', { name: 'Manual workflow run status' })
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledExactlyOnceWith(runId, runId, { signal: expect.any(AbortSignal) })
  expect(screen.getByLabelText('Run UUID')).toHaveValue(runId)
  expect(client.submitManualWorkflow).not.toHaveBeenCalled()
  expect(client.resumeManualWorkflowRun).not.toHaveBeenCalled()
  expect(client.reconcileManualWorkflowStepV2).not.toHaveBeenCalled()
  expect(document.activeElement).toContainElement(screen.getByText('Run details verified.'))
})
it.each(['workflow_ref', 'submission_digest', 'authority_binding_digest', 'created_at', 'revision'])('anchors selected history %s before accepting details', async (field) => {
  const { props, client } = setup()
  const patch = { workflow_ref: { ...ref, digest: 'f'.repeat(64) }, submission_digest: 'f'.repeat(64),
    authority_binding_digest: 'f'.repeat(64), created_at: '2026-10-19T00:00:00Z', revision: 2 }
  client.listManualWorkflowRunsV2 = vi.fn().mockResolvedValue({ items: [{ ...snapshot.run, [field]: patch[field] }], next_cursor: null })
  render(<ManualWorkflowRunPanel {...props} />)
  await history()
  fireEvent.click(screen.getByRole('button', { name: `Open run details ${runId}` }))
  await screen.findByRole('alert')
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledOnce()
  expect(client.resumeManualWorkflowRun).not.toHaveBeenCalled()
})
it('supports A to B to A reselection while retaining the stricter verified revision of A', async () => {
  const { props, client } = setup(running)
  const other = { ...snapshot, run: { ...snapshot.run, run_id: childId }, steps: [{ ...snapshot.steps[0], run_id: childId }] }
  client.listManualWorkflowRunsV2 = vi.fn().mockResolvedValue({ items: [snapshot.run, other.run], next_cursor: null })
  render(<ManualWorkflowRunPanel {...props} />)
  await history()
  const select = (id) => fireEvent.click(screen.getByRole('button', { name: `Open run details ${id}` }))
  select(runId)
  await screen.findByText('Workflow in progress.')
  await waitFor(() => expect(screen.getByRole('button', { name: `Open run details ${childId}` })).toBeEnabled())
  client.getManualWorkflowRunV2.mockResolvedValueOnce(other)
  select(childId)
  await screen.findByText('Accepted. Awaiting execution results.')
  await waitFor(() => expect(screen.getByRole('button', { name: `Open run details ${runId}` })).toBeEnabled())
  client.getManualWorkflowRunV2.mockResolvedValueOnce(snapshot)
  select(runId)
  await screen.findByRole('alert')
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
  expect(screen.getByLabelText('Run UUID')).toHaveValue(runId)
  select(runId)
  await screen.findByText('Workflow in progress.')
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledTimes(4)
  expect(client.listManualWorkflowRunsV2).toHaveBeenCalledOnce()
})
it('keeps selection disabled through parent operations and clears summaries when a detail read is forbidden', async () => {
  const { props, client } = setup()
  client.listManualWorkflowRunsV2 = vi.fn().mockResolvedValue({ items: [snapshot.run], next_cursor: null })
  let finish
  client.getManualWorkflowRunV2.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  render(<ManualWorkflowRunPanel {...props} />)
  await history()
  fireEvent.click(screen.getByRole('button', { name: `Open run details ${runId}` }))
  await waitFor(() => expect(client.getManualWorkflowRunV2).toHaveBeenCalledOnce())
  expect(screen.getByRole('button', { name: `Open run details ${runId}` })).toBeDisabled()
  await act(async () => finish(snapshot))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh', exact: true })).toBeEnabled())
  client.getManualWorkflowRunV2.mockRejectedValueOnce(Object.assign(new Error('private'), { status: 403 }))
  fireEvent.click(screen.getByRole('button', { name: 'Refresh', exact: true }))
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
  expect(screen.queryByRole('list', { name: 'Stored workflow runs' })).toBeNull()
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
})
it('rejects late selected details when the inspector context changes', async () => {
  const { props, client } = setup()
  client.listManualWorkflowRunsV2 = vi.fn().mockResolvedValue({ items: [snapshot.run], next_cursor: null })
  let finish
  client.getManualWorkflowRunV2.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const view = render(<ManualWorkflowRunPanel {...props} />)
  await history()
  fireEvent.click(screen.getByRole('button', { name: `Open run details ${runId}` }))
  await waitFor(() => expect(client.getManualWorkflowRunV2).toHaveBeenCalledOnce())
  const pendingSignal = client.getManualWorkflowRunV2.mock.calls[0][2].signal
  view.rerender(<ManualWorkflowRunPanel {...props} projectId={childId} />)
  expect(pendingSignal.aborted).toBe(true)
  expect(screen.queryByRole('list', { name: 'Stored workflow runs' })).toBeNull()
  await act(async () => finish(snapshot))
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
  expect(screen.getByLabelText('Run UUID')).toHaveValue('')
})
it('retains the admitted UUID after first GET failure and recovers only through explicit Refresh', async () => {
  const { props, client } = setup()
  client.getManualWorkflowRunV2.mockRejectedValueOnce(new Error('lost'))
  const view = render(<ManualWorkflowRunPanel {...props} requestedRunId={runId} admittedBinding={{ ...snapshot.run, schema_version: 1 }} />)
  await screen.findByRole('alert')
  expect(screen.getByLabelText('Run UUID')).toHaveValue(runId)
  view.rerender(<ManualWorkflowRunPanel {...props} requestedRunId={runId} admittedBinding={{ ...snapshot.run }} />)
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByRole('region', { name: 'Manual workflow run status' })
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledTimes(2)
  expect(client.resumeManualWorkflowRun).not.toHaveBeenCalled()
})
it.each(['workflow_ref', 'submission_digest', 'authority_binding_digest', 'created_at', 'revision'])('checks the admitted %s anchor on the first GET', async (field) => {
  const { props, client } = setup()
  const patch = { workflow_ref: { ...ref, digest: 'f'.repeat(64) }, submission_digest: 'f'.repeat(64),
    authority_binding_digest: 'f'.repeat(64), created_at: '2026-10-19T00:00:00Z', revision: 2 }
  const anchor = { ...snapshot.run, [field]: patch[field] }
  render(<ManualWorkflowRunPanel {...props} requestedRunId={runId} admittedBinding={anchor} />)
  await screen.findByRole('alert')
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledWith(runId, runId, { signal: expect.any(AbortSignal) })
})
it('aborts A for rapid B handoff, ignores late A, and leaves later manual selection alone', async () => {
  const { props, client } = setup()
  let finishA
  client.getManualWorkflowRunV2.mockImplementationOnce(() => new Promise((resolve) => { finishA = resolve }))
  const other = { ...snapshot, run: { ...snapshot.run, run_id: childId }, steps: [{ ...snapshot.steps[0], run_id: childId }] }
  client.getManualWorkflowRunV2.mockResolvedValueOnce(other)
  const view = render(<ManualWorkflowRunPanel {...props} requestedRunId={runId} admittedBinding={snapshot.run} />)
  await waitFor(() => expect(client.getManualWorkflowRunV2).toHaveBeenCalledOnce())
  const firstSignal = client.getManualWorkflowRunV2.mock.calls[0][2].signal
  view.rerender(<ManualWorkflowRunPanel {...props} requestedRunId={childId} admittedBinding={other.run} />)
  await screen.findByRole('region', { name: 'Manual workflow run status' })
  expect(firstSignal.aborted).toBe(true)
  await act(async () => finishA(snapshot))
  expect(screen.getByLabelText('Run UUID')).toHaveValue(childId)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Load', exact: true })).toBeEnabled())
  await load()
  view.rerender(<ManualWorkflowRunPanel {...props} requestedRunId={childId} admittedBinding={{ ...other.run }} />)
  expect(screen.getByLabelText('Run UUID')).toHaveValue(runId)
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledTimes(3)
})
it('loads and refreshes without dispatch, and resumes only on explicit request', async () => {
  const { props, client } = setup()
  render(<ManualWorkflowRunPanel {...props} />)
  expect(client.getManualWorkflowRunV2).not.toHaveBeenCalled()
  await load()
  expect(screen.getByText('Accepted. Awaiting execution results.')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(props.guard).toHaveBeenCalledTimes(4))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled())
  expect(client.resumeManualWorkflowRun).not.toHaveBeenCalled()
  expect(client.reconcileManualWorkflowStepV2).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
  await screen.findByText('Workflow in progress.')
  expect(client.resumeManualWorkflowRun).toHaveBeenCalledWith(runId, runId, {}, { signal: expect.any(AbortSignal) })
  expect(screen.getByLabelText('Run UUID')).toHaveValue(runId)
})
it.each([snapshot, running])('reconciles only the selected persisted step and reloads its parent, never resumes', async (initial) => {
  const { props, client } = setup(initial)
  const order = []
  client.reconcileManualWorkflowStepV2.mockImplementation(async () => { order.push('reconcile'); return child })
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  client.getManualWorkflowRunV2.mockImplementation(async () => { order.push('get'); return running })
  fireEvent.change(screen.getByLabelText('Persisted step'), { target: { value: 'send' } })
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Reconcile' })).toBeEnabled())
  expect(order).toEqual(['reconcile', 'get'])
  expect(client.reconcileManualWorkflowStepV2).toHaveBeenCalledWith(runId, runId, 'send', {}, { signal: expect.any(AbortSignal) })
  expect(client.resumeManualWorkflowRun).not.toHaveBeenCalled()
  expect(props.guard).toHaveBeenCalledTimes(5)
})
it('allows only one operation even with immediate double submit', async () => {
  const { props, client } = setup()
  let finish
  client.getManualWorkflowRunV2.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  render(<ManualWorkflowRunPanel {...props} />)
  fireEvent.change(screen.getByLabelText('Run UUID'), { target: { value: runId } })
  const form = screen.getByRole('button', { name: 'Load', exact: true }).closest('form')
  fireEvent.submit(form)
  fireEvent.submit(form)
  await waitFor(() => expect(client.getManualWorkflowRunV2).toHaveBeenCalledTimes(1))
  expect(screen.getByText('Load in progress…')).toBeInTheDocument()
  await act(async () => finish(snapshot))
})
it.each(['Resume', 'Reconcile'])('recovers lost %s outcomes through explicit Refresh without automatic retries', async (operation) => {
  const { props, client } = setup()
  client.resumeManualWorkflowRun.mockRejectedValue(new Error('private secret'))
  client.reconcileManualWorkflowStepV2.mockRejectedValue(new Error('private secret'))
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  fireEvent.change(screen.getByLabelText('Persisted step'), { target: { value: 'send' } })
  fireEvent.click(screen.getByRole('button', { name: operation }))
  await screen.findByText(/Outcome unknown. Work may have completed/)
  expect(screen.getByRole('region', { name: 'Manual workflow run status' })).toBeInTheDocument()
  expect(screen.queryByText('private secret')).toBeNull()
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(screen.queryByText(/Outcome unknown. Work may have completed/)).toBeNull())
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledTimes(2)
})
it('retains uncertainty if reconciliation succeeds but the parent reload fails', async () => {
  const { props, client } = setup()
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  client.getManualWorkflowRunV2.mockRejectedValueOnce(new Error('lost'))
  fireEvent.change(screen.getByLabelText('Persisted step'), { target: { value: 'send' } })
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile' }))
  await screen.findByText(/Outcome unknown/)
  expect(client.resumeManualWorkflowRun).not.toHaveBeenCalled()
  expect(screen.getByText('Accepted. Awaiting execution results.')).toBeInTheDocument()
})
it.each(['run_id', 'connection_id', 'connector_ref', 'operation_ref', 'grant_revision', 'submission_digest', 'authority_binding_digest', 'created_at'])('rejects reconciled child %s drift before parent reload', async (field) => {
  const { props, client } = setup(running)
  const values = { run_id: runId, connection_id: 'different', connector_ref: { ...ref, digest: 'f'.repeat(64) },
    operation_ref: { ...ref, digest: 'f'.repeat(64) }, grant_revision: 2, submission_digest: 'f'.repeat(64),
    authority_binding_digest: 'f'.repeat(64), created_at: '2026-10-19T00:00:00Z' }
  client.reconcileManualWorkflowStepV2.mockResolvedValue({ ...child, run: { ...child.run, [field]: values[field] } })
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  fireEvent.change(screen.getByLabelText('Persisted step'), { target: { value: 'send' } })
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile' }))
  await screen.findByText(/Outcome unknown/)
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledTimes(1)
})
it.each([
  {}, { ...snapshot, run: { ...snapshot.run, run_id: childId }, steps: [{ run_id: childId, step_id: 'send', state: 'pending' }] },
  { ...running, run: { ...running.run, submission_digest: 'f'.repeat(64) } }, snapshot,
])('preserves verified evidence after malformed, mismatched, identity-changing or regressive updates', async (update) => {
  const { props, client } = setup(running)
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  client.getManualWorkflowRunV2.mockResolvedValueOnce(update)
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByRole('alert')
  expect(screen.getByText('Workflow in progress.')).toBeInTheDocument()
  expect(screen.queryByText('Accepted. Awaiting execution results.')).toBeNull()
})
it('hides old-run evidence immediately when selecting another run', async () => {
  const { props, client } = setup()
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  client.getManualWorkflowRunV2.mockImplementation(() => new Promise(() => {}))
  fireEvent.change(screen.getByLabelText('Run UUID'), { target: { value: childId } })
  fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
})
it('shows terminal failure evidence without execution controls being enabled', async () => {
  const failed = { ...snapshot, run: { ...snapshot.run, state: 'failed' }, steps: [{ run_id: runId, step_id: 'send', state: 'blocked', failure_code: 'forbidden', effect_outcome: 'not_dispatched' }] }
  const { props, client } = setup(failed)
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  expect(screen.getByText(/This run is terminal/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Reconcile' })).toBeDisabled()
  expect(client.resumeManualWorkflowRun).not.toHaveBeenCalled()
})
it('keeps uncertainty visible when an explicit recovery Refresh also fails', async () => {
  const { props, client } = setup()
  client.resumeManualWorkflowRun.mockRejectedValueOnce(new Error('lost'))
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
  await screen.findByText(/Outcome unknown/)
  client.getManualWorkflowRunV2.mockRejectedValueOnce(new Error('lost read'))
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByText(/The workflow could not be verified/)
  expect(screen.getByText(/Outcome unknown/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
})
it('shows a successful terminal run and its canonical applied receipt without dispatch', async () => {
  const action = { run: { ...child.run, state: 'succeeded', effect_outcome: 'applied' }, receipts: [{ schema_version: 1,
    receipt_id: runId, run_id: childId, sequence: 1, submission_digest: child.run.submission_digest, operation_ref: ref,
    observed_at: time, effect_outcome: 'applied' }] }
  const succeeded = { ...running, run: { ...running.run, state: 'succeeded' }, steps: [{ ...running.steps[0], action }] }
  const { props, client } = setup(succeeded)
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  expect(screen.getByText('Workflow succeeded.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
  expect(client.resumeManualWorkflowRun).not.toHaveBeenCalled()
  client.getManualWorkflowRunV2.mockResolvedValueOnce({ ...succeeded, run: { ...succeeded.run, revision: 3, state: 'running' } })
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByRole('alert')
  expect(screen.getByText('Workflow succeeded.')).toBeInTheDocument()
  expect(screen.queryByText('All step actions applied. Workflow completion has not been recorded.')).toBeNull()
  expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Reconcile' })).toBeDisabled()
  expect(client.resumeManualWorkflowRun).not.toHaveBeenCalled()
})
it('shows canonical unknown child evidence and reconciles only explicitly', async () => {
  const action = { run: { ...child.run, state: 'uncertain' }, receipts: [{ schema_version: 1,
    receipt_id: runId, run_id: childId, sequence: 1, submission_digest: child.run.submission_digest, operation_ref: ref,
    observed_at: time, effect_outcome: 'unknown', failure_code: 'outcome_unknown' }] }
  const uncertain = { ...running, run: { ...running.run, state: 'uncertain' }, steps: [{ ...running.steps[0], action }] }
  const { props, client } = setup(uncertain)
  client.reconcileManualWorkflowStepV2.mockResolvedValue(action)
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  expect(screen.getByText('Workflow outcome uncertain. An action outcome is unknown.')).toBeInTheDocument()
  expect(client.reconcileManualWorkflowStepV2).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('Persisted step'), { target: { value: 'send' } })
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile' }))
  await waitFor(() => expect(client.getManualWorkflowRunV2).toHaveBeenCalledTimes(2))
  expect(client.resumeManualWorkflowRun).not.toHaveBeenCalled()
})
it.each([401, 403])('clears protected evidence on HTTP %s revocation', async (status) => {
  const { props, client } = setup()
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  client.getManualWorkflowRunV2.mockRejectedValueOnce(Object.assign(new Error('private'), { status }))
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledTimes(1))
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledTimes(2)
})
it('retains pending evidence and a safe error when an unstarted step reconciliation is refused', async () => {
  const { props, client } = setup()
  client.reconcileManualWorkflowStepV2.mockRejectedValueOnce(Object.assign(new Error('private refusal'), { status: 409 }))
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  fireEvent.change(screen.getByLabelText('Persisted step'), { target: { value: 'send' } })
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile' }))
  await screen.findByText(/Outcome unknown/)
  expect(screen.getByText('Pending. No execution result recorded.')).toBeInTheDocument()
  expect(screen.queryByText('private refusal')).toBeNull()
  expect(client.getManualWorkflowRunV2).toHaveBeenCalledTimes(1)
})
it('times out a mutation as unknown and ignores a late result', async () => {
  const { props, client } = setup()
  let finish
  client.resumeManualWorkflowRun.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  render(<ManualWorkflowRunPanel {...props} />)
  await load()
  vi.useFakeTimers()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Resume' })))
  const signal = client.resumeManualWorkflowRun.mock.calls[0][3].signal
  await act(async () => vi.advanceTimersByTime(45000))
  expect(signal.aborted).toBe(true)
  expect(screen.getByText(/Outcome unknown/)).toBeInTheDocument()
  await act(async () => finish({ ...running, run: { ...running.run, schema_version: 1 } }))
  expect(screen.getByText('Accepted. Awaiting execution results.')).toBeInTheDocument()
})
