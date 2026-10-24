import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { WorkflowScheduleDraftPanel } from './WorkflowScheduleDraftPanel.jsx'
import { IntegrationWorkflowScheduleDraftSaveError } from '../../../vendor/assistant-react/src/actions'

const projectId = '11111111-1111-4111-8111-111111111111'
const workflowRef = { id: 'workflow', version: 3, digest: 'a'.repeat(64) }
const draftId = `schedule-draft-${projectId}`
const reference = { id: draftId, version: 1, digest: 'b'.repeat(64) }
const initial = { schema_version: 1, id: draftId, version: 1, title: 'Order workflow', workflow_ref: workflowRef,
  schedule: { kind: 'daily', timezone: 'UTC', hour: 9, minute: 0,
    gap_policy: 'next_valid_minute', fold_policy: 'first_occurrence' }, state: 'draft' }
const clone = (value) => JSON.parse(JSON.stringify(value))
const change = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const submit = () => fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
const unknown = () => screen.findByText(/Save outcome unknown/)
const saved = () => screen.findByText(/^Draft saved\./)
function setup() {
  const parent = new AbortController()
  let stored
  const client = {
    registerWorkflowScheduleDraft: vi.fn(async (_, draft) => { stored = clone(draft); return clone(reference) }),
    getWorkflowScheduleDraft: vi.fn(async () => clone(stored)),
  }
  const props = { client, projectId, workflowRef, title: initial.title, signal: parent.signal,
    guard: vi.fn().mockResolvedValue(), onForbidden: vi.fn() }
  return { props, parent, client }
}
beforeEach(() => vi.stubGlobal('crypto', { randomUUID: vi.fn(() => projectId) }))
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

