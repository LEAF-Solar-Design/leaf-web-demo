import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ReadableStream } from 'node:stream/web'
import { TextDecoder, TextEncoder } from 'node:util'
import { ManualWorkflowDefinitionPanel } from './ManualWorkflowDefinitionPanel.jsx'
import { createIntegrationsClient } from './client.js'

vi.mock('../api.js', () => ({ authHeaders: () => ({ Authorization: 'Bearer current' }), noteUnauthorized: vi.fn() }))
const projectId = '11111111-1111-4111-8111-111111111111'
const reference = { id: 'workflow', version: 1, digest: 'a'.repeat(64) }
const entry = { connection_id: 'orders', connection_title: 'Orders', connector_ref: { ...reference, id: 'connector' },
  operation_ref: { ...reference, id: 'send' }, title: '<b>Send order</b>', form: { submit_label: 'Send', description: '<img src=x>', fields: [
    { id: 'note', label: 'Note', kind: 'text', required: true, nullable: false, min_length: 0, max_length: 5 },
    { id: 'count', label: 'Count', kind: 'number', required: true, nullable: false, integer: true, minimum: 0, maximum: 10 },
    { id: 'enabled', label: 'Enabled', kind: 'boolean', required: true, nullable: false },
    { id: 'optional', label: 'Optional', kind: 'text', required: false, nullable: true },
    { id: 'nullable', label: 'Nullable', kind: 'text', required: false, nullable: true },
    { id: 'choice', label: 'Choice', kind: 'choice', required: true, nullable: false, choices: [{ label: 'One', value: 'one' }] },
  ] } }
