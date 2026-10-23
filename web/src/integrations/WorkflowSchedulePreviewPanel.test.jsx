import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { TextEncoder } from 'node:util'
import { WorkflowSchedulePreviewPanel } from './WorkflowSchedulePreviewPanel.jsx'

const projectId = '11111111-1111-4111-8111-111111111111'
const input = { schema_version: 1, schedule: { kind: 'daily', timezone: 'UTC', hour: 9, minute: 30 }, after: '2026-01-01T00:00:00Z' }
function previewResult(timezone = 'UTC', hour = 9, minute = 30, scheduledLocalDate = '2026-01-01', instant = '2026-01-01T09:30:00Z') {
  return { schema_version: 1, preview_only: true, occurrence: { timezone, hour, minute, scheduledLocalDate, instant,
    occurrenceKey: JSON.stringify(['daily', timezone, hour, minute, scheduledLocalDate]) } }
}
function setup() {
  const controller = new AbortController()
  const props = { client: { previewWorkflowSchedule: vi.fn().mockResolvedValue(previewResult()) }, projectId,
    signal: controller.signal, guard: vi.fn().mockResolvedValue(), onForbidden: vi.fn() }
  return { props, controller }
}
const change = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
function fill(timezone = 'UTC', hour = '9', minute = '30', after = input.after) {
  change('IANA timezone', timezone); change('Daily hour', hour); change('Daily minute', minute); change('After (UTC)', after)
}
const submit = () => fireEvent.click(screen.getByRole('button', { name: 'Preview next occurrence' }))
beforeEach(() => vi.stubGlobal('TextEncoder', TextEncoder))
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })

