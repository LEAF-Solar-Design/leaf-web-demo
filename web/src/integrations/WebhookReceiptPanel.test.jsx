import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { WebhookReceiptPanel } from './WebhookReceiptPanel.jsx'

const projectId = '11111111-1111-4111-8111-111111111111'
const receipt = { registrationId: 'Forge.Reg_1', registrationRevision: 2, connectionId: 'Forge.Conn_1', provider: 'forgejo',
  deliveryId: 'Delivery:AbC-1', payloadSha256: 'a'.repeat(64), byteLength: 123, connectionRevision: 3,
  createdBy: '22222222-2222-4222-8222-222222222222', createdAt: '2026-10-23 09:30:00+00' }
function setup() {
  const controller = new AbortController()
  const props = { client: { getWebhookReceipt: vi.fn().mockResolvedValue(receipt) }, projectId,
    signal: controller.signal, guard: vi.fn().mockResolvedValue(), onForbidden: vi.fn() }
  return { props, controller }
}
const change = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
function fill() { change('Registration ID', receipt.registrationId); change('Delivery ID', receipt.deliveryId) }
const submit = () => fireEvent.submit(screen.getByRole('form', { name: 'Look up webhook receipt' }))
const evidence = () => screen.queryByRole('region', { name: 'Stored admission evidence' })
const ready = () => screen.findByRole('region', { name: 'Stored admission evidence' })
beforeEach(() => vi.useRealTimers())
afterEach(() => { cleanup(); vi.useRealTimers() })