it('is inert on mount and equivalent rerender, retaining one identity and edits', () => {
  const { props, client } = setup()
  const { rerender } = render(<WorkflowScheduleDraftPanel {...props} />)
  expect(screen.getByLabelText('Draft title')).toHaveValue(initial.title)
  expect(screen.getByLabelText('Timezone')).toHaveValue('UTC')
  expect(screen.getByLabelText('Hour (0 to 23)')).toHaveValue('9')
  expect(screen.getByLabelText('Minute (0 to 59)')).toHaveValue('0')
  expect(screen.getByText('Saving this draft does not schedule or authorize any run.')).toBeInTheDocument()
  change('Draft title', 'Edited title')
  rerender(<WorkflowScheduleDraftPanel {...props} workflowRef={{ ...workflowRef }} />)
  expect(screen.getByLabelText('Draft title')).toHaveValue('Edited title')
  expect(crypto.randomUUID).toHaveBeenCalledOnce()
  expect(client.registerWorkflowScheduleDraft).not.toHaveBeenCalled()
  expect(client.getWorkflowScheduleDraft).not.toHaveBeenCalled()
  expect(props.guard).not.toHaveBeenCalled()
  expect(document.body.textContent).not.toContain('contextKey')
})
it('never saves from preparation or elapsed time', async () => {
  vi.useFakeTimers()
  const { props, client } = setup()
  render(<WorkflowScheduleDraftPanel {...props} />)
  change('Timezone', 'US/Eastern')
  await act(async () => vi.advanceTimersByTimeAsync(90000))
  expect(client.registerWorkflowScheduleDraft).not.toHaveBeenCalled()
  expect(client.getWorkflowScheduleDraft).not.toHaveBeenCalled()
  expect(props.guard).not.toHaveBeenCalled()
})
it.each([undefined, {}, { randomUUID: () => { throw new Error('private sentinel') } },
  { randomUUID: () => 'invalid' }, { randomUUID: () => `${projectId}\n` }])('fails closed with unavailable crypto %j', (cryptoValue) => {
  vi.stubGlobal('crypto', cryptoValue)
  const { props, client } = setup()
  render(<WorkflowScheduleDraftPanel {...props} />)
  expect(screen.getByRole('status')).toHaveTextContent('Schedule draft saving is unavailable.')
  expect(screen.queryByRole('button')).toBeNull()
  expect(client.registerWorkflowScheduleDraft).not.toHaveBeenCalled()
  expect(document.body.textContent).not.toContain('private sentinel')
})
it.each([{ workflowRef: null }, { workflowRef: { ...workflowRef, digest: 'bad' } }, { title: '' },
  { projectId: 'bad' }, { signal: null }, { guard: null }, { onForbidden: null }, { client: {} }])('fails closed for malformed host props %j', (patch) => {
  const { props, client } = setup()
  render(<WorkflowScheduleDraftPanel {...props} {...patch} />)
  expect(screen.getByRole('status')).toHaveTextContent('Schedule draft saving is unavailable.')
  expect(client.registerWorkflowScheduleDraft).not.toHaveBeenCalled()
})
it('guards POST, exact GET and complete acceptance, preserves timezone spelling and locks success', async () => {
  const { props, client } = setup()
  const order = []
  props.guard.mockImplementation(async () => { order.push('guard') })
  client.registerWorkflowScheduleDraft.mockImplementation(async (_, draft) => {
    order.push('post'); expect(draft).toEqual({ ...initial, schedule: { ...initial.schedule, timezone: 'US/Eastern' } })
    return clone(reference)
  })
  client.getWorkflowScheduleDraft.mockImplementation(async (_, ref) => {
    order.push('get'); expect(ref).toEqual(reference)
    return { ...clone(initial), schedule: { ...initial.schedule, timezone: 'US/Eastern' } }
  })
  render(<WorkflowScheduleDraftPanel {...props} />)
  change('Timezone', 'US/Eastern'); submit()
  const result = await saved()
  expect(order).toEqual(['guard', 'post', 'guard', 'get', 'guard'])
  expect(result).toHaveTextContent('Nothing is scheduled or authorized to run. This version is locked.')
  expect(document.activeElement).toBe(result)
  expect(screen.getByLabelText('Timezone')).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
  const postSignal = client.registerWorkflowScheduleDraft.mock.calls[0][2].signal
  expect(postSignal).toBeInstanceOf(AbortSignal)
  expect(postSignal.aborted).toBe(false)
  expect(client.getWorkflowScheduleDraft.mock.calls[0]).toEqual([projectId, reference, { signal: postSignal }])
})
it.each(['post', 'get'])('deduplicates click/Enter and retries the exact frozen payload after uncertain %s, despite client mutation', async (phase) => {
  const { props, client } = setup()
  const payloads = []
  let finish
  client.registerWorkflowScheduleDraft.mockImplementation(async (_, input) => {
    payloads.push(clone(input)); input.title = 'mutated by client'; input.workflow_ref.digest = 'c'.repeat(64)
    if (phase === 'post' && payloads.length === 1) return new Promise((_, reject) => { finish = reject })
    return clone(reference)
  })
  client.getWorkflowScheduleDraft.mockImplementation(async () => {
    if (phase === 'get' && payloads.length === 1) return new Promise((_, reject) => { finish = reject })
    return clone(initial)
  })
  render(<WorkflowScheduleDraftPanel {...props} />)
  submit(); submit(); fireEvent.submit(screen.getByRole('button', { name: 'Save draft' }).closest('form'))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  expect(client.registerWorkflowScheduleDraft).toHaveBeenCalledOnce()
  await act(async () => finish({ status: 503, dispatched: false, message: 'private sentinel' }))
  await unknown()
  expect(screen.getByLabelText('Draft title')).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Retry save' }))
  await saved()
  expect(payloads).toEqual([initial, initial])
  expect(crypto.randomUUID).toHaveBeenCalledOnce()
  expect(document.body.textContent).not.toContain('private sentinel')
})
it('locks only POST 409 as conflict', async () => {
  const { props, client } = setup()
  client.registerWorkflowScheduleDraft.mockRejectedValueOnce({ status: 409, message: 'private sentinel' })
  render(<WorkflowScheduleDraftPanel {...props} />); submit()
  await screen.findByText(/This version conflicts with stored data and is locked/)
  expect(screen.queryByRole('button', { name: 'Retry save' })).toBeNull()
  expect(screen.getByLabelText('Draft title')).toBeDisabled()
  expect(client.getWorkflowScheduleDraft).not.toHaveBeenCalled()
  expect(document.body.textContent).not.toContain('private sentinel')
})
it.each([['post', { status: 400 }], ['get', { status: 400 }], ['get', { status: 409 }],
  ['get', new IntegrationWorkflowScheduleDraftSaveError('conflict')], ['post', new Error('private sentinel')]])('leaves %s failure %j unknown without timezone inference', async (phase, failure) => {
  const { props, client } = setup()
  client[phase === 'post' ? 'registerWorkflowScheduleDraft' : 'getWorkflowScheduleDraft'].mockRejectedValueOnce(failure)
  render(<WorkflowScheduleDraftPanel {...props} />); submit()
  await unknown()
  expect(screen.getByRole('button', { name: 'Retry save' })).toBeEnabled()
  expect(screen.getByLabelText('Timezone')).toBeDisabled()
  expect(document.body.textContent).not.toContain('private sentinel')
  expect(document.body.textContent).not.toContain('Enter another timezone')
})
it.each(['post', 'get', 'guard'])('invalidates old authority on 401/403 from %s', async (phase) => {
  for (const status of [401, 403]) {
    const { props, client } = setup()
    if (phase === 'guard') props.guard.mockRejectedValueOnce({ status })
    else client[phase === 'post' ? 'registerWorkflowScheduleDraft' : 'getWorkflowScheduleDraft'].mockRejectedValueOnce({ status })
    render(<WorkflowScheduleDraftPanel {...props} />); submit()
    await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
    expect(screen.getByText(/You do not have permission to save this schedule draft/)).toBeInTheDocument()
    const retry = screen.queryByRole('button', { name: 'Retry save' })
    if (retry) expect(retry).toBeDisabled()
    if (phase !== 'guard') expect(client.registerWorkflowScheduleDraft.mock.calls[0][2].signal.aborted).toBe(true)
    cleanup()
  }
})
it.each([{ id: 'wrong' }, { version: 2 }, { digest: 'bad' }, { extra: true }])('cannot accept a wrong or malformed acknowledgement %j', async (patch) => {
  const { props, client } = setup()
  client.registerWorkflowScheduleDraft.mockResolvedValueOnce({ ...reference, ...patch })
  render(<WorkflowScheduleDraftPanel {...props} />); submit(); await unknown()
  expect(client.getWorkflowScheduleDraft).not.toHaveBeenCalled()
})
it.each([
  { title: 'Wrong title' }, { workflow_ref: { ...workflowRef, digest: 'c'.repeat(64) } },
  { workflow_ref: { ...workflowRef, version: 4 } }, { workflow_ref: { ...workflowRef, id: 'other' } },
  ...['timezone', 'hour', 'minute', 'gap_policy', 'fold_policy'].map((field) => ({ schedule: { ...initial.schedule,
    [field]: ({ timezone: 'Etc/UTC', hour: 10, minute: 1, gap_policy: 'skip', fold_policy: 'both' })[field] } })),
  { id: 'other' }, { version: 2 }, { state: 'active' }, { schema_version: 2 }, { extra: true },
])('requires complete canonical readback equality %j', async (patch) => {
  const { props, client } = setup()
  client.getWorkflowScheduleDraft.mockResolvedValueOnce({ ...clone(initial), ...patch })
  render(<WorkflowScheduleDraftPanel {...props} />); submit(); await unknown()
  expect(screen.queryByText(/^Draft saved\./)).toBeNull()
})
it.each(['preflight', 'post', 'get', 'acceptance'])('aborts %s on parent loss and ignores transports which settle late', async (phase) => {
  const { props, client, parent } = setup()
  let finish, operationSignal
  const stalled = (signal) => { operationSignal = signal; return new Promise((resolve) => { finish = resolve }) }
  if (phase === 'preflight') props.guard.mockImplementationOnce(stalled)
  else if (phase === 'acceptance') props.guard.mockResolvedValueOnce().mockResolvedValueOnce().mockImplementationOnce(stalled)
  else client[phase === 'post' ? 'registerWorkflowScheduleDraft' : 'getWorkflowScheduleDraft'].mockImplementationOnce((_, __, { signal }) => stalled(signal))
  render(<WorkflowScheduleDraftPanel {...props} />); submit()
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  act(() => parent.abort())
  expect(operationSignal.aborted).toBe(true)
  await act(async () => finish(phase === 'post' ? reference : initial))
  expect(screen.queryByText(/^Draft saved\./)).toBeNull()
  expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
  if (phase === 'preflight') expect(client.registerWorkflowScheduleDraft).not.toHaveBeenCalled()
})
it.each(['project', 'client', 'signal', 'workflow', 'title', 'unmount'])('resets %s context and discards old readback', async (kind) => {
  const { props, client } = setup()
  let finish
  client.getWorkflowScheduleDraft.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const { rerender, unmount } = render(<WorkflowScheduleDraftPanel {...props} />)
  submit(); await waitFor(() => expect(finish).toBeTypeOf('function'))
  const oldSignal = client.getWorkflowScheduleDraft.mock.calls[0][2].signal
  const patch = { project: { projectId: '22222222-2222-4222-8222-222222222222' }, client: { client: { ...client } },
    signal: { signal: new AbortController().signal }, workflow: { workflowRef: { ...workflowRef, version: 4 } }, title: { title: 'New title' } }
  if (kind === 'unmount') unmount()
  else rerender(<WorkflowScheduleDraftPanel {...props} {...patch[kind]} />)
  expect(oldSignal.aborted).toBe(true)
  await act(async () => finish(clone(initial)))
  expect(screen.queryByText(/^Draft saved\./)).toBeNull()
  if (kind !== 'unmount') {
    expect(screen.getByLabelText('Draft title')).toHaveValue(kind === 'title' ? 'New title' : initial.title)
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeEnabled()
    expect(crypto.randomUUID).toHaveBeenCalledTimes(2)
  }
})
it('invalidates guard-discovered scope loss instead of offering old-context retry', async () => {
  const { props, client } = setup()
  props.guard.mockResolvedValueOnce().mockRejectedValueOnce(new Error('private scope sentinel'))
  render(<WorkflowScheduleDraftPanel {...props} />); submit()
  await screen.findByText(/Schedule draft saving is unavailable/)
  expect(client.getWorkflowScheduleDraft).not.toHaveBeenCalled()
  const retry = screen.queryByRole('button', { name: 'Retry save' })
  if (retry) expect(retry).toBeDisabled()
  expect(document.body.textContent).not.toContain('private scope sentinel')
})
it('uses only the shared deadline and explicitly retries after timeout while ignoring the old POST', async () => {
  vi.useFakeTimers()
  const scheduled = vi.spyOn(globalThis, 'setTimeout')
  const cleared = vi.spyOn(globalThis, 'clearTimeout')
  const { props, client } = setup()
  let finish
  client.registerWorkflowScheduleDraft.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  render(<WorkflowScheduleDraftPanel {...props} />)
  await act(async () => submit())
  const oldSignal = client.registerWorkflowScheduleDraft.mock.calls[0][2].signal
  const deadlineIndex = scheduled.mock.calls.findIndex((call) => call[1] === 45000)
  expect(deadlineIndex).toBeGreaterThanOrEqual(0)
  const deadline = scheduled.mock.results[deadlineIndex].value
  await act(async () => vi.advanceTimersByTimeAsync(45000))
  expect(oldSignal.aborted).toBe(true)
  expect(screen.getByText(/Save outcome unknown/)).toBeInTheDocument()
  expect(client.registerWorkflowScheduleDraft).toHaveBeenCalledOnce()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Retry save' })))
  expect(screen.getByText(/^Draft saved\./)).toBeInTheDocument()
  await act(async () => finish(reference))
  expect(client.getWorkflowScheduleDraft).toHaveBeenCalledOnce()
  expect(client.registerWorkflowScheduleDraft.mock.calls.map(([, draft]) => draft)).toEqual([initial, initial])
  expect(cleared).toHaveBeenCalledWith(deadline)
})
it.each(['guard', 'get'])('ignores late authorization failures from a timed-out %s after an explicit retry succeeds', async (phase) => {
  vi.useFakeTimers()
  const { props, client } = setup()
  let failExpired
  const stalled = () => new Promise((_, reject) => { failExpired = reject })
  if (phase === 'guard') props.guard.mockImplementationOnce(stalled)
  else client.getWorkflowScheduleDraft.mockImplementationOnce(stalled)
  render(<WorkflowScheduleDraftPanel {...props} />)
  await act(async () => submit())
  await act(async () => vi.advanceTimersByTimeAsync(45000))
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Retry save' })))
  expect(screen.getByText(/^Draft saved\./)).toBeInTheDocument()
  await act(async () => failExpired({ status: 403, message: 'private sentinel' }))
  expect(props.onForbidden).not.toHaveBeenCalled()
  expect(screen.getByText(/^Draft saved\./)).toBeInTheDocument()
  expect(document.body.textContent).not.toContain('private sentinel')
})