it('starts empty without mount focus, makes no edit requests and previews only on explicit submit', async () => {
  const { props } = setup()
  render(<WorkflowSchedulePreviewPanel {...props} />)
  expect(screen.getByLabelText('IANA timezone')).toHaveValue('')
  expect(screen.getByLabelText('After (UTC)')).toHaveValue('')
  expect(document.activeElement).toBe(document.body)
  expect(screen.getByText('Preview only. No schedule is saved or activated.')).toBeInTheDocument()
  fill()
  expect(props.client.previewWorkflowSchedule).not.toHaveBeenCalled()
  expect(props.guard).not.toHaveBeenCalled()
  submit()
  const result = await screen.findByRole('region', { name: 'Next scheduled occurrence' })
  expect(props.client.previewWorkflowSchedule).toHaveBeenCalledOnce()
  expect(props.client.previewWorkflowSchedule).toHaveBeenCalledWith(projectId, input, { signal: expect.any(AbortSignal) })
  expect(props.guard).toHaveBeenCalledTimes(2)
  expect(document.activeElement).toContainElement(result)
  expect(result).toHaveTextContent('Daily time: 09:30')
  expect(result).toHaveTextContent(input.after.slice(0, 10))
  expect(result).toHaveTextContent('2026-01-01T09:30:00Z')
  expect(result).not.toHaveTextContent('occurrenceKey')
  change('Daily hour', '10')
  expect(screen.queryByRole('region', { name: 'Next scheduled occurrence' })).toBeNull()
  expect(props.client.previewWorkflowSchedule).toHaveBeenCalledTimes(1)
})
it.each([
  ['IANA timezone', ' UTC'], ['Daily hour', '24'], ['Daily hour', ''], ['Daily minute', '1.5'],
  ['After (UTC)', '2026-02-30T00:00:00Z'], ['After (UTC)', '2026-01-01T00:00:00+00:00'],
])('validates %s, focuses feedback and preserves inputs', async (label, value) => {
  const { props } = setup(); render(<WorkflowSchedulePreviewPanel {...props} />)
  fill(); change(label, value); submit()
  const error = await screen.findByRole('alert')
  expect(document.activeElement).toContainElement(error)
  expect(props.client.previewWorkflowSchedule).not.toHaveBeenCalled()
  expect(screen.getByLabelText(label)).toHaveValue(value)
})
it('locks inputs and synchronous duplicate submits through both guards and the request', async () => {
  const { props } = setup()
  let finishGuard, finishRequest
  props.guard.mockImplementationOnce(() => new Promise((resolve) => { finishGuard = resolve }))
  props.client.previewWorkflowSchedule.mockImplementationOnce(() => new Promise((resolve) => { finishRequest = resolve }))
  render(<WorkflowSchedulePreviewPanel {...props} />); fill(); submit(); submit()
  expect(screen.getByText('Calculating schedule preview…')).toBeInTheDocument()
  expect(screen.getByLabelText('Daily hour')).toBeDisabled()
  expect(props.client.previewWorkflowSchedule).not.toHaveBeenCalled()
  await act(async () => finishGuard())
  submit(); change('Daily hour', '10')
  expect(screen.getByLabelText('Daily hour')).toHaveValue('9')
  await act(async () => finishRequest(previewResult()))
  await screen.findByRole('region', { name: 'Next scheduled occurrence' })
  expect(props.client.previewWorkflowSchedule).toHaveBeenCalledOnce()
})
it.each([new Error('private credentials'), { status: 404 }, { status: 503 }])('uses fixed unavailable feedback and explicit retry: %j', async (failure) => {
  const { props } = setup(); props.client.previewWorkflowSchedule.mockRejectedValueOnce(failure)
  render(<WorkflowSchedulePreviewPanel {...props} />); fill(); submit()
  expect(await screen.findByRole('alert')).toHaveTextContent('Schedule preview is unavailable. Please retry using Preview next occurrence.')
  expect(screen.getByLabelText('After (UTC)')).toHaveValue(input.after)
  expect(document.body.textContent).not.toContain('private credentials')
  expect(props.client.previewWorkflowSchedule).toHaveBeenCalledOnce()
  submit(); await screen.findByRole('region', { name: 'Next scheduled occurrence' })
  expect(props.client.previewWorkflowSchedule).toHaveBeenCalledTimes(2)
})
it.each(['preflight', 'request', 'completion'])('bounds a never-settling %s, retains inputs and isolates late completion from retry', async (phase) => {
  vi.useFakeTimers()
  const { props } = setup()
  let finishExpired, finishRetry, expiredSignal
  const stalled = (signal) => {
    expiredSignal = signal
    return new Promise((resolve) => { finishExpired = resolve })
  }
  if (phase === 'preflight') props.guard.mockImplementationOnce(stalled)
  else if (phase === 'completion') props.guard.mockResolvedValueOnce().mockImplementationOnce(stalled)
  else props.client.previewWorkflowSchedule.mockImplementationOnce((_, __, { signal }) => stalled(signal))
  render(<WorkflowSchedulePreviewPanel {...props} />); fill()
  await act(async () => submit())
  expect(finishExpired).toBeTypeOf('function')
  await act(async () => vi.advanceTimersByTimeAsync(44999))
  expect(screen.getByLabelText('Daily hour')).toBeDisabled()
  expect(screen.queryByRole('alert')).toBeNull()
  await act(async () => vi.advanceTimersByTimeAsync(1))
  const error = screen.getByRole('alert')
  expect(error).toHaveTextContent('Schedule preview is unavailable. Please retry using Preview next occurrence.')
  expect(document.activeElement).toContainElement(error)
  expect(expiredSignal.aborted).toBe(true)
  expect(screen.getByLabelText('IANA timezone')).toHaveValue('UTC')
  expect(screen.getByLabelText('Daily hour')).toHaveValue('9')
  expect(screen.getByLabelText('Daily minute')).toHaveValue('30')
  expect(screen.getByLabelText('After (UTC)')).toHaveValue(input.after)
  expect(screen.getByRole('button', { name: 'Preview next occurrence' })).toBeEnabled()
  const callsBeforeRetry = props.client.previewWorkflowSchedule.mock.calls.length
  props.client.previewWorkflowSchedule.mockImplementationOnce(() => new Promise((resolve) => { finishRetry = resolve }))
  await act(async () => submit())
  const retrySignal = props.client.previewWorkflowSchedule.mock.calls.at(-1)[2].signal
  expect(retrySignal).not.toBe(expiredSignal)
  expect(retrySignal.aborted).toBe(false)
  await act(async () => finishExpired(phase === 'request' ? previewResult() : undefined))
  expect(screen.queryByRole('region', { name: 'Next scheduled occurrence' })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByText('Calculating schedule preview…')).toBeInTheDocument()
  expect(screen.getByLabelText('Daily hour')).toBeDisabled()
  submit()
  expect(props.client.previewWorkflowSchedule).toHaveBeenCalledTimes(callsBeforeRetry + 1)
  await act(async () => finishRetry(previewResult()))
  expect(screen.getByRole('region', { name: 'Next scheduled occurrence' })).toHaveTextContent('2026-01-01T09:30:00Z')
  expect(screen.getByRole('button', { name: 'Preview next occurrence' })).toBeEnabled()
  expect(props.onForbidden).not.toHaveBeenCalled()
})
it.each(['abort', 'context', 'unmount'])('cleans up a stalled preview timeout on %s without stale feedback', async (action) => {
  vi.useFakeTimers()
  const { props, controller } = setup()
  let finish
  props.client.previewWorkflowSchedule.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const { rerender, unmount } = render(<WorkflowSchedulePreviewPanel {...props} />); fill()
  await act(async () => submit())
  const operationSignal = props.client.previewWorkflowSchedule.mock.calls[0][2].signal
  await act(async () => {
    if (action === 'abort') controller.abort()
    else if (action === 'context') rerender(<WorkflowSchedulePreviewPanel {...props} signal={new AbortController().signal} />)
    else unmount()
  })
  expect(operationSignal.aborted).toBe(true)
  expect(vi.getTimerCount()).toBe(0)
  await act(async () => {
    finish(previewResult())
    await vi.advanceTimersByTimeAsync(45000)
  })
  expect(screen.queryByRole('region', { name: 'Next scheduled occurrence' })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  if (action !== 'unmount') expect(screen.getByLabelText('After (UTC)')).toHaveValue('')
  expect(props.guard).toHaveBeenCalledOnce()
  expect(props.onForbidden).not.toHaveBeenCalled()
})
it.each([{ preview_only: false }, { occurrence: { ...previewResult().occurrence, minute: 31 } }, { extra: true }])('validates injected clients at the component boundary %j', async (patch) => {
  const { props } = setup(); props.client.previewWorkflowSchedule.mockResolvedValueOnce({ ...previewResult(), ...patch })
  render(<WorkflowSchedulePreviewPanel {...props} />); fill(); submit()
  await screen.findByRole('alert')
  expect(screen.queryByRole('region', { name: 'Next scheduled occurrence' })).toBeNull()
})
it.each([401, 403])('clears results and follows forbidden on %s', async (status) => {
  const { props } = setup()
  render(<WorkflowSchedulePreviewPanel {...props} />); fill(); submit()
  await screen.findByRole('region', { name: 'Next scheduled occurrence' })
  props.guard.mockRejectedValueOnce({ status }); submit()
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
  expect(screen.queryByRole('region', { name: 'Next scheduled occurrence' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Preview next occurrence' })).toBeDisabled()
})
it.each(['preflight', 'request', 'completion'])('aborts work and suppresses late %s responses', async (phase) => {
  const { props, controller } = setup()
  let finish
  const deferred = () => new Promise((resolve) => { finish = resolve })
  if (phase === 'preflight') props.guard.mockImplementationOnce(deferred)
  else if (phase === 'completion') props.guard.mockResolvedValueOnce().mockImplementationOnce(deferred)
  else props.client.previewWorkflowSchedule.mockImplementationOnce(deferred)
  render(<WorkflowSchedulePreviewPanel {...props} />); fill(); submit()
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  act(() => controller.abort())
  await act(async () => finish(phase === 'request' ? previewResult() : undefined))
  expect(screen.queryByRole('region', { name: 'Next scheduled occurrence' })).toBeNull()
  expect(screen.getByLabelText('IANA timezone')).toHaveValue('')
  if (phase === 'preflight') expect(props.client.previewWorkflowSchedule).not.toHaveBeenCalled()
  else expect(props.client.previewWorkflowSchedule.mock.calls[0][2].signal.aborted).toBe(true)
})
it('clears scoped results when the parent signal changes', async () => {
  const { props } = setup()
  const { rerender } = render(<WorkflowSchedulePreviewPanel {...props} />); fill(); submit()
  await screen.findByRole('region', { name: 'Next scheduled occurrence' })
  rerender(<WorkflowSchedulePreviewPanel {...props} signal={new AbortController().signal} />)
  expect(screen.queryByRole('region', { name: 'Next scheduled occurrence' })).toBeNull()
  expect(screen.getByLabelText('After (UTC)')).toHaveValue('')
})
it('aborts on unmount and ignores a late injected response', async () => {
  const { props } = setup()
  let finish
  props.client.previewWorkflowSchedule.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const { unmount } = render(<WorkflowSchedulePreviewPanel {...props} />); fill(); submit()
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const signal = props.client.previewWorkflowSchedule.mock.calls[0][2].signal
  unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => finish(previewResult()))
  expect(props.guard).toHaveBeenCalledOnce()
  expect(props.onForbidden).not.toHaveBeenCalled()
})
it.each([
  ['UTC', '9', '0', '2026-01-20T08:59:59.999Z', previewResult('UTC', 9, 0, '2026-01-20', '2026-01-20T09:00:00.000Z')],
  ['America/New_York', '2', '30', '2026-03-08T05:00:00Z', previewResult('America/New_York', 2, 30, '2026-03-08', '2026-03-08T07:00:00.000Z')],
  ['America/New_York', '1', '30', '2026-11-01T04:00:00Z', previewResult('America/New_York', 1, 30, '2026-11-01', '2026-11-01T05:30:00.000Z')],
])('renders simulated %s calendar evidence verbatim without planning in the browser', async (timezone, hour, minute, after, result) => {
  const { props } = setup(); props.client.previewWorkflowSchedule.mockResolvedValueOnce(result)
  render(<WorkflowSchedulePreviewPanel {...props} />); fill(timezone, hour, minute, after); submit()
  const region = await screen.findByRole('region', { name: 'Next scheduled occurrence' })
  expect(region).toHaveTextContent(result.occurrence.instant)
  expect(region).toHaveTextContent(result.occurrence.scheduledLocalDate)
  expect(region).toHaveTextContent(timezone)
  expect(screen.getByText(/A missing local time moves to the next valid time/)).toBeInTheDocument()
})
