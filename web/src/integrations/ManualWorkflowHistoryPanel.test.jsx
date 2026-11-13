import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ManualWorkflowHistoryPanel } from './ManualWorkflowHistoryPanel.jsx'
import { createIntegrationsClient } from './client.js'
import { ReadableStream } from 'node:stream/web'
import { TextDecoder, TextEncoder } from 'node:util'

vi.mock('../api.js', () => ({ authHeaders: () => ({ Authorization: 'Bearer current' }), noteUnauthorized: vi.fn() }))

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
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

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

const definition = { schema_version: 1, id: run.workflow_ref.id, version: 1, title: 'Stored orders', trigger: 'manual', authority_mode: 'caller_bound',
  steps: [{ id: 'send', kind: 'action', connection_id: 'orders', connector_ref: run.workflow_ref, operation_ref: run.workflow_ref,
    arguments: { note: 'saved', count: 0, enabled: false, nullable: null } }] }
const action = { connection_id: 'orders', connection_title: 'Orders', connector_ref: run.workflow_ref, operation_ref: run.workflow_ref,
  title: 'Send', form: { submit_label: 'Send', fields: [
    { id: 'note', label: 'Note', kind: 'text', required: true, nullable: false },
    { id: 'count', label: 'Count', kind: 'number', required: true, nullable: false, integer: true },
    { id: 'enabled', label: 'Enabled', kind: 'boolean', required: true, nullable: false },
    { id: 'nullable', label: 'Nullable', kind: 'text', required: false, nullable: true },
  ] } }
const failed = { ...run, revision: 2, state: 'failed' }
const child = { schema_version: 1, run_id: otherId, revision: 2, connection_id: 'orders', connector_ref: run.workflow_ref,
  operation_ref: run.workflow_ref, grant_revision: 1, submission_digest: 'd'.repeat(64), authority_binding_digest: 'e'.repeat(64),
  created_at: run.created_at, updated_at: run.updated_at, state: 'failed', effect_outcome: 'not_applied' }
const receipt = { schema_version: 1, receipt_id: '33333333-3333-4333-8333-333333333333', run_id: otherId, sequence: 1,
  submission_digest: child.submission_digest, operation_ref: child.operation_ref, observed_at: run.updated_at,
  effect_outcome: 'not_applied', failure_code: 'timeout' }
const evidence = { definition, run: failed, steps: [{ run_id: runId, step_id: 'send', state: 'action', action: { run: child, receipts: [receipt] } }] }

function response(value, status = 200) {
  return { status, ok: status >= 200 && status < 300, headers: { get: (key) => key === 'content-type' ? 'application/json' : null },
    body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(JSON.stringify(value))); controller.close() } }) }
}
function storedSetup(snapshot = evidence) {
  vi.stubGlobal('TextEncoder', TextEncoder); vi.stubGlobal('TextDecoder', TextDecoder)
  const { props, controller } = setup()
  const fetchImpl = vi.fn(async (url, options) => {
    if (url.includes('/manual-workflow-runs-v2?')) return response({ items: [failed], next_cursor: null })
    if (url.endsWith(`/manual-workflow-runs-v2/${runId}`)) return response(snapshot)
    if (url.includes('/manual-workflows/orders/versions/1?')) return response(snapshot.definition)
    if (url.includes('/action-catalog?')) return response({ actions: [action], next_cursor: null })
    if (url.endsWith('/manual-workflows') && options.method === 'POST') return response({ ...run.workflow_ref, version: 2, digest: 'f'.repeat(64) })
    throw new Error('Unexpected execution or credential call')
  })
  props.client = createIntegrationsClient({ origin: 'https://ops.example', fetchImpl })
  return { props, controller, fetchImpl }
}
function downloadSetup() {
  const createObjectURL = vi.fn(() => 'blob:workflow-export'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', class extends URL { static createObjectURL = createObjectURL; static revokeObjectURL = revokeObjectURL })
  const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  return { createObjectURL, revokeObjectURL, download }
}
async function readDownload(createObjectURL) {
  const blob = createObjectURL.mock.calls[0][0]
  expect(blob.type).toBe('application/json')
  return JSON.parse(await new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsText(blob)
  }))
}

it('reopens a history entry after reload, edits its saved arguments and posts only a new definition version', async () => {
  const { props, fetchImpl } = storedSetup()
  const first = render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await loaded(); first.unmount()
  // A fresh client and mount have no retained draft or reference cache.
  props.client = createIntegrationsClient({ origin: 'https://ops.example', fetchImpl })
  render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await loaded()
  click(`Open as new version ${runId}`)
  await waitFor(() => expect(screen.getByLabelText('Definition version')).toHaveValue(2))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save workflow definition' })).toBeEnabled())
  expect(screen.getByLabelText('Step 1 Note (required)')).toHaveValue('saved')
  expect(screen.getByLabelText('Step 1 Count (required)')).toHaveValue(0)
  expect(screen.getByLabelText('Step 1 Enabled (required)')).toHaveValue('false')
  expect(screen.getByLabelText('Step 1 Nullable presence')).toHaveValue('null')
  expect(fetchImpl.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true)
  expect(fetchImpl.mock.calls.some(([url]) => url.endsWith(`/manual-workflows/orders/versions/1?digest=${run.workflow_ref.digest}`))).toBe(true)
  fireEvent.change(screen.getByLabelText('Step 1 Note (required)'), { target: { value: 'edited' } })
  click('Save workflow definition')
  await screen.findByRole('region', { name: 'Saved immutable definition' })
  const posts = fetchImpl.mock.calls.filter(([, options]) => options.method === 'POST')
  expect(posts).toHaveLength(1)
  expect(posts[0][0]).toBe(`https://ops.example/api/v1/projects/${runId}/integrations/manual-workflows`)
  expect(JSON.parse(posts[0][1].body)).toEqual({ ...definition, version: 2,
    steps: [{ ...definition.steps[0], arguments: { ...definition.steps[0].arguments, note: 'edited' } }] })
  expect(definition.version).toBe(1)
  expect(definition.steps[0].arguments.note).toBe('saved')
})

