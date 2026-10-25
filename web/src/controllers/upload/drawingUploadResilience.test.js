import { useSyncExternalStore } from 'react'
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createDrawingUploadController } from './createDrawingUploadController.js'

const file = { name: 'roof.dxf', size: 128 }
const policy = { enabled: true, accepted: ['.dxf'], max_bytes: 1024 }
const controllers = []

function setup(options = {}) {
  const services = {
    policy: vi.fn().mockResolvedValue(policy),
    upload: vi.fn().mockResolvedValue({ drawing_id: 'drawing-1', status: 'extracting' }),
    status: vi.fn().mockResolvedValue({ status: 'ready' }),
    intake: vi.fn().mockResolvedValue({ drawing: 'roof.dxf' }),
    wait: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  }
  const controller = createDrawingUploadController({ services, ...options })
  controllers.push(controller)
  const { result } = renderHook(() => useSyncExternalStore(controller.subscribe, controller.getSnapshot))
  return { controller, services, result }
}

async function begin(controller) {
  await act(async () => { await controller.loadPolicy() })
  let pending
  await act(async () => { pending = controller.upload(file) })
  return { pending }
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  controllers.splice(0).forEach((controller) => controller.dispose())
  cleanup()
  vi.useRealTimers()
})

describe('drawing upload resilience', () => {
  it('completes after two failed polls and a successful poll on the existing cadence', async () => {
    const { controller, services, result } = setup()
    services.status.mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockRejectedValueOnce(new Error('The service did not respond within 5000 ms.'))
    const { pending } = await begin(controller)
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(result.current).toMatchObject({ phase: 'extracting', busy: true, error: null })
    expect(services.status).toHaveBeenCalledTimes(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(500); await pending })
    expect(result.current).toMatchObject({ phase: 'ready', busy: false, error: null })
    expect(services.intake).toHaveBeenCalledTimes(1)
  })

  it('fails after three consecutive failed polls with readable text', async () => {
    const { controller, services, result } = setup()
    services.status.mockRejectedValue(Object.assign(new Error('GET /api/drawings/private-id/upload-status -> 503'), { status: 503 }))
    const { pending } = await begin(controller)
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); await pending })
    expect(result.current).toMatchObject({ phase: 'failed', busy: false, error: 'The service had a temporary problem. Try again.' })
    expect(services.status).toHaveBeenCalledTimes(3)
    expect(services.intake).not.toHaveBeenCalled()
  })

  it('resets consecutive failures after a successful extracting poll', async () => {
    const { controller, services, result } = setup()
    const error = new TypeError('Failed to fetch')
    services.status.mockRejectedValueOnce(error).mockRejectedValueOnce(error)
      .mockResolvedValueOnce({ status: 'extracting' })
      .mockRejectedValueOnce(error).mockRejectedValueOnce(error)
    const { pending } = await begin(controller)
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); await pending })
    expect(result.current.phase).toBe('ready')
    expect(services.status).toHaveBeenCalledTimes(6)
  })

  it.each([408, 429, 500, 503])('retries status %s', async (status) => {
    const { controller, services, result } = setup()
    services.status.mockRejectedValueOnce(Object.assign(new Error('Temporary problem'), { status }))
    const { pending } = await begin(controller)
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); await pending })
    expect(result.current.phase).toBe('ready')
    expect(services.status).toHaveBeenCalledTimes(2)
  })

  it.each([400, 401, 403, 404, 422])('fails immediately for status %s', async (status) => {
    const { controller, services, result } = setup()
    services.status.mockRejectedValue(Object.assign(new Error(`GET /api/drawings/private-id/upload-status -> ${status}`), { status }))
    const { pending } = await begin(controller)
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); await pending })
    expect(result.current.phase).toBe('failed')
    expect(result.current.error).not.toMatch(/GET|\/api\/|private-id/)
    expect(services.status).toHaveBeenCalledTimes(1)
  })

  it.each(['Failed to fetch', 'The service did not respond within 5000 ms.'])('humanizes exhausted poll errors: %s', async (message) => {
    const { controller, services, result } = setup()
    services.status.mockRejectedValue(new Error(message))
    const { pending } = await begin(controller)
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); await pending })
    expect(result.current.error).toBe("Couldn't reach the service. Check the connection and retry.")
  })

  it('enables uploads when the first automatic policy retry succeeds', async () => {
    const { controller, services, result } = setup()
    services.policy.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await act(async () => { await controller.loadPolicy() })
    expect(result.current).toMatchObject({ policyLoading: true, error: null })
    await act(async () => { await vi.advanceTimersByTimeAsync(1999) })
    expect(services.policy).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(result.current).toMatchObject({ policyLoading: false, policy: { enabled: true }, error: null })
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    expect(services.policy).toHaveBeenCalledTimes(2)
  })

  it('settles disabled after policy retries at two seconds and five more seconds', async () => {
    const { controller, services, result } = setup()
    services.policy.mockRejectedValue(new TypeError('Failed to fetch'))
    await act(async () => { await controller.loadPolicy() })
    await act(async () => { await vi.advanceTimersByTimeAsync(6999) })
    expect(services.policy).toHaveBeenCalledTimes(2)
    expect(result.current.policyLoading).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(result.current).toMatchObject({ policyLoading: false, policy: { enabled: false }, error: "Couldn't reach the service. Check the connection and retry." })
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    expect(services.policy).toHaveBeenCalledTimes(3)
  })

  it('clears a pending policy retry on dispose', async () => {
    const { controller, services } = setup()
    services.policy.mockRejectedValue(new TypeError('Failed to fetch'))
    await act(async () => { await controller.loadPolicy() })
    controller.dispose()
    expect(vi.getTimerCount()).toBe(0)
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    expect(services.policy).toHaveBeenCalledTimes(1)
  })

  it('manual policy refresh supersedes the scheduled retry', async () => {
    const { controller, services, result } = setup()
    services.policy.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await act(async () => { await controller.loadPolicy(); await controller.loadPolicy() })
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    expect(result.current.policy.enabled).toBe(true)
    expect(services.policy).toHaveBeenCalledTimes(2)
  })

  it('keeps the overall poll cap even when transient failures consume attempts', async () => {
    const { controller, services, result } = setup({ maxPolls: 2 })
    services.status.mockRejectedValue(new TypeError('Failed to fetch'))
    const { pending } = await begin(controller)
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); await pending })
    expect(services.status).toHaveBeenCalledTimes(2)
    expect(result.current.error).toContain('still running')
    expect(services.intake).not.toHaveBeenCalled()
  })

  it('does not schedule retries when a policy request rejects after disposal', async () => {
    const { controller, services } = setup()
    let reject
    services.policy.mockImplementationOnce(() => new Promise((resolve, fail) => { reject = fail }))
    let pending
    await act(async () => { pending = controller.loadPolicy() })
    controller.dispose()
    await act(async () => { reject(new TypeError('Failed to fetch')); await pending })
    expect(vi.getTimerCount()).toBe(0)
    expect(services.policy).toHaveBeenCalledTimes(1)
  })
})
