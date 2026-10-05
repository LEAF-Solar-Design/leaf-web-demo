import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useLayoutEffect, useMemo } from 'react'
import { useBoardConversation } from '../workspace/ProjectWorkspacePanels.jsx'
import useConverseSessionController from './useConverseSessionController.js'
import { ensureSession, postMessage } from '../converse.js'

vi.mock('../converse.js', () => ({
  ensureSession: vi.fn(), postMessage: vi.fn(), resetSession: vi.fn(),
  classifyAgentError: () => 'busy', projectActivityProjection: (value) => value,
}))

beforeEach(() => {
  vi.clearAllMocks()
  ensureSession.mockResolvedValue({ session_id: 'session-a' })
})

function projectController() {
  const hook = renderHook(() => useConverseSessionController({ drawingId: 'drawing-a', retryNotFound: true }))
  act(() => hook.result.current.setProjectContext('project-a'))
  return hook
}

it('A2-13 attaches a project without a drawing', async () => {
  const { result } = renderHook(() => useConverseSessionController({ drawingId: null }))
  act(() => result.current.setProjectContext('project-a'))
  await act(async () => { await result.current.attach() })
  expect(ensureSession).toHaveBeenCalledWith(null, 'project-a')
  expect(result.current.sessionId).toBe('session-a')
  expect(postMessage).not.toHaveBeenCalled()
})

it('A2-16 shares pending attachment without sending a turn', async () => {
  let resolve
  ensureSession.mockImplementation(() => new Promise((done) => { resolve = done }))
  const { result } = projectController()
  let first, second
  await act(async () => {
    first = result.current.attach()
    second = result.current.attach()
  })
  expect(first).toBe(second)
  expect(ensureSession).toHaveBeenCalledTimes(1)
  await act(async () => { resolve({ session_id: 'attached' }); await first })
  expect(result.current.sessionId).toBe('attached')
  expect(result.current.turns).toEqual([])
  expect(postMessage).not.toHaveBeenCalled()
})

it('A2-17 permits a fresh attachment after failure', async () => {
  ensureSession.mockRejectedValueOnce(new Error('offline'))
  const { result } = projectController()
  await act(async () => { await expect(result.current.attach()).rejects.toThrow('offline') })
  await act(async () => { await result.current.attach() })
  expect(ensureSession).toHaveBeenCalledTimes(2)
  expect(result.current.sessionId).toBe('session-a')
  expect(postMessage).not.toHaveBeenCalled()
})

it.each(['project', 'clear', 'drawing', 'unmount'])('A2-23 rejects attachment invalidated by %s', async (change) => {
  let resolve
  ensureSession.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const hook = renderHook(({ drawingId }) => useConverseSessionController({ drawingId }), {
    initialProps: { drawingId: 'drawing-a' },
  })
  act(() => hook.result.current.setProjectContext('project-a'))
  let pending
  await act(async () => { pending = hook.result.current.attach().catch((error) => error) })
  act(() => {
    if (change === 'project') hook.result.current.setProjectContext('project-b')
    if (change === 'clear') hook.result.current.clear()
    if (change === 'drawing') hook.rerender({ drawingId: 'drawing-b' })
    if (change === 'unmount') hook.unmount()
  })
  await act(async () => {
    resolve({ session_id: 'stale' })
    expect(await pending).toBeInstanceOf(Error)
  })
  expect(hook.result.current.sessionId).toBeNull()
  expect(postMessage).not.toHaveBeenCalled()
})

it('A2-23 does not start transport after immediate session invalidation', async () => {
  const { result } = projectController()
  let pending
  act(() => {
    pending = result.current.attach().catch((error) => error)
    result.current.clear()
  })
  await act(async () => { expect(await pending).toBeInstanceOf(Error) })
  expect(ensureSession).not.toHaveBeenCalled()
  expect(result.current.sessionId).toBeNull()
})

