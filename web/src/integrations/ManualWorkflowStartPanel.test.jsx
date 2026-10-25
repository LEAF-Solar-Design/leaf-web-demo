import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ManualWorkflowStartPanel } from './ManualWorkflowStartPanel.jsx'

const id = '11111111-1111-4111-8111-111111111111'
const ref = { id: 'orders', version: 1, digest: 'a'.repeat(64) }
const step = { id: 'send', kind: 'action', connection_id: 'orders', connector_ref: ref, operation_ref: ref, arguments: { count: 2 } }
const definition = { schema_version: 1, id: 'orders', version: 1, title: 'Order workflow', trigger: 'manual', authority_mode: 'caller_bound', steps: [step, { ...step, id: 'again' }] }
const connection = { id: 'orders', state: 'active', connector_ref: ref, operation_ids: ['orders'], grant_revision: 7 }
const run = { schema_version: 1, run_id: id, revision: 1, workflow_ref: ref, submission_digest: 'b'.repeat(64),
  authority_binding_digest: 'c'.repeat(64), authority_mode: 'caller_bound', created_at: '2026-10-20T00:00:00Z', updated_at: '2026-10-20T00:00:00Z', state: 'accepted' }
function setup() {
  vi.stubGlobal('crypto', { randomUUID: vi.fn().mockReturnValue(id) })
  const controller = new AbortController()
  const client = { getManualWorkflowDefinition: vi.fn().mockResolvedValue(definition), getConnection: vi.fn().mockResolvedValue(connection),
    submitManualWorkflow: vi.fn().mockResolvedValue({ status: 'admitted', run, replayed: false }) }
  const props = { client, projectId: id, signal: controller.signal, guard: vi.fn().mockResolvedValue(), onForbidden: vi.fn(), onAdmitted: vi.fn() }
  return { props, client, controller }
}
async function review() {
  for (const [name, value] of [['Workflow definition ID', 'orders'], ['Workflow version', '1'], ['Workflow digest', ref.digest]]) {
    fireEvent.change(screen.getByLabelText(name), { target: { value } })
  }
  fireEvent.click(screen.getByRole('button', { name: 'Load workflow review' }))
  await screen.findByRole('region', { name: 'Immutable workflow review' })
}
const start = () => fireEvent.click(screen.getByRole('button', { name: 'Start workflow', exact: true }))
const recover = () => fireEvent.click(screen.getByRole('button', { name: /Recover original request/ }))
it('offers explicit abandonment after changed permissions block original-request recovery', async () => {
  const { props, client } = setup()
  client.submitManualWorkflow.mockRejectedValue(new Error('lost'))
  render(<ManualWorkflowStartPanel {...props} />)
  await review(); start()
  await screen.findByRole('button', { name: /Recover original request/ })
  client.getConnection.mockResolvedValue({ ...connection, grant_revision: 8 })
  recover()
  await screen.findByText(/Connection permissions changed/)
  expect(screen.getByRole('button', { name: 'Load workflow review' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Leave recovery without cancelling' }))
  expect(screen.getByRole('button', { name: 'Load workflow review' })).toBeEnabled()
  expect(screen.getByLabelText('Workflow definition ID')).toHaveValue('')
  expect(client.submitManualWorkflow).toHaveBeenCalledTimes(1)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers() })

it('reviews fixed ordered actions using distinct connection reads and only submits explicitly', async () => {
  const { props, client } = setup()
  render(<ManualWorkflowStartPanel {...props} />)
  await review()
  expect(client.getConnection).toHaveBeenCalledTimes(1)
  expect(client.submitManualWorkflow).not.toHaveBeenCalled()
  expect(screen.getAllByText(/Fixed arguments:/)).toHaveLength(2)
  expect(screen.getAllByRole('listitem').map((item) => item.textContent)).toEqual([expect.stringContaining('send'), expect.stringContaining('again')])
  start()
  await waitFor(() => expect(props.onAdmitted).toHaveBeenCalledWith(run))
  expect(client.submitManualWorkflow.mock.calls[0][1]).toEqual({ schema_version: 1, workflow_ref: ref, idempotency_key: id,
    step_grants: [{ step_id: 'send', expected_grant_revision: 7 }, { step_id: 'again', expected_grant_revision: 7 }] })
  expect(props.guard).toHaveBeenCalledTimes(6)
})
it.each([
  { id: 'different' }, { state: 'revoked' }, { connector_ref: { ...ref, digest: 'd'.repeat(64) } },
  { operation_ids: ['different'] }, { grant_revision: 8 }, { grant_revision: 0 },
])('refuses changed connection authority before POST: %j', async (patch) => {
  const { props, client } = setup()
  render(<ManualWorkflowStartPanel {...props} />)
  await review()
  client.getConnection.mockResolvedValue({ ...connection, ...patch })
  start()
  await screen.findByRole('alert')
  expect(client.submitManualWorkflow).not.toHaveBeenCalled()
  expect(crypto.randomUUID).not.toHaveBeenCalled()
})
it('checks full scope again after awaited connection reads and refuses dispatch on scope loss', async () => {
  const { props, client } = setup()
  render(<ManualWorkflowStartPanel {...props} />)
  await review()
  props.guard.mockResolvedValueOnce().mockRejectedValueOnce(Object.assign(new Error('private'), { status: 403 }))
  start()
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
  expect(client.submitManualWorkflow).not.toHaveBeenCalled()
  expect(screen.queryByRole('region', { name: 'Immutable workflow review' })).toBeNull()
})
it('suppresses duplicates synchronously and recovers only the identical serialized request and key', async () => {
  const { props, client } = setup()
  let reject
  client.submitManualWorkflow.mockImplementationOnce(() => new Promise((_, fail) => { reject = fail }))
  render(<ManualWorkflowStartPanel {...props} />)
  await review()
  start(); start()
  await waitFor(() => expect(client.submitManualWorkflow).toHaveBeenCalledOnce())
  expect(screen.getByRole('button', { name: 'Start workflow', exact: true })).toBeDisabled()
  await act(async () => reject(new Error('private response lost')))
  await screen.findByText(/Outcome unknown/)
  expect(client.submitManualWorkflow).toHaveBeenCalledOnce()
  expect(screen.getByLabelText('Workflow definition ID')).toBeDisabled()
  expect(screen.queryByText('private response lost')).toBeNull()
  const original = JSON.stringify(client.submitManualWorkflow.mock.calls[0][1])
  recover()
  await waitFor(() => expect(props.onAdmitted).toHaveBeenCalledOnce())
  expect(JSON.stringify(client.submitManualWorkflow.mock.calls[1][1])).toBe(original)
  expect(crypto.randomUUID).toHaveBeenCalledOnce()
})
it('retains uncertainty and original intent when authority drifts before recovery', async () => {
  const { props, client } = setup()
  client.submitManualWorkflow.mockRejectedValueOnce(new Error('lost'))
  render(<ManualWorkflowStartPanel {...props} />)
  await review(); start()
  await screen.findByText(/Outcome unknown/)
  client.getConnection.mockResolvedValue({ ...connection, grant_revision: 8 })
  recover()
  await screen.findByText(/Connection permissions changed/)
  expect(screen.getByText(/Outcome unknown/)).toBeInTheDocument()
  expect(client.submitManualWorkflow).toHaveBeenCalledOnce()
  expect(crypto.randomUUID).toHaveBeenCalledOnce()
})
it('renders only fixed conflict refusal copy', async () => {
  const { props, client } = setup()
  client.submitManualWorkflow.mockResolvedValue({ status: 'refused', code: 'idempotency_conflict' })
  render(<ManualWorkflowStartPanel {...props} />)
  await review(); start()
  await screen.findByText(/No other invocation details are available/)
  expect(props.onAdmitted).not.toHaveBeenCalled()
  expect(screen.queryByText(/Outcome unknown/)).toBeNull()
  expect(screen.queryByRole('button', { name: /Recover original/ })).toBeNull()
})
it.each([{}, { status: 'admitted', run: { ...run, workflow_ref: { ...ref, digest: 'e'.repeat(64) } }, replayed: false }])('treats malformed or mismatched admission as unknown', async (result) => {
  const { props, client } = setup()
  client.submitManualWorkflow.mockResolvedValue(result)
  render(<ManualWorkflowStartPanel {...props} />)
  await review(); start()
  await screen.findByText(/Outcome unknown/)
  expect(props.onAdmitted).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: 'Review a new workflow intent' })).toBeNull()
})
it('clears protected state on abort and ignores a late admitted response', async () => {
  const { props, client, controller } = setup()
  let finish
  client.submitManualWorkflow.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  render(<ManualWorkflowStartPanel {...props} />)
  await review(); start()
  await waitFor(() => expect(client.submitManualWorkflow).toHaveBeenCalledOnce())
  await act(async () => controller.abort())
  await act(async () => finish({ status: 'admitted', run, replayed: false }))
  expect(screen.queryByRole('region', { name: 'Immutable workflow review' })).toBeNull()
  expect(screen.getByLabelText('Workflow definition ID')).toHaveValue('')
  expect(props.onAdmitted).not.toHaveBeenCalled()
})
it('times out as unknown and rejects a late response without automatic retry', async () => {
  const { props, client } = setup()
  let finish
  client.submitManualWorkflow.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  render(<ManualWorkflowStartPanel {...props} />)
  await review()
  vi.useFakeTimers()
  await act(async () => start())
  await act(async () => vi.advanceTimersByTime(45000))
  expect(screen.getByText(/Outcome unknown/)).toBeInTheDocument()
  await act(async () => finish({ status: 'admitted', run, replayed: false }))
  expect(props.onAdmitted).not.toHaveBeenCalled()
  expect(client.submitManualWorkflow).toHaveBeenCalledOnce()
})
