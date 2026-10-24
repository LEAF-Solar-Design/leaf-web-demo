import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { TextEncoder } from 'node:util'
import { ActionDefinitionPanel } from './ActionDefinitionPanel.jsx'

const projectId = '11111111-1111-4111-8111-111111111111'
const reference = { id: 'connector', version: 1, digest: 'a'.repeat(64) }
const entry = { connection_id: 'orders', connection_title: 'Orders', connector_ref: reference,
  operation_ref: { ...reference, id: 'send' }, title: '<b>Send order</b>',
  form: { fields: [{ id: 'note', label: 'Order note', required: true, nullable: false, kind: 'text' }],
    submit_label: 'Send', description: '<img src=x>' } }
const recipe = { schema_version: 1, id: 'order-form', title: 'Order form', kind: 'integration.action',
  connection_id: entry.connection_id, connector_ref: entry.connector_ref, operation_ref: entry.operation_ref, form: entry.form }
const record = { element: recipe, revision: 1 }
const change = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const click = (name) => fireEvent.click(screen.getByRole('button', { name, exact: true }))
function setup(page = { actions: [entry], next_cursor: null }) {
  const controller = new AbortController()
  const client = { getActionCatalog: vi.fn().mockResolvedValue(page), saveElement: vi.fn().mockResolvedValue(record),
    getElement: vi.fn().mockResolvedValue(record), submitAction: vi.fn(), reconcileAction: vi.fn(), submitManualWorkflow: vi.fn() }
  const props = { client, projectId, signal: controller.signal, guard: vi.fn().mockResolvedValue(), onForbidden: vi.fn(), onSaved: vi.fn() }
  return { props, controller, client }
}
async function load() {
  click('Load permitted actions')
  await waitFor(() => expect(screen.getByRole('button', { name: 'Load permitted actions' })).toBeEnabled())
}
async function compose() {
  await load()
  change('Form title', ' Order form '); change('Form stable ID', 'order-form')
  change('Permitted action', screen.getByRole('option', { name: /Send order/ }).value)
}
const save = () => click('Save action form')
beforeEach(() => vi.stubGlobal('TextEncoder', TextEncoder))
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers() })

it('explicitly loads plain catalog text and creates the exact inert recipe with no replacement revision', async () => {
  const { props, client } = setup()
  render(<ActionDefinitionPanel {...props} />)
  expect(client.getActionCatalog).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Save action form' })).toBeDisabled()
  await compose()
  expect(screen.getByText('<img src=x>')).toBeInTheDocument()
  expect(document.querySelector('img, b')).toBeNull()
  expect(screen.queryByLabelText('Order note')).toBeNull()
  save()
  await screen.findByText('Action form saved. No action was submitted.')
  expect(client.saveElement).toHaveBeenCalledExactlyOnceWith(projectId, recipe, { signal: expect.any(AbortSignal) })
  expect(props.onSaved).toHaveBeenCalledExactlyOnceWith(record)
  expect(props.guard).toHaveBeenCalledTimes(4)
  expect(document.activeElement).toContainElement(screen.getByText('Action form saved. No action was submitted.'))
  expect(client.submitAction).not.toHaveBeenCalled()
  expect(client.reconcileAction).not.toHaveBeenCalled()
  expect(client.submitManualWorkflow).not.toHaveBeenCalled()
})

it('retains the exact selected entry across empty and changed-operation pages and stops cyclic cursors', async () => {
  const { props, client } = setup({ actions: [entry], next_cursor: 'two' })
  client.getActionCatalog.mockResolvedValueOnce({ actions: [entry], next_cursor: 'two' })
    .mockResolvedValueOnce({ actions: [], next_cursor: 'three' })
    .mockResolvedValueOnce({ actions: [{ ...entry, form: { fields: [], submit_label: 'Changed' } }], next_cursor: 'two' })
  render(<ActionDefinitionPanel {...props} />)
  await compose()
  const selected = screen.getByLabelText('Permitted action').value
  click('Next permitted actions page')
  await screen.findByText('No permitted actions are available on this catalog page.')
  expect(screen.getByLabelText('Permitted action')).toHaveValue(selected)
  expect(screen.getByRole('button', { name: 'Save action form' })).toBeEnabled()
  click('Next permitted actions page')
  await screen.findByText('Catalog navigation stopped at a repeated cursor or the page limit.')
  expect(screen.getByRole('button', { name: 'Next permitted actions page' })).toBeDisabled()
  expect(screen.getByLabelText('Permitted action')).toHaveValue(selected)
  save()
  await waitFor(() => expect(props.onSaved).toHaveBeenCalled())
  expect(client.saveElement.mock.calls[0][1]).toEqual(recipe)
  expect(client.getActionCatalog.mock.calls.map(([, options]) => options.after)).toEqual([undefined, 'two', 'three'])
})