it('looks up only explicitly, checks scope twice, focuses feedback and renders every public field verbatim', async () => {
  const { props } = setup()
  render(<WebhookReceiptPanel {...props} />)
  expect(document.activeElement).toBe(document.body)
  expect(screen.getByLabelText('Registration ID')).toHaveValue('')
  expect(screen.getByLabelText('Delivery ID')).toHaveAttribute('maxlength', '128')
  fill()
  expect(props.client.getWebhookReceipt).not.toHaveBeenCalled()
  expect(props.guard).not.toHaveBeenCalled()
  submit()
  const region = await ready()
  expect(props.client.getWebhookReceipt).toHaveBeenCalledOnce()
  expect(props.client.getWebhookReceipt).toHaveBeenCalledWith(projectId, receipt.registrationId, receipt.deliveryId, { signal: expect.any(AbortSignal) })
  expect(props.guard).toHaveBeenCalledTimes(2)
  for (const value of Object.values(receipt)) expect(region).toHaveTextContent(String(value))
  expect(document.activeElement).toContainElement(region)
  expect(screen.getByText(/does not prove event identity, provider execution or exactly-once side effects/)).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Webhook admission receipt' })).toHaveAttribute('aria-busy', 'false')
  change('Delivery ID', 'Other:delivery')
  expect(evidence()).toBeNull()
  expect(props.client.getWebhookReceipt).toHaveBeenCalledOnce()
})
it.each([['Registration ID', 'a:b'], ['Registration ID', 'x'.repeat(129)], ['Delivery ID', 'a/b'], ['Delivery ID', 'a%3Ab'], ['Delivery ID', '']])('validates %s before scope or transport', async (label, value) => {
  const { props } = setup(); render(<WebhookReceiptPanel {...props} />)
  fill(); change(label, value); submit()
  const error = await screen.findByRole('alert')
  expect(document.activeElement).toContainElement(error)
  expect(screen.getByLabelText(label)).toHaveValue(value)
  expect(props.guard).not.toHaveBeenCalled()
  expect(props.client.getWebhookReceipt).not.toHaveBeenCalled()
})
it('blocks synchronous duplicate form submits through both scope checks and transport', async () => {
  const { props } = setup()
  let before, request, after
  props.guard.mockImplementationOnce(() => new Promise((resolve) => { before = resolve }))
    .mockImplementationOnce(() => new Promise((resolve) => { after = resolve }))
  props.client.getWebhookReceipt.mockImplementationOnce(() => new Promise((resolve) => { request = resolve }))
  render(<WebhookReceiptPanel {...props} />); fill(); submit(); submit()
  expect(props.guard).toHaveBeenCalledOnce()
  expect(screen.getByRole('region', { name: 'Webhook admission receipt' })).toHaveAttribute('aria-busy', 'true')
  await act(async () => before()); submit()
  expect(props.client.getWebhookReceipt).toHaveBeenCalledOnce()
  await act(async () => request(receipt)); submit()
  expect(evidence()).toBeNull()
  expect(props.guard).toHaveBeenCalledTimes(2)
  await act(async () => after()); await ready()
})
it.each(['Registration ID', 'Delivery ID'])('editing %s cancels pending work and suppresses late completion', async (label) => {
  const { props } = setup()
  let finish
  props.client.getWebhookReceipt.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  render(<WebhookReceiptPanel {...props} />); fill(); submit()
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const signal = props.client.getWebhookReceipt.mock.calls[0][3].signal
  change(label, 'Changed')
  expect(signal.aborted).toBe(true)
  expect(screen.getByRole('button', { name: 'Look up receipt' })).toBeEnabled()
  await act(async () => finish(receipt))
  expect(evidence()).toBeNull()
  expect(props.guard).toHaveBeenCalledOnce()
  expect(screen.queryByRole('alert')).toBeNull()
})
it.each(['preflight', 'completion'])('editing inputs invalidates a late %s guard before evidence can appear', async (phase) => {
  const { props } = setup()
  let finish
  const stall = () => new Promise((resolve) => { finish = resolve })
  if (phase === 'preflight') props.guard.mockImplementationOnce(stall)
  else props.guard.mockResolvedValueOnce().mockImplementationOnce(stall)
  render(<WebhookReceiptPanel {...props} />); fill(); submit()
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const oldSignal = props.guard.mock.calls.at(-1)[0]
  change('Delivery ID', 'Changed:ID')
  expect(oldSignal.aborted).toBe(true)
  await act(async () => finish())
  expect(evidence()).toBeNull()
  expect(props.client.getWebhookReceipt).toHaveBeenCalledTimes(phase === 'preflight' ? 0 : 1)
  expect(screen.getByLabelText('Delivery ID')).toHaveValue('Changed:ID')
  expect(screen.queryByRole('alert')).toBeNull()
})
it.each([404, 409, 503])('clears old evidence on failed %s refresh, retains IDs and permits explicit retry', async (status) => {
  const { props } = setup(); render(<WebhookReceiptPanel {...props} />); fill(); submit(); await ready()
  props.client.getWebhookReceipt.mockRejectedValueOnce({ status, message: 'private payload' })
  submit(); expect(evidence()).toBeNull()
  const error = await screen.findByRole('alert')
  expect(error).toHaveTextContent(status === 404 ? 'No accessible receipt was returned for these IDs.' : 'Receipt lookup is unavailable. Please retry using Look up receipt.')
  expect(document.body.textContent).not.toContain('private payload')
  expect(screen.getByLabelText('Registration ID')).toHaveValue(receipt.registrationId)
  expect(screen.getByLabelText('Delivery ID')).toHaveValue(receipt.deliveryId)
  submit(); await ready()
  expect(props.client.getWebhookReceipt).toHaveBeenCalledTimes(3)
})
it.each(['preflight', 'request', 'completion'])('times out a stalled %s and isolates late completion from retry', async (phase) => {
  vi.useFakeTimers()
  const { props } = setup()
  let finish, retry
  const stall = () => new Promise((resolve) => { finish = resolve })
  if (phase === 'preflight') props.guard.mockImplementationOnce(stall)
  else if (phase === 'completion') props.guard.mockResolvedValueOnce().mockImplementationOnce(stall)
  else props.client.getWebhookReceipt.mockImplementationOnce(stall)
  render(<WebhookReceiptPanel {...props} />); fill()
  await act(async () => submit())
  await act(async () => vi.advanceTimersByTimeAsync(44999))
  expect(screen.queryByRole('alert')).toBeNull()
  await act(async () => vi.advanceTimersByTimeAsync(1))
  expect(screen.getByRole('alert')).toHaveTextContent('Receipt lookup is unavailable.')
  expect(screen.getByLabelText('Delivery ID')).toHaveValue(receipt.deliveryId)
  expect(screen.getByRole('button', { name: 'Look up receipt' })).toBeEnabled()
  props.client.getWebhookReceipt.mockImplementationOnce(() => new Promise((resolve) => { retry = resolve }))
  await act(async () => submit())
  const retrySignal = props.client.getWebhookReceipt.mock.calls.at(-1)[3].signal
  await act(async () => finish(phase === 'request' ? receipt : undefined))
  expect(retrySignal.aborted).toBe(false)
  expect(evidence()).toBeNull()
  expect(screen.getByText('Looking up receipt…')).toBeInTheDocument()
  await act(async () => retry(receipt))
  expect(evidence()).toBeInTheDocument()
})
it.each([null, { ...receipt, provider: 'invented' }, { ...receipt, extra: 'private' }, { ...receipt, deliveryId: 'other' }])('validates injected receipt before display: %j', async (value) => {
  const { props } = setup(); props.client.getWebhookReceipt.mockResolvedValueOnce(value)
  render(<WebhookReceiptPanel {...props} />); fill(); submit()
  await screen.findByRole('alert'); expect(evidence()).toBeNull()
})
it.each([401, 403])('clears evidence and follows authority loss for %s at either guard or transport', async (status) => {
  for (const phase of ['preflight', 'request', 'completion']) {
    const { props } = setup(); const view = render(<WebhookReceiptPanel {...props} />)
    fill(); submit(); await ready()
    if (phase === 'request') props.client.getWebhookReceipt.mockRejectedValueOnce({ status })
    else if (phase === 'preflight') props.guard.mockRejectedValueOnce({ status })
    else props.guard.mockResolvedValueOnce().mockRejectedValueOnce({ status })
    submit(); await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
    expect(evidence()).toBeNull()
    expect(screen.getByLabelText('Delivery ID')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Look up receipt' })).toBeDisabled()
    view.unmount()
  }
})
it.each(['abort', 'signal', 'project', 'client', 'unmount'])('cancels pending work and clears timers on %s change', async (changeKind) => {
  vi.useFakeTimers()
  const { props, controller } = setup()
  let finish
  props.client.getWebhookReceipt.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const view = render(<WebhookReceiptPanel {...props} />); fill()
  await act(async () => submit())
  const signal = props.client.getWebhookReceipt.mock.calls[0][3].signal
  await act(async () => {
    if (changeKind === 'abort') controller.abort()
    else if (changeKind === 'unmount') view.unmount()
    else view.rerender(<WebhookReceiptPanel {...props} {...(changeKind === 'signal' ? { signal: new AbortController().signal } :
      changeKind === 'project' ? { projectId: receipt.createdBy } : { client: { ...props.client } })} />)
  })
  expect(signal.aborted).toBe(true)
  expect(vi.getTimerCount()).toBe(0)
  await act(async () => finish(receipt))
  expect(evidence()).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(props.onForbidden).not.toHaveBeenCalled()
  if (changeKind !== 'unmount') expect(screen.getByLabelText('Registration ID')).toHaveValue('')
})