const other = { ...entry, title: 'Archive', operation_ref: { ...reference, id: 'archive' }, form: { fields: [], submit_label: 'Archive' } }
const catalog = { actions: [entry, other], next_cursor: null }
function response(value, status = 200) {
  return { status, ok: status >= 200 && status < 300, headers: { get: (key) => key === 'content-type' ? 'application/json' : null },
    body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(JSON.stringify(value))); controller.close() } }) }
}
function setup(page = catalog) {
  const controller = new AbortController()
  const fetchImpl = vi.fn(async (url, options) => {
    if (url.includes('/action-catalog?')) return response(page)
    if (url.endsWith('/manual-workflows') && options.method === 'POST') return response(reference)
    throw new Error('Unexpected execution or credential call')
  })
  const client = createIntegrationsClient({ origin: 'https://ops.example', fetchImpl })
  const props = { client, projectId, signal: controller.signal, guard: vi.fn().mockResolvedValue(), onForbidden: vi.fn() }
  return { props, controller, client, fetchImpl }
}
const posts = (fetchImpl) => fetchImpl.mock.calls.filter(([, options]) => options.method === 'POST')
const change = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const save = () => fireEvent.click(screen.getByRole('button', { name: 'Save workflow definition' }))
const key = (action) => JSON.stringify([action.connection_id, action.connector_ref, action.operation_ref])
async function open() {
  fireEvent.click(screen.getByRole('button', { name: 'Open action catalog' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Open action catalog' })).toBeEnabled())
}
async function compose() {
  await open()
  change('Definition title', 'Order workflow'); change('Definition stable ID', 'workflow')
  change('Step 1 action', key(entry)); change('Step 1 Count (required)', '0'); change('Step 1 Choice (required)', 'one')
}
beforeEach(() => { vi.stubGlobal('TextEncoder', TextEncoder); vi.stubGlobal('TextDecoder', TextDecoder) })
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers() })

it('focuses a specific numeric field validation message', async () => {
  const { props } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose(); change('Step 1 Count (required)', '11'); save()
  expect(await screen.findByRole('alert')).toHaveTextContent('Step 1: Enter a number within the allowed range.')
  await waitFor(() => expect(screen.getByLabelText('Step 1 Count (required)')).toHaveFocus())
})

it('copies the saved workflow reference with one click', async () => {
  const writeText = vi.fn().mockResolvedValue()
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  const { props } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose(); save()
  fireEvent.click(await screen.findByRole('button', { name: 'Copy workflow reference' }))
  expect(writeText).toHaveBeenCalledWith(JSON.stringify(reference))
})

it('opens only explicitly, renders catalog text safely and saves typed fixed data without execution', async () => {
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  expect(fetchImpl).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Save workflow definition' })).toBeDisabled()
  await compose()
  change('Step 1 Nullable presence', 'null')
  expect(document.querySelector('img')).toBeNull()
  save()
  await screen.findByRole('region', { name: 'Saved immutable definition' })
  expect(posts(fetchImpl)).toHaveLength(1)
  const definition = JSON.parse(posts(fetchImpl)[0][1].body)
  expect(definition).toMatchObject({ id: 'workflow', version: 1, title: 'Order workflow', trigger: 'manual', authority_mode: 'caller_bound',
    steps: [{ id: 'step-1', connection_id: entry.connection_id, connector_ref: entry.connector_ref, operation_ref: entry.operation_ref,
      arguments: { note: '', count: 0, enabled: false, nullable: null, choice: 'one' } }] })
  expect(definition.steps[0].arguments).not.toHaveProperty('optional')
  expect(fetchImpl.mock.calls.every(([url]) => /action-catalog\?|\/manual-workflows$/.test(url))).toBe(true)
  expect(screen.getByLabelText('Saved workflow digest')).toHaveValue(reference.digest)
  expect(screen.getByLabelText('Saved workflow digest')).toHaveAttribute('readonly')
  expect(screen.getByText('Workflow definition saved. Saving has not started a run.')).toBeInTheDocument()
  expect(document.activeElement).toContainElement(screen.getByRole('region', { name: 'Saved immutable definition' }))
  fireEvent.click(screen.getByRole('button', { name: 'Compose a new definition' }))
  expect(screen.getByLabelText('Definition stable ID')).toHaveValue('')
  expect(screen.getByRole('button', { name: 'Save workflow definition' })).toBeDisabled()
})
it('mounts an inert schedule form only below the saved immutable definition and removes it on composer reset', async () => {
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => projectId) })
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  expect(screen.queryByRole('region', { name: 'Schedule draft' })).toBeNull()
  await compose(); save()
  const definition = await screen.findByRole('region', { name: 'Saved immutable definition' })
  const form = screen.getByRole('region', { name: 'Schedule draft' })
  expect(definition).toContainElement(form)
  expect(form.closest('form')).toBeNull()
  expect(screen.getByLabelText('Draft title')).toHaveValue('Order workflow')
  expect(form).toHaveTextContent('Workflow ID: workflow. Workflow version: 1.')
  expect(posts(fetchImpl)).toHaveLength(1)
  let draft
  const savedRef = { id: `schedule-draft-${projectId}`, version: 1, digest: 'b'.repeat(64) }
  fetchImpl.mockImplementationOnce(async (_, options) => { draft = JSON.parse(options.body); return response(savedRef) })
  fetchImpl.mockImplementationOnce(async () => response(draft))
  fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
  await screen.findByText(/^Draft saved\./)
  expect(draft).toEqual({ schema_version: 1, id: savedRef.id, version: 1, title: 'Order workflow', workflow_ref: reference,
    schedule: { kind: 'daily', timezone: 'UTC', hour: 9, minute: 0,
      gap_policy: 'next_valid_minute', fold_policy: 'first_occurrence' }, state: 'draft' })
  const base = `https://ops.example/api/v1/projects/${projectId}/integrations/manual-workflow-schedule-drafts`
  expect(fetchImpl.mock.calls.slice(-2).map(([url, options]) => [url, options.method])).toEqual([
    [base, 'POST'], [`${base}/${savedRef.id}/versions/1?digest=${savedRef.digest}`, 'GET'],
  ])
  fireEvent.click(screen.getByRole('button', { name: 'Compose a new definition' }))
  expect(screen.queryByRole('region', { name: 'Schedule draft' })).toBeNull()
})
it('composer reset aborts a pending draft save and ignores its late acknowledgement', async () => {
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => projectId) })
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose(); save(); await screen.findByRole('region', { name: 'Saved immutable definition' })
  let finish, draftSignal
  fetchImpl.mockImplementationOnce((_, options) => {
    draftSignal = options.signal
    return new Promise((resolve) => { finish = resolve })
  })
  fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  fireEvent.click(screen.getByRole('button', { name: 'Compose a new definition' }))
  expect(draftSignal.aborted).toBe(true)
  await act(async () => finish(response({ id: `schedule-draft-${projectId}`, version: 1, digest: 'b'.repeat(64) })))
  expect(screen.queryByRole('region', { name: 'Schedule draft' })).toBeNull()
  expect(fetchImpl.mock.calls.filter(([url, options]) => url.includes('manual-workflow-schedule-drafts') && options.method === 'GET')).toHaveLength(0)
})
it('honestly handles an empty catalog and bounds explicit pagination with retained selections', async () => {
  const { props, fetchImpl } = setup({ actions: [], next_cursor: 'page_2' })
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await open()
  expect(screen.getByText(/No actions are available/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save workflow definition' })).toBeDisabled()
  expect(fetchImpl).toHaveBeenCalledOnce()
  fetchImpl.mockResolvedValueOnce(response({ actions: [other], next_cursor: 'page_2' }))
  fireEvent.click(screen.getByRole('button', { name: 'Next action catalog page' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Open action catalog' })).toBeEnabled())
  expect(fetchImpl.mock.calls[1][0]).toContain('limit=25&after=page_2')
  expect(screen.getByRole('button', { name: 'Next action catalog page' })).toBeDisabled()
  change('Step 1 action', key(other))
  fetchImpl.mockResolvedValueOnce(response({ actions: [entry], next_cursor: null }))
  await open()
  expect(screen.getByLabelText('Step 1 action')).toHaveValue(key(other))
})
it('restarts pagination after a successful catalog reload while retaining the draft', async () => {
  const { props, fetchImpl } = setup({ actions: [entry], next_cursor: 'page_2' })
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose()
  change('Definition version', '3'); change('Step 1 Note (required)', 'keep')
  const next = () => screen.getByRole('button', { name: 'Next action catalog page' })
  const pageTwo = () => response({ actions: [other], next_cursor: 'page_2' })
  fetchImpl.mockResolvedValueOnce(pageTwo())
  fireEvent.click(next())
  await waitFor(() => expect(screen.getByRole('button', { name: 'Open action catalog' })).toBeEnabled())
  expect(next()).toBeDisabled()
  fetchImpl.mockRejectedValueOnce(new Error('offline'))
  await open()
  expect(next()).toBeDisabled()
  await open()
  expect(next()).toBeEnabled()
  expect(screen.getByLabelText('Definition title')).toHaveValue('Order workflow')
  expect(screen.getByLabelText('Definition stable ID')).toHaveValue('workflow')
  expect(screen.getByLabelText('Definition version')).toHaveValue(3)
  expect(screen.getByLabelText('Step 1 action')).toHaveValue(key(entry))
  expect(screen.getByLabelText('Step 1 Note (required)')).toHaveValue('keep')
  expect(screen.getByLabelText('Step 1 Count (required)')).toHaveValue(0)
  fetchImpl.mockResolvedValueOnce(pageTwo())
  fireEvent.click(next())
  await waitFor(() => expect(screen.getByRole('button', { name: 'Open action catalog' })).toBeEnabled())
  expect(fetchImpl.mock.calls.at(-1)[0]).toContain('limit=25&after=page_2')
  expect(next()).toBeDisabled()
  const calls = fetchImpl.mock.calls.length
  fireEvent.click(next())
  expect(fetchImpl).toHaveBeenCalledTimes(calls)
  expect(posts(fetchImpl)).toHaveLength(0)
})
it.each([
  ['Definition stable ID', 'Bad ID'], ['Definition title', ''], ['Definition version', '0'], ['Definition version', '1.5'],
  ['Definition version', '9007199254740992'], ['Step 1 stable ID', 'BAD'], ['Step 1 Count (required)', ''],
  ['Step 1 Count (required)', '-1'], ['Step 1 Count (required)', '11'], ['Step 1 Count (required)', '1.5'],
  ['Step 1 Note (required)', 'longer'], ['Step 1 Choice (required)', ''],
])('validates %s = %s before writes and keeps editing available', async (label, value) => {
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose(); change(label, value); save()
  await screen.findByRole('alert')
  expect(posts(fetchImpl)).toHaveLength(0)
  expect(screen.getByLabelText('Definition stable ID')).toBeEnabled()
})
it('validates required text minimum and missing actions', async () => {
  const required = { ...other, form: { fields: [{ id: 'text', label: 'Required text', kind: 'text', required: true, nullable: false, min_length: 2 }], submit_label: 'Send' } }
  const { props, fetchImpl } = setup({ actions: [required], next_cursor: null })
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await open(); change('Definition title', 'Test'); change('Definition stable ID', 'workflow'); save()
  await screen.findByRole('alert')
  change('Step 1 action', key(required)); change('Step 1 Required text (required)', 'x'); save()
  await screen.findByRole('alert')
  expect(posts(fetchImpl)).toHaveLength(0)
})
it('orders steps, rejects duplicate stable IDs, limits to eight and supports removal', async () => {
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose()
  fireEvent.click(screen.getByRole('button', { name: 'Add step' }))
  change('Step 2 action', key(other)); change('Step 2 stable ID', 'step-1'); save()
  await screen.findByRole('alert')
  expect(posts(fetchImpl)).toHaveLength(0)
  change('Step 2 stable ID', 'archive')
  fireEvent.click(screen.getByRole('button', { name: 'Move step 2 up' }))
  expect(screen.getByLabelText('Step 1 stable ID')).toHaveValue('archive')
  for (let count = 2; count < 8; count++) fireEvent.click(screen.getByRole('button', { name: 'Add step' }))
  expect(screen.getByRole('button', { name: 'Add step' })).toBeDisabled()
  for (let count = 8; count > 2; count--) fireEvent.click(screen.getByRole('button', { name: `Remove step ${count}` }))
  save(); await screen.findByRole('region', { name: 'Saved immutable definition' })
  expect(JSON.parse(posts(fetchImpl)[0][1].body).steps.map((step) => step.id)).toEqual(['archive', 'step-1'])
})
it('locks an ambiguous save and retries the exact serialized definition only explicitly', async () => {
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose()
  fetchImpl.mockRejectedValueOnce(new Error('private failure'))
  save()
  const retry = await screen.findByRole('button', { name: 'Retry identical workflow definition' })
  expect(screen.getByLabelText('Definition version')).toBeDisabled()
  expect(posts(fetchImpl)).toHaveLength(1)
  const body = posts(fetchImpl)[0][1].body
  fireEvent.click(retry)
  await screen.findByRole('region', { name: 'Saved immutable definition' })
  expect(posts(fetchImpl)).toHaveLength(2)
  expect(posts(fetchImpl)[1][1].body).toBe(body)
  expect(document.body.textContent).not.toContain('private failure')
})
it.each([{ ...reference, id: 'other' }, { reference }, { ...reference, digest: 'bad' }])('keeps malformed/mismatched success ambiguous: %j', async (value) => {
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose(); fetchImpl.mockResolvedValueOnce(response(value)); save()
  await screen.findByRole('button', { name: 'Retry identical workflow definition' })
  expect(screen.queryByRole('region', { name: 'Saved immutable definition' })).toBeNull()
})
it('retains conflicting input for deliberate version correction', async () => {
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose(); fetchImpl.mockResolvedValueOnce(response({}, 409)); save()
  await screen.findByText(/This immutable ID and version conflict/)
  expect(screen.getByLabelText('Definition title')).toHaveValue('Order workflow')
  expect(screen.getByLabelText('Step 1 Count (required)')).toHaveValue(0)
  expect(screen.queryByRole('button', { name: 'Retry identical workflow definition' })).toBeNull()
  change('Definition version', '2'); fetchImpl.mockResolvedValueOnce(response({ ...reference, version: 2 })); save()
  await screen.findByRole('region', { name: 'Saved immutable definition' })
  expect(JSON.parse(posts(fetchImpl)[1][1].body).version).toBe(2)
})
it('preserves an original ambiguous intent when recovery fails before dispatch', async () => {
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose(); fetchImpl.mockRejectedValueOnce(new Error('lost response')); save()
  const retry = await screen.findByRole('button', { name: 'Retry identical workflow definition' })
  const body = posts(fetchImpl)[0][1].body
  props.guard.mockRejectedValueOnce(Object.assign(new Error('scope unavailable'), { dispatched: false }))
  fireEvent.click(retry)
  await waitFor(() => expect(retry).toBeEnabled())
  expect(posts(fetchImpl)).toHaveLength(1)
  expect(screen.getByLabelText('Definition title')).toBeDisabled()
  fireEvent.click(retry)
  await screen.findByRole('region', { name: 'Saved immutable definition' })
  expect(posts(fetchImpl)[1][1].body).toBe(body)
})
it('keeps a save ambiguous if the acceptance scope check fails without a dispatch', async () => {
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose()
  props.guard.mockResolvedValueOnce().mockRejectedValueOnce(Object.assign(new Error('scope unavailable'), { dispatched: false }))
  save()
  await screen.findByRole('button', { name: 'Retry identical workflow definition' })
  expect(posts(fetchImpl)).toHaveLength(1)
  expect(screen.queryByRole('region', { name: 'Saved immutable definition' })).toBeNull()
  expect(screen.getByLabelText('Definition title')).toBeDisabled()
})
it('blocks duplicate clicks while the scope check or write is pending', async () => {
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose()
  let finishGuard, finishWrite
  props.guard.mockImplementationOnce(() => new Promise((resolve) => { finishGuard = resolve }))
  fetchImpl.mockImplementationOnce(() => new Promise((resolve) => { finishWrite = resolve }))
  save(); save()
  expect(posts(fetchImpl)).toHaveLength(0)
  await act(async () => finishGuard())
  expect(posts(fetchImpl)).toHaveLength(1)
  save()
  await act(async () => finishWrite(response(reference)))
  await screen.findByRole('region', { name: 'Saved immutable definition' })
  expect(posts(fetchImpl)).toHaveLength(1)
})
it.each([401, 403])('clears protected draft and invokes forbidden on HTTP %s', async (status) => {
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose(); fetchImpl.mockResolvedValueOnce(response({}, status)); save()
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
  expect(screen.getByLabelText('Definition stable ID')).toHaveValue('')
  expect(screen.queryByRole('button', { name: 'Retry identical workflow definition' })).toBeNull()
})
it('clears a returned reference when the final scope guard forbids acceptance', async () => {
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose()
  props.guard.mockResolvedValueOnce().mockRejectedValueOnce({ status: 403 })
  save()
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
  expect(posts(fetchImpl)).toHaveLength(1)
  expect(screen.queryByRole('region', { name: 'Saved immutable definition' })).toBeNull()
  expect(screen.getByLabelText('Definition title')).toHaveValue('')
})
it('clears an ambiguous original intent when the binding signal changes', async () => {
  const { props, fetchImpl } = setup()
  const { rerender } = render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose(); fetchImpl.mockRejectedValueOnce(new Error('lost')); save()
  await screen.findByRole('button', { name: 'Retry identical workflow definition' })
  rerender(<ManualWorkflowDefinitionPanel {...props} signal={new AbortController().signal} />)
  expect(screen.getByLabelText('Definition stable ID')).toHaveValue('')
  expect(screen.queryByRole('button', { name: 'Retry identical workflow definition' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Save workflow definition' })).toBeDisabled()
})
it.each(['catalog', 'save', 'preflight', 'completion'])('suppresses late %s continuations after context abort', async (kind) => {
  const { props, fetchImpl, controller } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  if (kind !== 'catalog') await compose()
  let finish
  if (kind === 'preflight') props.guard.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  else if (kind === 'completion') props.guard.mockResolvedValueOnce().mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  else fetchImpl.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  if (kind === 'catalog') fireEvent.click(screen.getByRole('button', { name: 'Open action catalog' }))
  else save()
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const lastSignal = fetchImpl.mock.calls.at(-1)?.[1].signal
  act(() => controller.abort())
  if (kind !== 'preflight') expect(lastSignal.aborted).toBe(true)
  await act(async () => finish(kind === 'catalog' ? response(catalog) : kind === 'save' ? response(reference) : undefined))
  expect(screen.getByLabelText('Definition stable ID')).toHaveValue('')
  expect(screen.queryByRole('region', { name: 'Saved immutable definition' })).toBeNull()
  if (kind === 'preflight') expect(posts(fetchImpl)).toHaveLength(0)
})
it('bounds a hung save, aborts I/O and ignores a late response during identical recovery', async () => {
  const { props, fetchImpl } = setup()
  render(<ManualWorkflowDefinitionPanel {...props} />)
  await compose()
  let finish
  fetchImpl.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  vi.useFakeTimers()
  await act(async () => save())
  const original = posts(fetchImpl)[0][1]
  await act(async () => vi.advanceTimersByTime(45000))
  expect(original.signal.aborted).toBe(true)
  expect(screen.getByRole('button', { name: 'Retry identical workflow definition' })).toBeEnabled()
  await act(async () => finish(response(reference)))
  expect(screen.queryByRole('region', { name: 'Saved immutable definition' })).toBeNull()
  vi.useRealTimers()
  fireEvent.click(screen.getByRole('button', { name: 'Retry identical workflow definition' }))
  await screen.findByRole('region', { name: 'Saved immutable definition' })
  expect(posts(fetchImpl)[1][1].body).toBe(original.body)
})