it('A2-29 invalidates a scheduled board attachment before its microtask and permits sign-in recovery', async () => {
  let resolveOld
  const oldTransport = new Promise((resolve) => { resolveOld = resolve })
  ensureSession.mockReturnValue(oldTransport)
  const onOpen = vi.fn()

  const hook = renderHook(({ signedIn }) => {
    const controller = useConverseSessionController({ drawingId: null })
    useLayoutEffect(() => {
      controller.setProjectContext('project-a')
    }, [controller.setProjectContext])
    useLayoutEffect(() => {
      if (!signedIn) controller.clear()
    }, [signedIn, controller.clear])
    const context = useMemo(() => ({}), [signedIn])
    useBoardConversation({
      active: true, eligible: signedIn, sessionId: controller.sessionId,
      context, attach: controller.attach, onOpen,
    })
    return controller
  }, { initialProps: { signedIn: true } })

  // renderHook flushes the scheduling effect, but no await has yielded yet.
  expect(onOpen).toHaveBeenCalledOnce()
  hook.rerender({ signedIn: false })

  await act(async () => {
    await Promise.resolve()
    resolveOld({ session_id: 'late-session' })
    await oldTransport
  })
  expect(ensureSession).not.toHaveBeenCalled()
  expect(hook.result.current.sessionId).toBeNull()
  expect(hook.result.current.turns).toEqual([])
  expect(postMessage).not.toHaveBeenCalled()

  ensureSession.mockResolvedValue({ session_id: 'fresh-session' })
  hook.rerender({ signedIn: true })
  await act(async () => { await Promise.resolve() })
  expect(ensureSession).toHaveBeenCalledTimes(1)
  expect(ensureSession).toHaveBeenCalledWith(null, 'project-a')
  expect(hook.result.current.sessionId).toBe('fresh-session')
  expect(hook.result.current.turns).toEqual([])
  expect(postMessage).not.toHaveBeenCalled()
})

describe('author immediate-turn contract', () => {
  it('records a completed journaled answer as a completed turn', async () => {
    postMessage.mockResolvedValue({ status: 'completed', turn_id: 'turn-a', request_id: 'request-a' })
    const { result } = projectController()
    let response
    await act(async () => { response = await result.current.startTurn('hello') })
    expect(response).toMatchObject({ session_id: 'session-a', turn_id: 'turn-a', status: 'completed' })
    expect(result.current.turns).toHaveLength(1)
    expect(result.current.turns[0]).toMatchObject({ turnId: 'turn-a', status: 'completed' })
    expect(result.current.requestStatus).toBe('completed')
  })

  it('an immediate turn refuses a completed answer without reposting', async () => {
    postMessage.mockResolvedValue({ status: 'completed', turn_id: 'turn-a', request_id: 'request-a' })
    const { result } = projectController()
    await act(async () => {
      await expect(result.current.startTurn('hello', {}, { requireImmediateTurn: true })).rejects.toThrow('already finished')
    })
    expect(postMessage).toHaveBeenCalledTimes(1)
  })

  it('keeps ordinary project chat queued with a request identity', async () => {
    postMessage.mockResolvedValue({ status: 'queued', request_id: 'request-a' })
    const { result } = projectController()
    await act(async () => { await result.current.startTurn('hello') })
    expect(postMessage).toHaveBeenCalledTimes(1)
    expect(postMessage.mock.calls[0][1]).toMatchObject({ queue: true, request_id: expect.any(String) })
  })

  it('requests an immediate turn and returns the session actually sent to', async () => {
    postMessage.mockResolvedValue({ status: 'started', turn_id: 'turn-a' })
    const { result } = projectController()
    let response
    await act(async () => { response = await result.current.startTurn('author', {}, { requireImmediateTurn: true }) })
    expect(postMessage.mock.calls[0]).toEqual(['session-a', expect.objectContaining({ queue: false, request_id: expect.any(String) })])
    expect(response).toMatchObject({ session_id: 'session-a', turn_id: 'turn-a' })
  })

  it('rejects a queued 202 shape without posting a second message', async () => {
    postMessage.mockResolvedValue({ status: 'queued', request_id: 'request-a', active_requests: { queued: 1, executing: 1, total: 2 } })
    const { result } = projectController()
    await act(async () => {
      await expect(result.current.startTurn('author', {}, { requireImmediateTurn: true })).rejects.toThrow('has not started')
    })
    expect(postMessage).toHaveBeenCalledTimes(1)
  })

  it('does not retry a busy refusal', async () => {
    postMessage.mockRejectedValue(new Error('busy'))
    const { result } = projectController()
    await act(async () => {
      await expect(result.current.startTurn('author', {}, { requireImmediateTurn: true })).rejects.toThrow('busy')
    })
    expect(postMessage).toHaveBeenCalledTimes(1)
  })

  it('rejects an old response after the project changes', async () => {
    let resolveResponse
    postMessage.mockImplementation(() => new Promise((resolve) => { resolveResponse = resolve }))
    const { result } = projectController()
    let pending
    await act(async () => {
      pending = result.current.startTurn('author', {}, { requireImmediateTurn: true }).catch((error) => error)
      await Promise.resolve()
      await Promise.resolve()
    })
    act(() => result.current.setProjectContext('project-b'))
    await act(async () => {
      resolveResponse({ status: 'started', turn_id: 'old-turn' })
      expect(await pending).toBeInstanceOf(Error)
    })
    expect(postMessage).toHaveBeenCalledTimes(1)
  })
})
