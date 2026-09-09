// The R5 stage POST fail-closes server-side without turn authority (409
// stage_authority_invalid). This pins the controller's half of the fix: an
// injected authority provider is consulted once per fresh stage submission
// and its result rides the stageAuthorTool call — never on a poll/reconnect,
// and never submitted when the provider has not acquired authority.
import { describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'

import useAuthorStageController from './useAuthorStageController.js'

function memoryStorage() {
  const map = new Map()
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
  }
}

function staged() {
  return { tool: { name: 'demo_tool' }, receipt: { change_set_id: 'cs-1', state: 'staged' } }
}

describe('useAuthorStageController turn-authority provider', () => {
  it('calls the provider once and passes its result to stageAuthorTool', async () => {
    const authorityProvider = vi.fn(async () => ({ sessionId: 'session-1', turnId: 'turn-1' }))
    const stageAuthorTool = vi.fn(async () => staged())
    const { result } = renderHook(() => useAuthorStageController({
      mock: false, storage: memoryStorage(), stageAuthorTool, authorityProvider,
    }))

    await act(async () => { await result.current.stage('count panels near the ridge line') })

    expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(authorityProvider).toHaveBeenCalledWith('count panels near the ridge line', { allowSecretOnce: false })
    expect(stageAuthorTool).toHaveBeenCalledTimes(1)
    const opts = stageAuthorTool.mock.calls[0][3]
    expect(opts.authority).toEqual({ sessionId: 'session-1', turnId: 'turn-1' })
    expect(result.current.phase).toBe('succeeded')
  })

  it('does not submit when the provider returns null', async () => {
    const authorityProvider = vi.fn(async () => null)
    const stageAuthorTool = vi.fn(async () => staged())
    const { result } = renderHook(() => useAuthorStageController({
      mock: false, storage: memoryStorage(), stageAuthorTool, authorityProvider,
    }))

    await act(async () => { await result.current.stage('count panels near the ridge line') })

    expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(stageAuthorTool).not.toHaveBeenCalled()
    expect(result.current.error.message).toContain('Could not start authoring')
    expect(result.current.pointer).toBeNull()
    await act(async () => { await result.current.resume() })
    expect(authorityProvider).toHaveBeenCalledTimes(1)
  })

  it('preserves the provider error without submitting', async () => {
    const authorityProvider = vi.fn(async () => { throw new Error('mint failed') })
    const stageAuthorTool = vi.fn(async () => staged())
    const { result } = renderHook(() => useAuthorStageController({
      mock: false, storage: memoryStorage(), stageAuthorTool, authorityProvider,
    }))

    await act(async () => { await result.current.stage('count panels near the ridge line') })

    expect(stageAuthorTool).not.toHaveBeenCalled()
    expect(result.current.error.message).toBe('mint failed')
    expect(result.current.pointer).toBeNull()
  })

  it('does not submit an incomplete authority tuple', async () => {
    const stageAuthorTool = vi.fn(async () => staged())
    const { result } = renderHook(() => useAuthorStageController({
      storage: memoryStorage(), stageAuthorTool,
      authorityProvider: async () => ({ sessionId: 'session-1' }),
    }))
    await act(async () => { await result.current.stage('organize recipes') })
    expect(stageAuthorTool).not.toHaveBeenCalled()
  })

  it('does not submit when authority arrives after unmount', async () => {
    let resolveAuthority
    const authorityProvider = vi.fn(() => new Promise((resolve) => { resolveAuthority = resolve }))
    const stageAuthorTool = vi.fn(async () => staged())
    const { result, unmount } = renderHook(() => useAuthorStageController({
      storage: memoryStorage(), stageAuthorTool, authorityProvider,
    }))
    let pending
    act(() => { pending = result.current.stage('organize recipes') })
    unmount()
    await act(async () => {
      resolveAuthority({ sessionId: 'session-1', turnId: 'turn-1' })
      await pending
    })
    expect(stageAuthorTool).not.toHaveBeenCalled()
  })

  it('reconnects an accepted request without minting another turn', async () => {
    const authorityProvider = vi.fn(async () => ({ sessionId: 'session-1', turnId: 'turn-1' }))
    let attempts = 0
    const stageAuthorTool = vi.fn(async (_mock, _text, _target, options) => {
      attempts += 1
      if (attempts === 1) {
        options.onAccepted({ change_set_id: 'cs-1', poll_url: '/api/author/jobs/cs-1', retry_after_ms: 1 })
        throw new Error('connection lost')
      }
      expect(options.pollUrl).toBe('/api/author/jobs/cs-1')
      expect(options.authority).toBeNull()
      return staged()
    })
    const { result } = renderHook(() => useAuthorStageController({
      storage: memoryStorage(), stageAuthorTool, authorityProvider,
    }))
    await act(async () => { await result.current.stage('organize recipes') })
    await act(async () => { await result.current.resume() })
    expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(result.current.phase).toBe('succeeded')
  })

  it('is absent-safe: byte-identical behavior with no provider supplied', async () => {
    const stageAuthorTool = vi.fn(async () => staged())
    const { result } = renderHook(() => useAuthorStageController({
      mock: false, storage: memoryStorage(), stageAuthorTool,
    }))

    await act(async () => { await result.current.stage('count panels near the ridge line') })

    const opts = stageAuthorTool.mock.calls[0][3]
    expect(opts.authority).toBeNull()
    expect(result.current.phase).toBe('succeeded')
  })

  // Regression pin (S8a fix round 4): an AuthorPanel "Send anyway" call
  // forwards allowSecretOnce to `stage`, and that authorisation must reach
  // the authority mint too. A provider that ignores the second argument
  // guards its own POST on the SAME wire (the converse turn start), so
  // dropping the flag here would refuse the mint and silently swallow the
  // override into a null-authority fallback -- the override never actually
  // landed on the call it was meant to authorise.
  //
  // The generic ("api_key: xxxx...") shape, not an AWS-style key: that shape
  // is the ONE overridable pattern (evaluateSecretGuard fails every other
  // shape closed, with no way past it), so it is the only text for which
  // `stage(..., { allowSecretOnce: true })` clears the storage-boundary guard
  // and actually reaches the authority mint this test is pinning.
  const FAKE_GENERIC = `api_key: ${'x'.repeat(24)}`

  it('forwards allowSecretOnce on the re-run of a valid pointer that never got its poll_url', async () => {
    // The gap a lens found one path over: stage() re-runs a persisted pointer
    // whose first POST never landed (no poll_url), and that re-run mints again.
    const authorityProvider = vi.fn(async () => ({ sessionId: 'session-1', turnId: 'turn-1' }))
    let calls = 0
    const stageAuthorTool = vi.fn(async () => {
      calls += 1
      if (calls === 1) throw new Error('network down before accept')
      return staged()
    })
    const storage = memoryStorage()
    const { result } = renderHook(() => useAuthorStageController({
      mock: false, storage, stageAuthorTool, authorityProvider,
    }))
    await act(async () => {
      await result.current.stage(FAKE_GENERIC, null, { allowSecretOnce: true }).catch(() => null)
    })
    authorityProvider.mockClear()
    await act(async () => {
      await result.current.stage(FAKE_GENERIC, null, { allowSecretOnce: true })
    })
    expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(authorityProvider).toHaveBeenCalledWith(FAKE_GENERIC, { allowSecretOnce: true })
  })
  it('forwards allowSecretOnce to the authority provider on a Send-anyway re-stage', async () => {
    const authorityProvider = vi.fn(async () => ({ sessionId: 'session-1', turnId: 'turn-1' }))
    const stageAuthorTool = vi.fn(async () => staged())
    const { result } = renderHook(() => useAuthorStageController({
      mock: false, storage: memoryStorage(), stageAuthorTool, authorityProvider,
    }))

    await act(async () => {
      await result.current.stage(FAKE_GENERIC, null, { allowSecretOnce: true })
    })

    expect(authorityProvider).toHaveBeenCalledWith(FAKE_GENERIC, { allowSecretOnce: true })
  })
})
