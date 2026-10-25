import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import IosSurface from './IosSurface.jsx'
import { useIosShipController } from './useIosShipController.js'
import { fetchIosShipReadiness, getIosShipExecution, requestIosShipLaunch } from '../site/iosShipReadiness.js'

vi.mock('../site/iosShipReadiness.js', async (importOriginal) => ({
  ...await importOriginal(),
  fetchIosShipReadiness: vi.fn(),
  getIosShipExecution: vi.fn(),
  requestIosShipLaunch: vi.fn(),
  fetchIosShipSources: vi.fn(async () => ({ sources: [], approvals: [], sync: null, canApprove: false })),
}))

const props = { projectId: 'p1', revision: 'r1', tenantKey: 'tenant1', enabled: true, sessionActive: true }
const ready = { projectId: 'p1', launchable: true, grantStatus: 'healthy', dispatchAvailable: true,
  approvedLaunch: { approval_id: 'a1', revision: 'r1' } }
const queued = { execution_id: 'e1', status: 'queued' }
const tick = async (ms) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const start = async () => {
  const hook = renderHook(() => useIosShipController(props))
  await act(async () => {})
  await act(async () => { await hook.result.current.launch() })
  return hook
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  sessionStorage.clear()
  fetchIosShipReadiness.mockResolvedValue(ready)
  requestIosShipLaunch.mockResolvedValue({ execution: queued })
  getIosShipExecution.mockResolvedValue({ execution: queued })
})
afterEach(() => { cleanup(); vi.useRealTimers(); sessionStorage.clear() })

it('S1-87 describes normal setup without an alert or raw setup identifiers', async () => {
  fetchIosShipReadiness.mockResolvedValue({ projectId: 'p1', launchable: false,
    reason: 'no_approved_project_revision', setupAction: 'mount-apple-ship-dispatch' })
  const { result } = renderHook(() => useIosShipController(props))
  await act(async () => {})
  const view = render(<IosSurface enabled ship={{ ...result.current, sync: { status: 'provider_unavailable' } }} />)
  expect(screen.getByRole('status')).toHaveTextContent('Finish setup before starting a build.')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(view.container.textContent).toContain('ask the project owner to approve it')
  expect(view.container.textContent).not.toMatch(/setup-required|mount-apple|provider_unavailable/)
})

it('S1-87 maps failed stages and transport failures to readable text', async () => {
  const { result } = await start()
  getIosShipExecution.mockRejectedValueOnce(new Error('GET /api/ios-ship/executions/e1 -> 503'))
  await tick(2000)
  const view = render(<IosSurface enabled ship={{ ...result.current,
    execution: { ...queued, failed_stage: 'SIGNING_READY' } }} />)
  expect(view.container.textContent).toContain('App signing ready')
  expect(screen.getByRole('alert')).toHaveTextContent('Try again.')
  expect(view.container.textContent).not.toMatch(/GET|\/api\/|503|SIGNING_READY/)
})

it('S1-184 follows through a transient failure without a manual refresh', async () => {
  const { result } = await start()
  getIosShipExecution.mockRejectedValueOnce(new Error('Failed to fetch'))
    .mockResolvedValueOnce({ execution: { ...queued, status: 'succeeded' } })
  await tick(2000)
  expect(result.current.phase).toBe('running')
  await tick(2000)
  expect(result.current.phase).toBe('succeeded')
  expect(result.current.error).toBeNull()
})

it('S1-184 bounds retries and resumes from the visible control', async () => {
  const { result } = await start()
  getIosShipExecution.mockRejectedValue(new Error('Failed to fetch'))
  await tick(16000)
  expect(getIosShipExecution).toHaveBeenCalledTimes(4)
  expect(result.current.followStopped).toBe(true)
  await tick(20000)
  expect(getIosShipExecution).toHaveBeenCalledTimes(4)
  render(<IosSurface enabled ship={result.current} />)
  getIosShipExecution.mockResolvedValue({ execution: { ...queued, status: 'succeeded' } })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Resume status updates' })) })
  await tick(2000)
  expect(result.current.phase).toBe('succeeded')
})

it('S1-185 offers another build after success or failure using fresh readiness', async () => {
  for (const status of ['failed', 'succeeded']) {
    const { result, unmount } = await start()
    getIosShipExecution.mockResolvedValue({ execution: { ...queued, status } })
    await tick(2000)
    fetchIosShipReadiness.mockResolvedValue({ ...ready, approvedLaunch: { approval_id: 'a2', revision: 'r1' } })
    const view = render(<IosSurface enabled ship={result.current} />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Check for next build' })) })
    expect(result.current.phase).toBe('ready')
    expect(result.current.execution).toBeNull()
    expect(result.current.readiness.approvedLaunch.approval_id).toBe('a2')
    expect(requestIosShipLaunch).toHaveBeenCalledTimes(status === 'failed' ? 1 : 2)
    view.unmount()
    unmount()
  }
})

it('S1-185 retains the terminal result when fresh readiness refuses another launch', async () => {
  const { result } = await start()
  getIosShipExecution.mockResolvedValue({ execution: { ...queued, status: 'failed' } })
  await tick(2000)
  fetchIosShipReadiness.mockResolvedValue({ launchable: false, reason: 'no_approved_project_revision' })
  await act(async () => { await result.current.nextBuild() })
  expect(result.current.execution.status).toBe('failed')
  expect(result.current.readiness.setupState).toBe('no-approved-revision')
})
