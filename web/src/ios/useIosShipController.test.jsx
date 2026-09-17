import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useIosShipController, shipSetupState } from './useIosShipController.js'
import { validateIosShipReadiness } from '../site/iosShipReadiness.js'

// Transport spies: the hook still calls every real fetch helper and validator.
const fetchIosShipReadiness = vi.fn()
const getIosShipExecution = vi.fn()
const getIosShipReceipt = vi.fn()
const requestIosShipLaunch = vi.fn()

const props = { projectId: 'p1', revision: 'r1', tenantKey: 'tenant1', sessionActive: true }
const key = 'leaf.ios-ship.pointer:tenant1:p1'
const approvedReadiness = ({ projectId = 'p1', revision = 'r1', ...overrides } = {}) => validateIosShipReadiness({
  record_kind: 'leaf.ios-ship-readiness.v1', project_id: projectId,
  launchable: true, healthy: true, grant_status: 'healthy', dispatch_available: true,
  approved_launch: { approval_id: 'a1', revision, source_revision: 'source1', source_sha256: 'a'.repeat(64),
    bundle_identifier: 'com.leaf.test', marketing_version: '1.0', build_number: '12' },
  ...overrides,
}, { projectId, revision })
const emptyIosShipReadiness = (reason) => approvedReadiness({ launchable: false, healthy: false, reason })
const transport = async (url, init) => {
  const path = new URL(url, 'http://localhost')
  let data
  if (path.pathname.endsWith('/readiness')) {
    const readiness = await fetchIosShipReadiness({ projectId: path.searchParams.get('project_id'), revision: path.searchParams.get('revision') })
    data = { readiness: { record_kind: readiness.kind, project_id: readiness.projectId,
      launchable: readiness.launchable, healthy: readiness.healthy, grant_status: readiness.grantStatus,
      dispatch_available: readiness.dispatchAvailable, approved_launch: readiness.approvedLaunch,
      reason: readiness.reason, setup_action: readiness.setupAction } }
  } else if (path.pathname.endsWith('/launch')) {
    const { project_id: projectId, ...approvedLaunch } = JSON.parse(init.body)
    data = { ok: true, ...await requestIosShipLaunch({ projectId, approvedLaunch, idempotencyKey: init.headers['Idempotency-Key'] }) }
  } else if (path.pathname.includes('/executions/')) {
    data = { ok: true, ...await getIosShipExecution({ projectId: path.searchParams.get('project_id'), executionId: decodeURIComponent(path.pathname.split('/').pop()) }) }
  } else if (path.pathname.includes('/receipts/')) {
    data = { ok: true, ...await getIosShipReceipt({ projectId: path.searchParams.get('project_id'), receiptId: decodeURIComponent(path.pathname.split('/').pop()) }) }
  } else throw new Error(`Unexpected request: ${path.pathname}`)
  return { ok: true, status: path.pathname.endsWith('/launch') ? 202 : 200, json: async () => data }
}
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
  vi.stubGlobal('fetch', vi.fn(transport))
  fetchIosShipReadiness.mockResolvedValue(approvedReadiness())
  requestIosShipLaunch.mockResolvedValue({ ok: true, execution: queued })
  getIosShipExecution.mockResolvedValue({ execution: queued })
  getIosShipReceipt.mockResolvedValue({ receipt })
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); sessionStorage.clear() })

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
  fetchIosShipReadiness.mockResolvedValue(approvedReadiness({ projectId: 'p2' }))
  rerender({ ...props, projectId: 'p2' })
  expect(result.current.execution).toBeNull()
  expect(result.current.receipt).toBeNull()
  await flush()
  await act(async () => { resolveOld(approvedReadiness()) })
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

it('I1 row11 revision ownership clears execution and receipt and ignores old pointers and polls', async () => {
  const { result, rerender } = mount()
  await flush()
  requestIosShipLaunch.mockResolvedValueOnce({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } })
  await launch(result)
  await flush()
  expect(result.current.receipt).toEqual(receipt)
  fetchIosShipReadiness.mockResolvedValue(approvedReadiness({ revision: 'r2' }))
  rerender({ ...props, revision: 'r2' })
  expect(result.current.execution).toBeNull()
  expect(result.current.receipt).toBeNull()
  await flush()
  fetchIosShipReadiness.mockResolvedValue(approvedReadiness())
  rerender(props)
  await flush()
  await launch(result)
  let resolvePoll
  getIosShipExecution.mockReturnValueOnce(new Promise((resolve) => { resolvePoll = resolve }))
  await tick()
  const pointer = sessionStorage.getItem(key)
  const calls = getIosShipExecution.mock.calls.length
  fetchIosShipReadiness.mockResolvedValue(approvedReadiness({ revision: 'r2' }))
  rerender({ ...props, revision: 'r2' })
  expect(result.current.execution).toBeNull()
  expect(result.current.receipt).toBeNull()
  await flush()
  expect(getIosShipExecution).toHaveBeenCalledTimes(calls)
  expect(sessionStorage.getItem(key)).toBe(pointer)
  await act(async () => { resolvePoll({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } }) })
  expect(result.current.execution).toBeNull()
  expect(result.current.receipt).toBeNull()
  expect(sessionStorage.getItem(key)).toBe(pointer)
})

it('I1 row12 pending launch locks across disable and reenable until settlement', async () => {
  let resolveLaunch
  requestIosShipLaunch.mockReturnValueOnce(new Promise((resolve) => { resolveLaunch = resolve }))
  const { result, rerender } = mount()
  await flush()
  let first
  act(() => { first = result.current.launch() })
  rerender({ ...props, enabled: false })
  rerender({ ...props, enabled: true })
  await flush()
  expect(result.current.readiness.launchable).toBe(true)
  await act(async () => { expect(await result.current.launch()).toBeNull() })
  expect(requestIosShipLaunch).toHaveBeenCalledTimes(1)
  await act(async () => { resolveLaunch({ execution: queued }); await first })
  expect(result.current.execution).toBeNull()
  await launch(result)
  expect(requestIosShipLaunch).toHaveBeenCalledTimes(2)
  expect(result.current.execution).toEqual(queued)
})

it('I1 row13 refresh during a receipt read does not request it twice or discard its result', async () => {
  let resolveReceipt
  getIosShipReceipt.mockReturnValueOnce(new Promise((resolve) => { resolveReceipt = resolve }))
  const { result } = mount()
  await flush()
  await launch(result)
  getIosShipExecution.mockResolvedValueOnce({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } })
  await tick()
  expect(getIosShipReceipt).toHaveBeenCalledTimes(1)
  await act(async () => { await result.current.refresh() })
  expect(getIosShipReceipt).toHaveBeenCalledTimes(1)
  await act(async () => { resolveReceipt({ receipt }) })
  expect(result.current.receipt).toEqual(receipt)
  await act(async () => { await result.current.refresh() })
  expect(getIosShipReceipt).toHaveBeenCalledTimes(1)
})

it('I1 row14 busy ends with the launch response while execution is running', async () => {
  const { result } = mount()
  await flush()
  await launch(result)
  expect(result.current.phase).toBe('running')
  expect(result.current.busy).toBe(false)
})