it('downloads the exact pinned definition and failed host run and audit receipts using only scoped reads', async () => {
  const { props, fetchImpl } = storedSetup()
  const { createObjectURL, revokeObjectURL, download } = downloadSetup()
  render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await loaded(); click(`Export ${runId}`)
  await waitFor(() => expect(download).toHaveBeenCalledOnce())
  const payload = await readDownload(createObjectURL)
  expect(payload).toEqual({ schema_version: 1, project_id: runId, workflow_ref: failed.workflow_ref, ...evidence })
  expect(payload.steps[0].action.receipts[0]).toEqual(receipt)
  expect(payload.run.state).toBe('failed')
  expect(JSON.stringify(payload)).not.toMatch(/Bearer|Authorization|credential|access_token/)
  expect(fetchImpl.mock.calls.every(([url, options]) => options.method === 'GET' && url.includes(`/projects/${runId}/integrations/`))).toBe(true)
  expect(revokeObjectURL).toHaveBeenCalledWith('blob:workflow-export')
  expect(document.querySelector('a[download]')).toBeNull()
  expect(await screen.findByText(/audit evidence exported/)).toBeInTheDocument()
})

it.each(['reference', 'run', 'revision', 'private-field', 'token', 'credential', 'pinned-arguments'])('refuses an export with %s mismatch or private data without downloading or executing', async (kind) => {
  const snapshot = JSON.parse(JSON.stringify(evidence))
  if (kind === 'reference') snapshot.run.workflow_ref.digest = 'f'.repeat(64)
  if (kind === 'run') { snapshot.run.run_id = otherId; snapshot.steps[0].run_id = otherId }
  if (kind === 'revision') snapshot.run.revision = 1
  if (kind === 'private-field') snapshot.steps[0].action.receipts[0].access_token = 'private'
  if (kind === 'token') snapshot.definition.steps[0].arguments.accessToken = 'private'
  if (kind === 'credential') snapshot.definition.steps[0].arguments.api_key = 'private'
  const { props, fetchImpl } = storedSetup(snapshot)
  if (kind === 'pinned-arguments') vi.spyOn(props.client, 'getManualWorkflowDefinition').mockResolvedValue({ ...definition,
    steps: [{ ...definition.steps[0], arguments: { ...definition.steps[0].arguments, note: 'different' } }] })
  const { createObjectURL, download } = downloadSetup()
  render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await loaded(); click(`Export ${runId}`)
  expect(await screen.findByRole('alert')).toHaveTextContent('Export refused')
  expect(createObjectURL).not.toHaveBeenCalled(); expect(download).not.toHaveBeenCalled()
  expect(document.body.textContent).not.toContain('private')
  expect(fetchImpl.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true)
})

it.each(['Export', 'Open as new version'])('visibly refuses unauthorized %s reads and clears the protected history', async (kind) => {
  const { props, fetchImpl } = storedSetup()
  const method = kind === 'Export' ? 'getManualWorkflowRunV2' : 'getManualWorkflowDefinition'
  vi.spyOn(props.client, method).mockRejectedValue(Object.assign(new Error('private'), { status: 403 }))
  const { download } = downloadSetup()
  render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await loaded(); click(`${kind} ${runId}`)
  expect(await screen.findByRole('alert')).toHaveTextContent('not authorized')
  expect(props.onForbidden).toHaveBeenCalledOnce()
  expect(screen.queryByRole('list')).toBeNull()
  expect(download).not.toHaveBeenCalled()
  expect(fetchImpl.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true)
})

it('suppresses a late export response after switching projects', async () => {
  const { props, fetchImpl } = storedSetup()
  let finish
  const get = vi.spyOn(props.client, 'getManualWorkflowRunV2').mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const { download } = downloadSetup()
  const view = render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await loaded(); click(`Export ${runId}`)
  await waitFor(() => expect(get).toHaveBeenCalledOnce())
  const readSignal = get.mock.calls[0][2].signal
  view.rerender(<ManualWorkflowHistoryPanel {...props} projectId={otherId} />)
  expect(readSignal.aborted).toBe(true)
  await act(async () => finish(evidence))
  expect(download).not.toHaveBeenCalled()
  expect(screen.queryByRole('list')).toBeNull()
  expect(fetchImpl.mock.calls).toHaveLength(1)
})

it('refuses download when authorization is revoked after the final pinned definition read', async () => {
  const { props, fetchImpl } = storedSetup()
  const { download } = downloadSetup()
  render(<ManualWorkflowHistoryPanel {...props} />)
  click('Load history'); await loaded()
  props.guard.mockResolvedValueOnce().mockResolvedValueOnce().mockRejectedValueOnce({ status: 403 })
  click(`Export ${runId}`)
  expect(await screen.findByRole('alert')).toHaveTextContent('not authorized')
  expect(props.onForbidden).toHaveBeenCalledOnce()
  expect(download).not.toHaveBeenCalled()
  expect(fetchImpl.mock.calls).toHaveLength(3)
  expect(fetchImpl.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true)
})
