import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'
import { stageAuthorTool as realStageAuthorTool } from '../api.js'
import { INFLIGHT_AUTHOR_KEY, readInflightAuthor } from '../authorStagePointer.js'
import useAuthorStageController from './useAuthorStageController.js'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

const changeSetId = '24b18e9d-3aaa-4e75-b443-6cab404ffd4c'
const pollUrl = `/api/author/stages/${changeSetId}`
const description = 'Validate campaign host enrollment evidence.'

function memoryStorage() {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  }
}

function failure() {
  return Object.assign(new Error('PROVIDER private details'), {
    authorTerminal: true, status: 500,
    body: {
      change_set_id: changeSetId, status: 'failed', poll_url: pollUrl,
      error: { reason_code: 'customization_author_job_failed', retryable: true },
      headers: { authorization: 'PROVIDER secret' }, payload: 'PROVIDER raw output',
    },
  })
}

async function createFailed() {
  const storage = memoryStorage()
  const stageAuthorTool = vi.fn(async (_mock, _description, _target, opts) => {
    opts.onAccepted({ change_set_id: changeSetId, poll_url: pollUrl, retry_after_ms: 1 })
    throw failure()
  })
  const authorityProvider = vi.fn(async () => ({ sessionId: 'session-1', turnId: 'turn-1' }))
  const hook = renderHook(() => useAuthorStageController({ storage, stageAuthorTool, authorityProvider }))
  await act(async () => { await hook.result.current.stage(description) })
  return { storage, stageAuthorTool, authorityProvider, hook }
}

describe('failed author recovery', () => {
  it('keeps the accepted request identity and bounded metadata after terminal failure', async () => {
    const { storage, stageAuthorTool, hook } = await createFailed()
    const pointer = readInflightAuthor(storage)
    expect(pointer).toMatchObject({
      idempotency_key: stageAuthorTool.mock.calls[0][3].idempotencyKey,
      change_set_id: changeSetId, poll_url: pollUrl, terminal_failed: true,
      failure: { status: 500, reason_code: 'customization_author_job_failed' },
    })
    expect(hook.result.current.phase).toBe('failed')
    expect(hook.result.current.failedRequest.change_set_id).toBe(changeSetId)
    const raw = storage.getItem(INFLIGHT_AUTHOR_KEY)
    for (const value of ['PROVIDER', 'authorization', 'headers', 'payload']) expect(raw).not.toContain(value)
  })

  it('restores a failed request after reload without authoring or minting authority', async () => {
    const { storage, hook } = await createFailed()
    hook.unmount()
    const stageAuthorTool = vi.fn()
    const authorityProvider = vi.fn()
    const restored = renderHook(() => useAuthorStageController({ storage, stageAuthorTool, authorityProvider }))
    await act(async () => {})
    expect(restored.result.current.phase).toBe('failed')
    expect(restored.result.current.pointer).toMatchObject({ change_set_id: changeSetId, description })
    expect(restored.result.current.error).toMatchObject({ authorTerminal: true, restored: true })
    expect(restored.result.current.error.status).toBeUndefined()
    expect(stageAuthorTool).not.toHaveBeenCalled()
    expect(authorityProvider).not.toHaveBeenCalled()
  })

  it('checks the original status through the real transport with one GET and no POST or authority mint', async () => {
    const { storage, hook } = await createFailed()
    const original = readInflightAuthor(storage)
    hook.unmount()
    const fetch = vi.fn(async () => new Response(JSON.stringify({
      contract: 'leaf.customization-stage-job.v1', change_set_id: changeSetId,
      status: 'failed', attempt: 1, phase: 'failed', poll_url: pollUrl,
      error: { reason_code: 'customization_author_job_failed', retryable: true },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetch)
    const authorityProvider = vi.fn()
    const restored = renderHook(() => useAuthorStageController({ storage, authorityProvider, stageAuthorTool: realStageAuthorTool }))
    await act(async () => { await restored.result.current.checkStatus() })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(String(fetch.mock.calls[0][0])).toBe(new URL(pollUrl, window.location.origin).href)
    expect(fetch.mock.calls[0][1]?.method ?? 'GET').toBe('GET')
    expect(authorityProvider).not.toHaveBeenCalled()
    expect(readInflightAuthor(storage)).toMatchObject({
      idempotency_key: original.idempotency_key, change_set_id: changeSetId,
      poll_url: pollUrl, terminal_failed: true,
    })
  })

  it('requires an explicit new attempt and retains the prior failed identity', async () => {
    const { storage, stageAuthorTool, authorityProvider, hook } = await createFailed()
    const original = readInflightAuthor(storage)
    stageAuthorTool.mockClear(); authorityProvider.mockClear()
    await act(async () => { await hook.result.current.stage(description) })
    expect(stageAuthorTool).not.toHaveBeenCalled()
    expect(authorityProvider).not.toHaveBeenCalled()
    await act(async () => { await hook.result.current.stage(description, null, { newAttempt: true }) })
    expect(stageAuthorTool).toHaveBeenCalledTimes(1)
    expect(authorityProvider).toHaveBeenCalledTimes(1)
    const opts = stageAuthorTool.mock.calls[0][3]
    expect(opts.idempotencyKey).not.toBe(original.idempotency_key)
    expect(opts.pollUrl).toBeNull()
    expect(readInflightAuthor(storage).prior_failure).toEqual({
      idempotency_key: original.idempotency_key, change_set_id: changeSetId,
      poll_url: pollUrl, failed_at: original.failed_at,
    })
  })

  it('keeps a nonterminal connection failure resumable', async () => {
    const storage = memoryStorage()
    const stageAuthorTool = vi.fn(async () => { throw new Error('Connection lost') })
    const { result } = renderHook(() => useAuthorStageController({ storage, stageAuthorTool }))
    await act(async () => { await result.current.stage(description) })
    expect(result.current.phase).toBe('interrupted')
    expect(result.current.resumable).toBe(true)
    expect(result.current.failedRequest).toBeNull()
    expect(readInflightAuthor(storage)).not.toBeNull()
  })

  it.each(['account', 'expiry'])('does not restore a failed request outside its %s boundary', async (boundary) => {
    const { storage, hook } = await createFailed()
    hook.unmount()
    if (boundary === 'account') storage.setItem('leaf.org_id', 'another-org')
    else {
      const pointer = readInflightAuthor(storage)
      storage.setItem(INFLIGHT_AUTHOR_KEY, JSON.stringify({ ...pointer, expires_at: Date.now() - 1 }))
    }
    const stageAuthorTool = vi.fn()
    const { result } = renderHook(() => useAuthorStageController({ storage, stageAuthorTool }))
    expect(result.current.pointer).toBeNull()
    expect(stageAuthorTool).not.toHaveBeenCalled()
    expect(storage.getItem(INFLIGHT_AUTHOR_KEY)).toBeNull()
  })

  it('rejects malformed persisted failure metadata', async () => {
    const { storage } = await createFailed()
    const pointer = readInflightAuthor(storage)
    storage.setItem(INFLIGHT_AUTHOR_KEY, JSON.stringify({ ...pointer, failure: { ...pointer.failure, headers: 'private' } }))
    expect(readInflightAuthor(storage)).toBeNull()
  })
})
