// The R5 stage POST fail-closes server-side without turn authority (409
// stage_authority_invalid). This pins the controller's half of the fix: an
// injected authority provider is consulted once per fresh stage submission
// and its result rides the stageAuthorTool call — never on a poll/reconnect,
// and never submitted when the provider has not acquired authority.
import { describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'

import useAuthorStageController, { INFLIGHT_AUTHOR_KEY } from './useAuthorStageController.js'

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

  // Demo authority refusal is a persistent limit; live mode keeps its turn advice.
  it.each([false, true])('does not submit when the provider returns null (mock: %s)', async (mock) => {
    const authorityProvider = vi.fn(async () => null)
    const stageAuthorTool = vi.fn(async () => staged())
    const { result } = renderHook(() => useAuthorStageController({
      mock, storage: memoryStorage(), stageAuthorTool, authorityProvider,
    }))

    await act(async () => { await result.current.stage('count panels near the ridge line') })

    expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(stageAuthorTool).not.toHaveBeenCalled()
    expect(result.current.error.message).toBe(mock
      ? 'Tool building is unavailable in this signed-out demo.'
      : 'Could not start authoring in this conversation. Wait for the current turn to finish, then try again.')
    expect(result.current.error.description).toBe('count panels near the ridge line')
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

  // The server binds a staged request to the turn that first sent it: the same
  // idempotency key with any other turn is refused as a different request. So
  // the controller repeats a request whose answer never arrived with the
  // authority it minted for it, and never starts a second turn to do so.
  const TURN_1 = { sessionId: 'session-1', turnId: 'turn-1' }

  // A stage request whose first answer is lost before the server's acceptance
  // reaches the client; every later call succeeds.
  function answerLostOnce() {
    let attempts = 0
    return vi.fn(async () => {
      attempts += 1
      if (attempts === 1) throw new Error('connection lost before the answer')
      return staged()
    })
  }

  it('C8B-22 a repeat of a request whose answer was lost carries the authority it was first sent with', async () => {
    const authorityProvider = vi.fn(async () => ({ ...TURN_1 }))
    const stageAuthorTool = answerLostOnce()
    const { result } = renderHook(() => useAuthorStageController({
      storage: memoryStorage(), stageAuthorTool, authorityProvider,
    }))
    await act(async () => { await result.current.stage('organize recipes') })
    expect(result.current.phase).toBe('interrupted')
    expect(result.current.resumable).toBe(true)

    await act(async () => { await result.current.resume() })

    expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(stageAuthorTool).toHaveBeenCalledTimes(2)
    const [sent, repeat] = stageAuthorTool.mock.calls.map((call) => call[3])
    expect(sent.authority).toEqual(TURN_1)
    expect(repeat.authority).toEqual(TURN_1)
    expect(repeat.idempotencyKey).toBe(sent.idempotencyKey)
    expect(repeat.pollUrl).toBeNull()
    expect(result.current.phase).toBe('succeeded')
  })

  it('C8B-23 the repeat goes out while the conversation is still busy with that turn', async () => {
    // What both shells answer once the first turn is active: no authority. A
    // controller that asked again would end the request here and forget it.
    let mints = 0
    const authorityProvider = vi.fn(async () => {
      mints += 1
      return mints === 1 ? { ...TURN_1 } : null
    })
    const stageAuthorTool = answerLostOnce()
    const { result } = renderHook(() => useAuthorStageController({
      storage: memoryStorage(), stageAuthorTool, authorityProvider,
    }))
    await act(async () => { await result.current.stage('organize recipes') })
    const saved = result.current.pointer.idempotency_key

    await act(async () => { await result.current.resume() })

    expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(stageAuthorTool).toHaveBeenCalledTimes(2)
    expect(stageAuthorTool.mock.calls[1][3].authority).toEqual(TURN_1)
    expect(stageAuthorTool.mock.calls[1][3].idempotencyKey).toBe(saved)
    expect(result.current.error).toBeNull()
    expect(result.current.phase).toBe('succeeded')
    expect(result.current.pointer.idempotency_key).toBe(saved)
  })

  it('C8B-24 a request made after a refusal starts its own turn under its own key', async () => {
    let mints = 0
    const authorityProvider = vi.fn(async () => {
      mints += 1
      return { sessionId: 'session-1', turnId: `turn-${mints}` }
    })
    let attempts = 0
    const stageAuthorTool = vi.fn(async () => {
      attempts += 1
      if (attempts === 1) {
        throw Object.assign(new Error('authoring authority is no longer valid'), { status: 409, authorTerminal: true })
      }
      return staged()
    })
    const { result } = renderHook(() => useAuthorStageController({
      storage: memoryStorage(), stageAuthorTool, authorityProvider,
    }))
    await act(async () => { await result.current.stage('organize recipes') })
    expect(result.current.phase).toBe('failed')
    expect(result.current.pointer).toBeNull()

    await act(async () => { await result.current.stage('organize recipes') })

    expect(authorityProvider).toHaveBeenCalledTimes(2)
    const [refused, next] = stageAuthorTool.mock.calls.map((call) => call[3])
    expect(refused.authority).toEqual({ sessionId: 'session-1', turnId: 'turn-1' })
    expect(next.authority).toEqual({ sessionId: 'session-1', turnId: 'turn-2' })
    expect(next.idempotencyKey).not.toBe(refused.idempotencyKey)
    expect(result.current.phase).toBe('succeeded')
  })

  // Leaves one saved request with no acceptance in `storage`, as a page that
  // lost the answer and was then closed would.
  async function savedUnanswered(storage, description = 'organize recipes') {
    const sent = []
    const first = renderHook(() => useAuthorStageController({
      storage,
      stageAuthorTool: async (_mock, _text, _target, options) => {
        sent.push(options)
        throw new Error('connection lost before the answer')
      },
      authorityProvider: async () => ({ ...TURN_1 }),
    }))
    await act(async () => { await first.result.current.stage(description) })
    first.unmount()
    expect(sent).toHaveLength(1)
    return sent[0].idempotencyKey
  }

  it('C8B-25 an authority that arrives for a superseded run never replaces the one a newer run sent', async () => {
    const storage = memoryStorage()
    const saved = await savedUnanswered(storage)
    let releaseResume
    let mints = 0
    const authorityProvider = vi.fn(() => {
      mints += 1
      if (mints === 1) return new Promise((resolve) => { releaseResume = resolve })
      return Promise.resolve({ sessionId: 'session-1', turnId: 'turn-newer' })
    })
    const stageAuthorTool = answerLostOnce()
    const { result } = renderHook(() => useAuthorStageController({
      storage, stageAuthorTool, authorityProvider,
    }))
    // The reload's own resume is still starting its turn when the drafter
    // submits again: the newer run mints, sends, and loses its answer.
    await act(async () => { await result.current.stage('organize recipes') })
    expect(result.current.phase).toBe('interrupted')
    // Only now does the superseded run's turn start answer.
    await act(async () => { releaseResume({ sessionId: 'session-1', turnId: 'turn-superseded' }) })

    await act(async () => { await result.current.resume() })

    expect(authorityProvider).toHaveBeenCalledTimes(2)
    expect(stageAuthorTool).toHaveBeenCalledTimes(2)
    const [sent, repeat] = stageAuthorTool.mock.calls.map((call) => call[3])
    expect(sent.idempotencyKey).toBe(saved)
    expect(repeat.idempotencyKey).toBe(saved)
    expect(sent.authority).toEqual({ sessionId: 'session-1', turnId: 'turn-newer' })
    expect(repeat.authority).toEqual({ sessionId: 'session-1', turnId: 'turn-newer' })
    expect(result.current.phase).toBe('succeeded')
  })

  it('C8B-26 a controller that did not send the request holds nothing for it', async () => {
    // After a reload the first authority is gone with the page that minted it,
    // so the saved request is repeated the only way left: with a new turn.
    const storage = memoryStorage()
    const saved = await savedUnanswered(storage)
    const authorityProvider = vi.fn(async () => ({ sessionId: 'session-1', turnId: 'turn-after-reload' }))
    const stageAuthorTool = vi.fn(async () => staged())
    const { result } = renderHook(() => useAuthorStageController({
      storage, stageAuthorTool, authorityProvider,
    }))
    await waitFor(() => expect(result.current.phase).toBe('succeeded'))
    expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(authorityProvider).toHaveBeenCalledWith('organize recipes', { allowSecretOnce: false })
    expect(stageAuthorTool).toHaveBeenCalledTimes(1)
    expect(stageAuthorTool.mock.calls[0][3].idempotencyKey).toBe(saved)
    expect(stageAuthorTool.mock.calls[0][3].authority).toEqual({ sessionId: 'session-1', turnId: 'turn-after-reload' })
  })

  it('C8B-28 only an authority this controller minted is held: a request first sent with no provider mints when one arrives', async () => {
    const authorityProvider = vi.fn(async () => ({ ...TURN_1 }))
    const stageAuthorTool = answerLostOnce()
    const storage = memoryStorage()
    const { result, rerender } = renderHook(
      ({ provider }) => useAuthorStageController({ storage, stageAuthorTool, authorityProvider: provider }),
      { initialProps: { provider: undefined } },
    )
    await act(async () => { await result.current.stage('organize recipes') })
    expect(result.current.phase).toBe('interrupted')
    expect(stageAuthorTool.mock.calls[0][3].authority).toBeNull()

    rerender({ provider: authorityProvider })
    await act(async () => { await result.current.resume() })

    // Nothing was minted for the first send, so nothing is held for its key: the repeat asks
    // the provider it now has, instead of repeating the first send's missing authority.
    expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(stageAuthorTool).toHaveBeenCalledTimes(2)
    const [sent, repeat] = stageAuthorTool.mock.calls.map((call) => call[3])
    expect(repeat.idempotencyKey).toBe(sent.idempotencyKey)
    expect(repeat.authority).toEqual(TURN_1)
    expect(result.current.phase).toBe('succeeded')
  })

  it('C8B-32 the held authority lives in memory only: no storage write ever carries it', async () => {
    const HELD = { sessionId: 'session-held-7f3a', turnId: 'turn-held-9c1e' }
    const store = memoryStorage()
    const writes = []
    const storage = {
      getItem: store.getItem,
      setItem: (key, value) => { writes.push([key, String(value)]); store.setItem(key, value) },
      removeItem: store.removeItem,
    }
    const authorityProvider = vi.fn(async () => ({ ...HELD }))
    const stageAuthorTool = answerLostOnce()
    const { result } = renderHook(() => useAuthorStageController({
      mock: false, storage, stageAuthorTool, authorityProvider,
    }))
    await act(async () => { await result.current.stage('organize recipes') })
    expect(result.current.phase).toBe('interrupted')

    await act(async () => { await result.current.resume() })

    // The repeat was sent with the authority the controller held, so there was one to leak.
    expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(stageAuthorTool.mock.calls[1][3].authority).toEqual(HELD)
    // The request itself is saved (that is what Resume repeats) under the one pointer key, and
    // nothing written anywhere names the session or the turn.
    expect(writes.length).toBeGreaterThan(0)
    expect([...new Set(writes.map(([key]) => key))]).toEqual([INFLIGHT_AUTHOR_KEY])
    for (const [key, value] of writes) {
      expect(`${key} ${value}`).not.toMatch(/session-held-7f3a|turn-held-9c1e/)
    }
    expect(storage.getItem(INFLIGHT_AUTHOR_KEY) || '').not.toMatch(/session-held-7f3a|turn-held-9c1e/)
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
    // stage() re-runs a persisted pointer whose first POST may not have landed
    // (no poll_url). The controller that minted for it repeats the request
    // with that same authority, so no second turn starts; the override still
    // has to reach the repeated request, whose own guard reads the text again.
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
    expect(authorityProvider).not.toHaveBeenCalled()
    expect(stageAuthorTool).toHaveBeenCalledTimes(2)
    const repeat = stageAuthorTool.mock.calls[1][3]
    expect(repeat.allowSecretOnce).toBe(true)
    expect(repeat.authority).toEqual({ sessionId: 'session-1', turnId: 'turn-1' })
    expect(repeat.idempotencyKey).toBe(stageAuthorTool.mock.calls[0][3].idempotencyKey)
  })

  it('C8B-27 the override reaches the mint when a re-run has nothing held', async () => {
    // After a reload nothing is held, so a re-run of the saved request mints.
    // Send anyway pressed while the reload's own resume is still starting its
    // turn supersedes that resume: the mint it makes must carry the override.
    const storage = memoryStorage()
    const first = renderHook(() => useAuthorStageController({
      mock: false, storage,
      stageAuthorTool: async () => { throw new Error('network down before accept') },
      authorityProvider: async () => ({ sessionId: 'session-1', turnId: 'turn-1' }),
    }))
    await act(async () => {
      await first.result.current.stage(FAKE_GENERIC, null, { allowSecretOnce: true }).catch(() => null)
    })
    first.unmount()

    let releaseResume
    let mints = 0
    const authorityProvider = vi.fn(() => {
      mints += 1
      if (mints === 1) return new Promise((resolve) => { releaseResume = resolve })
      return Promise.resolve({ sessionId: 'session-2', turnId: 'turn-2' })
    })
    const stageAuthorTool = vi.fn(async () => staged())
    const { result } = renderHook(() => useAuthorStageController({
      mock: false, storage, stageAuthorTool, authorityProvider,
    }))
    expect(authorityProvider).toHaveBeenCalledTimes(1)
    await act(async () => {
      await result.current.stage(FAKE_GENERIC, null, { allowSecretOnce: true })
    })
    await act(async () => { releaseResume({ sessionId: 'session-late', turnId: 'turn-late' }) })
    expect(authorityProvider.mock.calls).toEqual([
      [FAKE_GENERIC, { allowSecretOnce: false }],
      [FAKE_GENERIC, { allowSecretOnce: true }],
    ])
    expect(stageAuthorTool).toHaveBeenCalledTimes(1)
    expect(stageAuthorTool.mock.calls[0][3].allowSecretOnce).toBe(true)
    expect(stageAuthorTool.mock.calls[0][3].authority).toEqual({ sessionId: 'session-2', turnId: 'turn-2' })
    expect(result.current.phase).toBe('succeeded')
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
