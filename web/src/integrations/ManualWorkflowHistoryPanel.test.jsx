import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ManualWorkflowHistoryPanel } from './ManualWorkflowHistoryPanel.jsx'

const runId = '11111111-1111-4111-8111-111111111111'
const otherId = '22222222-2222-4222-8222-222222222222'
const run = { schema_version: 2, run_id: runId, revision: 1,
  workflow_ref: { id: 'orders', version: 1, digest: 'a'.repeat(64) }, submission_digest: 'b'.repeat(64),
  authority_binding_digest: 'c'.repeat(64), authority_mode: 'caller_bound',
  created_at: '2026-10-20T00:00:00Z', updated_at: '2026-10-20T00:00:00Z', state: 'accepted' }
const other = { ...run, run_id: otherId, workflow_ref: { ...run.workflow_ref, id: 'second' } }
function setup(page = { items: [run], next_cursor: null }) {
  const controller = new AbortController()
  const props = { client: { listManualWorkflowRunsV2: vi.fn().mockResolvedValue(page) }, projectId: runId,
    signal: controller.signal, guard: vi.fn().mockResolvedValue(), onForbidden: vi.fn(), onSelect: vi.fn() }
  return { props, controller, list: props.client.listManualWorkflowRunsV2 }
}
const click = (name) => fireEvent.click(screen.getByRole('button', { name, exact: true }))
async function loaded() { await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh history' })).toBeEnabled()) }
afterEach(() => { cleanup(); vi.useRealTimers() })

it('mounts inert, reads only on demand, announces and focuses results, and selects without fetching details itself', async () => {
  const { props, list } = setup()
  render(<ManualWorkflowHistoryPanel {...props} />)
  expect(list).not.toHaveBeenCalled()
  expect(props.guard).not.toHaveBeenCalled()
  expect(document.activeElement).toBe(document.body)
  click('Load history')
  await loaded()
  expect(list).toHaveBeenCalledExactlyOnceWith(runId, { limit: 25, after: undefined, signal: expect.any(AbortSignal) })
  expect(props.guard).toHaveBeenCalledTimes(2)
  expect(document.activeElement).toContainElement(screen.getByRole('status'))
  expect(screen.getByText(/stored run summaries. Opening details refreshes/)).toBeInTheDocument()
  expect(screen.getByText(/Version 1/)).toHaveTextContent('orders · Version 1 · accepted')
  click(`Open run details ${runId}`)
  expect(props.onSelect).toHaveBeenCalledExactlyOnceWith(run)
  expect(list).toHaveBeenCalledOnce()
})
it('preserves server order and replaces rather than accumulates pages', async () => {
  const { props, list } = setup({ items: [other, run], next_cursor: 'page2' })
  render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await loaded()
  expect(within(screen.getByRole('list')).getAllByRole('listitem').map((row) => row.textContent)).toEqual([
    expect.stringContaining(otherId), expect.stringContaining(runId),
  ])
  list.mockResolvedValueOnce({ items: [other], next_cursor: null })
  click('Next page'); await loaded()
  expect(screen.queryByRole('button', { name: `Open run details ${runId}` })).toBeNull()
  expect(list.mock.calls[1][1].after).toBe('page2')
  expect(screen.queryByRole('button', { name: 'Next page' })).toBeNull()
})
it('rejects self and longer cursor cycles, keeps the page, and resets traversal only on a successful refresh', async () => {
  const { props, list } = setup({ items: [run], next_cursor: 'page2' })
  render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await loaded()
  list.mockResolvedValueOnce({ items: [other], next_cursor: 'page2' })
  click('Next page'); await screen.findByRole('alert')
  expect(screen.getByRole('button', { name: `Open run details ${runId}` })).toBeEnabled()
  list.mockResolvedValueOnce({ items: [other], next_cursor: 'page3' })
  click('Retry history'); await loaded()
  list.mockRejectedValueOnce(new Error('private failure'))
  click('Refresh history'); await screen.findByRole('alert')
  list.mockResolvedValueOnce({ items: [run], next_cursor: 'page2' })
  click('Next page'); await screen.findByRole('alert')
  expect(screen.queryByRole('button', { name: `Open run details ${runId}` })).toBeNull()
  expect(screen.getByRole('button', { name: `Open run details ${otherId}` })).toBeEnabled()
  list.mockResolvedValueOnce({ items: [run], next_cursor: 'page2' })
  click('Refresh history'); await loaded()
  list.mockResolvedValueOnce({ items: [other], next_cursor: 'page3' })
  click('Next page'); await loaded()
  expect(screen.getByRole('button', { name: `Open run details ${otherId}` })).toBeEnabled()
  expect(list).toHaveBeenCalledTimes(7)
})
it.each([
  {}, { records: [], nextCursor: null }, { items: [], next_cursor: null, private: 'secret' },
  { items: [{ ...run, schema_version: 1 }], next_cursor: null },
  { items: [{ ...run, state: 'cancelled' }], next_cursor: null },
  { items: [{ ...run, private: 'secret' }], next_cursor: null },
  { items: [{ ...run, workflow_ref: { ...run.workflow_ref, private: true } }], next_cursor: null },
  { items: [run, run], next_cursor: null }, { items: Array(51).fill(run), next_cursor: null },
  ...['', 'bad=', 'a\n', 'a'.repeat(1025)].map((next_cursor) => ({ items: [], next_cursor })),
])('rejects fake-client invalid data without inventing an empty page: %j', async (invalid) => {
  const { props, list } = setup(invalid)
  render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await screen.findByRole('alert')
  expect(screen.queryByRole('list')).toBeNull()
  expect(screen.queryByText('No stored runs on this page.')).toBeNull()
  expect(screen.queryByText('secret')).toBeNull()
  expect(screen.getByRole('button', { name: 'Retry history' })).toBeEnabled()
  list.mockResolvedValueOnce({ items: [], next_cursor: null })
  click('Retry history'); await loaded()
  expect(screen.getByText('No stored runs on this page.')).toBeInTheDocument()
})
it('shows pending state and prevents duplicate requests and selection', async () => {
  const { props, list } = setup()
  render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await loaded()
  let finish
  list.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  act(() => { click('Refresh history'); click('Refresh history') })
  await waitFor(() => expect(list).toHaveBeenCalledTimes(2))
  expect(screen.getByText('Loading workflow history…')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: `Open run details ${runId}` })).toBeDisabled()
  await act(async () => finish({ items: [], next_cursor: null }))
})
it.each([401, 403])('clears all evidence and reports forbidden on %s', async (status) => {
  const { props, list } = setup()
  render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await loaded()
  list.mockRejectedValueOnce(Object.assign(new Error('private'), { status }))
  click('Refresh history')
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
  expect(screen.queryByRole('list')).toBeNull()
  expect(screen.getByRole('button', { name: 'Load history' })).toBeDisabled()
})
it('guards immediately around the read and clears on a post-read authority failure', async () => {
  const { props, list } = setup()
  const order = []
  props.guard.mockImplementationOnce(async () => { order.push('before') }).mockImplementationOnce(async () => {
    order.push('after'); throw Object.assign(new Error('denied'), { status: 403 })
  })
  list.mockImplementation(async () => { order.push('get'); return { items: [run], next_cursor: null } })
  render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history')
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
  expect(order).toEqual(['before', 'get', 'after'])
  expect(screen.queryByRole('list')).toBeNull()
})
it('does not read when the preflight guard fails, and allows an explicit retry', async () => {
  const { props, list } = setup()
  props.guard.mockRejectedValueOnce(new Error('private authority failure'))
  render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await screen.findByRole('alert')
  expect(list).not.toHaveBeenCalled()
  expect(screen.queryByText('private authority failure')).toBeNull()
  click('Retry history'); await loaded()
  expect(list).toHaveBeenCalledOnce()
})
it('uses display preferences and disables all selection during parent operations', async () => {
  const { props } = setup()
  const view = render(<ManualWorkflowHistoryPanel {...props} displayPreferences={{ timezone: 'America/New_York' }} />)
  click('Load history'); await loaded()
  expect(screen.getByText(/Created/)).toHaveTextContent('Oct 19, 2026')
  view.rerender(<ManualWorkflowHistoryPanel {...props} disabled />)
  const open = screen.getByRole('button', { name: `Open run details ${runId}` })
  expect(open).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Refresh history' })).toBeDisabled()
  fireEvent.click(open)
  expect(props.onSelect).not.toHaveBeenCalled()
})
it.each(['signal', 'project', 'client', 'abort', 'unmount'])('clears context and ignores late replies after %s change', async (change) => {
  const { props, list, controller } = setup()
  const view = render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await loaded()
  let finish
  list.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  click('Refresh history')
  await waitFor(() => expect(list).toHaveBeenCalledTimes(2))
  const pendingSignal = list.mock.calls[1][1].signal
  if (change === 'abort') act(() => controller.abort())
  else if (change === 'unmount') view.unmount()
  else view.rerender(<ManualWorkflowHistoryPanel {...props} {...(change === 'signal' ? { signal: new AbortController().signal } :
    change === 'project' ? { projectId: otherId } : { client: { ...props.client } })} />)
  expect(pendingSignal.aborted).toBe(true)
  expect(screen.queryByRole('list')).toBeNull()
  await act(async () => finish({ items: [other], next_cursor: 'late' }))
  expect(screen.queryByRole('list')).toBeNull()
})
it('times out safely, permits explicit retry, and ignores late results', async () => {
  const { props, list } = setup()
  let finish
  list.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  render(<ManualWorkflowHistoryPanel {...props} />)
  vi.useFakeTimers()
  await act(async () => click('Load history'))
  await act(async () => vi.advanceTimersByTime(45000))
  expect(list.mock.calls[0][1].signal.aborted).toBe(true)
  expect(screen.getByRole('alert')).toHaveTextContent('Workflow history is unavailable')
  await act(async () => finish({ items: [other], next_cursor: null }))
  expect(screen.queryByRole('list')).toBeNull()
  await act(async () => click('Retry history'))
  expect(screen.getByRole('button', { name: `Open run details ${runId}` })).toBeEnabled()
})
