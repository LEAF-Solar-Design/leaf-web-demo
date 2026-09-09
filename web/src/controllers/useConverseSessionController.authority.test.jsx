import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
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

describe('author immediate-turn contract', () => {
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
