import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useIosShipController, shipSetupState } from './useIosShipController.js'
import { emptyIosShipReadiness, fetchIosShipReadiness, getIosShipExecution, getIosShipReceipt, requestIosShipLaunch, validateIosShipReadiness } from '../site/iosShipReadiness.js'

vi.mock('../site/iosShipReadiness.js', async (original) => ({
  ...await original(),
  fetchIosShipReadiness: vi.fn(), getIosShipExecution: vi.fn(),
  getIosShipReceipt: vi.fn(), requestIosShipLaunch: vi.fn(),
}))

const props = { projectId: 'p1', revision: 'r1', tenantKey: 'tenant1', sessionActive: true }
const key = 'leaf.ios-ship.pointer:tenant1:p1'
const ready = (projectId = 'p1') => ({ ...emptyIosShipReadiness(), projectId, launchable: true, healthy: true,
  grantStatus: 'healthy', dispatchAvailable: true, approvedLaunch: { approval_id: 'a1', revision: 'r1' } })
const queued = { execution_id: 'e1', status: 'queued' }
const receipt = { kind: 'leaf.ios-testflight-receipt.v1', receipt_id: 'receipt1', build_number: '12' }
const flush = async () => { await act(async () => { await Promise.resolve() }) }
const tick = async (ms = 2000) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const mount = () => renderHook((input) => useIosShipController(input), { initialProps: props })
const launch = async (result) => { await act(async () => { await result.current.launch() }) }

beforeEach(() => {
  vi.useFakeTimers()
  vi.resetAllMocks()
  sessionStorage.clear()
  fetchIosShipReadiness.mockResolvedValue(ready())
  requestIosShipLaunch.mockResolvedValue({ ok: true, execution: queued })
  getIosShipExecution.mockResolvedValue({ execution: queued })
  getIosShipReceipt.mockResolvedValue({ receipt })
})
afterEach(() => { cleanup(); vi.useRealTimers(); sessionStorage.clear() })

it('I1 row1 maps named setup states and transport failures', async () => {
  for (const [record, phase, setupState] of [
    [emptyIosShipReadiness('no_approved_project_revision'), 'setup-required', 'no-approved-revision'],
    [emptyIosShipReadiness('provider_unavailable'), 'unavailable', 'executor-unavailable'],
    [validateIosShipReadiness({ record_kind: 'leaf.ios-ship-readiness.v1', healthy: false, launchable: false, reason: 'unhealthy', grant_status: 'expired' }, { projectId: 'p1' }), 'setup-required', 'grant-not-ready'],
    [emptyIosShipReadiness('unreachable'), 'unavailable', 'none'],
  ]) {
    fetchIosShipReadiness.mockResolvedValue(record)
    const { result, unmount } = mount()
    await flush()
    expect(result.current.phase).toBe(phase)
    expect(result.current.readiness.setupState).toBe(setupState)
    unmount()
  }
  expect(shipSetupState({ reason: 'mini_busy' })).toBe('executor-busy')
  expect(shipSetupState({ reason: 'revision_mismatch' })).toBe('no-approved-revision')
  expect(shipSetupState({ setupAction: 'renew-grant' })).toBe('grant-not-ready')
})

it('I1 row2 synchronously locks launch before awaiting one request', async () => {
  let resolveLaunch
  requestIosShipLaunch.mockReturnValue(new Promise((resolve) => { resolveLaunch = resolve }))
  const { result } = mount()
  await flush()
  expect(result.current.phase).toBe('ready')
  let first
  act(() => { first = result.current.launch(); result.current.launch() })
  expect(requestIosShipLaunch).toHaveBeenCalledTimes(1)
  expect(result.current.phase).toBe('launching')
  expect(requestIosShipLaunch.mock.calls[0][0].idempotencyKey).toBe('ios-ship:p1:a1')
  await act(async () => { resolveLaunch({ execution: queued }); await first })
  expect(result.current.phase).toBe('running')
  expect(result.current.execution).toEqual(queued)
  expect(JSON.parse(sessionStorage.getItem(key))).toMatchObject({ execution_id: 'e1', revision: 'r1' })
})