it('bounds even noncyclic pagination to twenty next-page reads', async () => {
  const { props, client } = setup()
  client.getActionCatalog.mockImplementation(async () => ({ actions: [], next_cursor: `page_${client.getActionCatalog.mock.calls.length}` }))
  render(<ActionDefinitionPanel {...props} />)
  await load()
  for (let index = 0; index < 20; index += 1) {
    click('Next permitted actions page')
    await waitFor(() => expect(client.getActionCatalog).toHaveBeenCalledTimes(index + 2))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Load permitted actions' })).toBeEnabled())
  }
  expect(screen.getByRole('button', { name: 'Next permitted actions page' })).toBeDisabled()
  expect(client.getActionCatalog).toHaveBeenCalledTimes(21)
})

it.each([{ actions: Array(51).fill(entry), next_cursor: null }, { actions: [entry], next_cursor: '../bad' },
  { actions: [{ ...entry, form: { ...entry.form, transport: 'unreviewed' } }], next_cursor: null }])('rejects malformed or unbounded catalog pages', async (page) => {
  const { props, client } = setup(page)
  render(<ActionDefinitionPanel {...props} />)
  await load()
  expect(screen.getByRole('alert')).toHaveTextContent('catalog is unavailable')
  expect(screen.queryByText('No permitted actions are available on this catalog page.')).toBeNull()
  expect(client.saveElement).not.toHaveBeenCalled()
})

it('keeps pre-dispatch errors separate and retains the editable draft', async () => {
  const { props, client } = setup()
  render(<ActionDefinitionPanel {...props} />)
  await compose(); change('Form stable ID', 'INVALID ID'); save()
  await screen.findByText(/The form was not saved/)
  expect(client.saveElement).not.toHaveBeenCalled()
  expect(screen.getByLabelText('Form title')).toHaveValue(' Order form ')
  expect(screen.queryByRole('button', { name: 'Check saved form' })).toBeNull()
  change('Form stable ID', 'order-form')
  client.saveElement.mockRejectedValueOnce({ dispatched: false })
  save()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save action form' })).toBeEnabled())
  expect(screen.getByLabelText('Form stable ID')).toHaveValue('order-form')
  expect(screen.queryByRole('button', { name: 'Check saved form' })).toBeNull()
})

it.each(['lost', 'conflict', 'malformed', 'mismatch'])('recovers %s saves only by explicit exact canonical readback', async (failure) => {
  const { props, client } = setup()
  if (failure === 'lost') client.saveElement.mockRejectedValueOnce(new Error('lost'))
  if (failure === 'conflict') client.saveElement.mockRejectedValueOnce({ status: 409 })
  if (failure === 'malformed') client.saveElement.mockResolvedValueOnce({ element: { ...recipe, execute: true }, revision: 1 })
  if (failure === 'mismatch') client.saveElement.mockResolvedValueOnce({ element: { ...recipe, title: 'Different' }, revision: 1 })
  render(<ActionDefinitionPanel {...props} />)
  await compose(); save()
  await screen.findByRole('button', { name: 'Check saved form' })
  expect(props.onSaved).not.toHaveBeenCalled()
  expect(client.getElement).not.toHaveBeenCalled()
  expect(screen.getByLabelText('Form stable ID')).toBeDisabled()
  click('Check saved form')
  await screen.findByText('Action form saved. No action was submitted.')
  expect(client.getElement).toHaveBeenCalledExactlyOnceWith(projectId, 'order-form', { signal: expect.any(AbortSignal) })
  expect(client.saveElement).toHaveBeenCalledTimes(1)
  expect(props.onSaved).toHaveBeenCalledExactlyOnceWith(record)
})