it('I1 row3 a slow old project cannot replace the new project or retain its execution', async () => {
  const { result, rerender } = mount()
  await flush()
  requestIosShipLaunch.mockResolvedValue({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } })
  await launch(result)
  await flush()
  expect(result.current.receipt).toEqual(receipt)
  let resolveOld
  fetchIosShipReadiness.mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve }))
  act(() => { result.current.refresh() })
  fetchIosShipReadiness.mockResolvedValue(ready('p2'))
  rerender({ ...props, projectId: 'p2' })
  expect(result.current.execution).toBeNull()
  expect(result.current.receipt).toBeNull()
  await flush()
  await act(async () => { resolveOld(ready('p1')) })
  expect(result.current.readiness.projectId).toBe('p2')
  expect(result.current.phase).toBe('ready')
})

it('I1 row4 follows to a receipt and preserves execution on a poll error', async () => {
  const { result } = mount()
  await flush()
  await launch(result)
  getIosShipExecution.mockRejectedValueOnce(new Error('temporary outage'))
  await tick()
  expect(result.current.execution).toEqual(queued)
  expect(result.current.error).toBe('temporary outage')
  getIosShipExecution.mockResolvedValueOnce({ execution: { ...queued, status: 'running', stage: 'BUILT' } })
    .mockResolvedValueOnce({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } })
  await act(async () => { await result.current.refresh() })
  await tick()
  expect(result.current.execution.status).toBe('running')
  await tick()
  expect(result.current.phase).toBe('succeeded')
  expect(result.current.receipt).toEqual(receipt)
  expect(getIosShipReceipt).toHaveBeenCalledTimes(1)
  expect(sessionStorage.getItem(key)).toBeNull()
})

it('I1 row5 recovers only the tenant and project pointer and clears a 404', async () => {
  sessionStorage.setItem('leaf.ios-ship.pointer:tenant1:other', JSON.stringify({ execution_id: 'other' }))
  sessionStorage.setItem('leaf.ios-ship.pointer:other:p1', JSON.stringify({ execution_id: 'other-tenant' }))
  let hook = mount()
  await flush()
  expect(getIosShipExecution).not.toHaveBeenCalled()
  hook.unmount()
  sessionStorage.setItem(key, JSON.stringify({ execution_id: 'e1', revision: 'r1', at: '2026-09-17' }))
  hook = mount()
  await flush()
  expect(getIosShipExecution).toHaveBeenCalledWith({ projectId: 'p1', executionId: 'e1' })
  expect(hook.result.current.phase).toBe('running')
  await tick()
  expect(getIosShipExecution).toHaveBeenCalledTimes(2)
  hook.unmount()
  getIosShipExecution.mockRejectedValueOnce(Object.assign(new Error('missing'), { status: 404 }))
  hook = mount()
  await flush()
  expect(sessionStorage.getItem(key)).toBeNull()
  expect(hook.result.current.error).toBeNull()
  hook.unmount()
  sessionStorage.setItem(key, JSON.stringify({ execution_id: 'e1', revision: 'r1' }))
  getIosShipExecution.mockRejectedValueOnce(new Error('recovery unavailable'))
  hook = mount()
  await flush()
  expect(sessionStorage.getItem(key)).not.toBeNull()
  expect(hook.result.current.error).toBe('recovery unavailable')
})

it('I1 row6 readiness failure keeps a historical receipt', async () => {
  const { result } = mount()
  await flush()
  requestIosShipLaunch.mockResolvedValue({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } })
  await launch(result)
  await flush()
  expect(result.current.receipt).toEqual(receipt)
  fetchIosShipReadiness.mockResolvedValue(emptyIosShipReadiness('unreachable'))
  await act(async () => { await result.current.refresh() })
  expect(result.current.readiness.reason).toBe('unreachable')
  expect(result.current.receipt).toEqual(receipt)
  expect(getIosShipReceipt).toHaveBeenCalledTimes(1)
})

it('I1 row7 stops following at thirty minutes and refresh resumes it', async () => {
  const { result } = mount()
  await flush()
  await launch(result)
  await tick(30 * 60 * 1000)
  expect(result.current.error).toBe('still running; refresh to keep following')
  const calls = getIosShipExecution.mock.calls.length
  await tick(10000)
  expect(getIosShipExecution).toHaveBeenCalledTimes(calls)
  await act(async () => { await result.current.refresh() })
  await tick()
  expect(getIosShipExecution).toHaveBeenCalledTimes(calls + 1)
})