it('never accepts or overwrites a different existing recipe', async () => {
  const { props, client } = setup()
  client.saveElement.mockRejectedValueOnce({ status: 409 })
  client.getElement.mockResolvedValue({ element: { ...recipe, form: { ...entry.form, fields: [] } }, revision: 2 })
  render(<ActionDefinitionPanel {...props} />)
  await compose(); save()
  click((await screen.findByRole('button', { name: 'Check saved form' })).textContent)
  await screen.findByText(/A different recipe exists/)
  expect(props.onSaved).not.toHaveBeenCalled()
  expect(client.saveElement).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('button', { name: 'Retry identical action form' })).toBeNull()
  expect(screen.getByLabelText('Form title')).toHaveValue(' Order form ')
})

it('permits an explicit identical create retry only after a guarded not-found readback', async () => {
  const { props, client } = setup()
  client.saveElement.mockRejectedValueOnce(new Error('lost'))
  client.getElement.mockRejectedValueOnce({ status: 404 })
  render(<ActionDefinitionPanel {...props} />)
  await compose(); save()
  await screen.findByRole('button', { name: 'Check saved form' })
  click('Check saved form')
  await screen.findByRole('button', { name: 'Retry identical action form' })
  expect(client.saveElement).toHaveBeenCalledTimes(1)
  click('Retry identical action form')
  await screen.findByText('Action form saved. No action was submitted.')
  expect(client.saveElement).toHaveBeenCalledTimes(2)
  expect(client.saveElement.mock.calls[1][1]).toEqual(client.saveElement.mock.calls[0][1])
})

it('blocks double saves and ignores a late result after the bounded timeout', async () => {
  const { props, client } = setup()
  let finish
  client.saveElement.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  render(<ActionDefinitionPanel {...props} />)
  await compose()
  vi.useFakeTimers()
  save(); save()
  await act(async () => {})
  expect(client.saveElement).toHaveBeenCalledTimes(1)
  await act(async () => vi.advanceTimersByTimeAsync(45000))
  expect(screen.getByRole('button', { name: 'Check saved form' })).toBeEnabled()
  expect(client.saveElement.mock.calls[0][2].signal.aborted).toBe(true)
  await act(async () => finish(record))
  expect(props.onSaved).not.toHaveBeenCalled()
  expect(screen.getByLabelText('Form stable ID')).toHaveValue('order-form')
})

it.each(['abort', 'project', 'client', 'unmount'])('cancels and discards draft, intent and late saves on %s lifetime change', async (kind) => {
  const { props, client, controller } = setup()
  let finish
  client.saveElement.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const { rerender, unmount } = render(<ActionDefinitionPanel {...props} />)
  await compose(); save()
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const signal = client.saveElement.mock.calls[0][2].signal
  if (kind === 'abort') act(() => controller.abort())
  else if (kind === 'unmount') unmount()
  else rerender(<ActionDefinitionPanel {...props} {...(kind === 'project' ? { projectId: 'other' } : { client: { ...client } })} />)
  expect(signal.aborted).toBe(true)
  await act(async () => finish(record))
  expect(props.onSaved).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: 'Check saved form' })).toBeNull()
  if (kind !== 'unmount') expect(screen.getByLabelText('Form stable ID')).toHaveValue('')
})

it.each(['catalog', 'save', 'check', 'guard-before', 'guard-after'])('handles forbidden %s through the host and clears protected drafts', async (kind) => {
  const { props, client } = setup()
  render(<ActionDefinitionPanel {...props} />)
  if (kind === 'catalog') {
    client.getActionCatalog.mockRejectedValueOnce({ status: 403 }); click('Load permitted actions')
  } else {
    await compose()
    if (kind === 'guard-before') props.guard.mockRejectedValueOnce({ status: 403 })
    if (kind === 'guard-after') props.guard.mockResolvedValueOnce().mockRejectedValueOnce({ status: 403 })
    if (kind === 'save') client.saveElement.mockRejectedValueOnce({ status: 403 })
    if (kind === 'check') {
      client.saveElement.mockRejectedValueOnce(new Error('lost')); client.getElement.mockRejectedValueOnce({ status: 403 })
    }
    save()
    if (kind === 'check') { await screen.findByRole('button', { name: 'Check saved form' }); click('Check saved form') }
  }
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledTimes(1))
  expect(props.onSaved).not.toHaveBeenCalled()
  expect(screen.getByLabelText('Form title')).toHaveValue('')
  if (kind === 'guard-before') expect(client.saveElement).not.toHaveBeenCalled()
})
