// The bounded 401-recovery contract for the post-callback token race
// (2026-08-17 UPDATE 9): retry when a token appears after a pre-token 401,
// recheck on a fixed bounded schedule while a code exchange is in flight,
// latch (fail closed) everywhere else. Every branch here maps to a decision
// the old code got wrong by latching unconditionally.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createTokenRaceRecovery, RECHECK_DELAYS_MS } from './tokenRaceRecovery.js'

const TOTAL_RECHECK_MS = RECHECK_DELAYS_MS.reduce((a, b) => a + b, 0)

function harness({ signedIn = false, callbackInFlight = false, maxRecoveries } = {}) {
  const state = { signedIn, callbackInFlight }
  const onRetry = vi.fn()
  const onLatch = vi.fn()
  const recovery = createTokenRaceRecovery({
    isSignedIn: () => state.signedIn,
    isCallbackInFlight: () => state.callbackInFlight,
    ...(maxRecoveries === undefined ? {} : { maxRecoveries }),
  })
  const decide = (hadTokenAtSend = false) =>
    recovery.handle401({ hadTokenAtSend, onRetry, onLatch })
  return { state, recovery, onRetry, onLatch, decide }
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('createTokenRaceRecovery', () => {
  it('latches immediately when the failed request carried a bearer', () => {
    const { decide, onRetry, onLatch } = harness({ signedIn: true, callbackInFlight: true })
    expect(decide(true)).toBe('latch')
    expect(onLatch).toHaveBeenCalledTimes(1)
    expect(onRetry).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('retries synchronously when the token appeared while the request was in flight', () => {
    const { decide, onRetry, onLatch } = harness({ signedIn: true })
    expect(decide(false)).toBe('retry')
    expect(onRetry).toHaveBeenCalledTimes(1)
    expect(onLatch).not.toHaveBeenCalled()
  })

  it('latches immediately with no token and no code exchange in flight (signed-out visitors keep the calm gate, undelayed)', () => {
    const { decide, onRetry, onLatch } = harness()
    expect(decide(false)).toBe('latch')
    expect(onLatch).toHaveBeenCalledTimes(1)
    expect(onRetry).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('rechecks while an exchange is in flight and retries at the first token sighting', () => {
    const { state, decide, onRetry, onLatch } = harness({ callbackInFlight: true })
    expect(decide(false)).toBe('recheck')
    vi.advanceTimersByTime(RECHECK_DELAYS_MS[0])
    expect(onRetry).not.toHaveBeenCalled() // no token yet -> keep waiting
    state.signedIn = true // the exchange landed leaf.jwt (~700ms class)
    vi.advanceTimersByTime(RECHECK_DELAYS_MS[1])
    expect(onRetry).toHaveBeenCalledTimes(1)
    expect(onLatch).not.toHaveBeenCalled()
    vi.advanceTimersByTime(TOTAL_RECHECK_MS)
    expect(onRetry).toHaveBeenCalledTimes(1) // the chain stopped: no busy loop
    expect(vi.getTimerCount()).toBe(0)
  })

  it('fails closed after the full bounded schedule when the token never appears', () => {
    const { decide, onRetry, onLatch } = harness({ callbackInFlight: true })
    expect(decide(false)).toBe('recheck')
    vi.advanceTimersByTime(TOTAL_RECHECK_MS - 1)
    expect(onLatch).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(onLatch).toHaveBeenCalledTimes(1)
    expect(onRetry).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('caps total recoveries: beyond maxRecoveries every 401 latches even with a token present', () => {
    const { decide, onRetry, onLatch } = harness({ signedIn: true, maxRecoveries: 2 })
    expect(decide(false)).toBe('retry')
    expect(decide(false)).toBe('retry')
    expect(decide(false)).toBe('latch')
    expect(onRetry).toHaveBeenCalledTimes(2)
    expect(onLatch).toHaveBeenCalledTimes(1)
  })

  it('counts recheck rounds against the same cap', () => {
    const { state, decide, onLatch } = harness({ callbackInFlight: true, maxRecoveries: 1 })
    expect(decide(false)).toBe('recheck')
    vi.advanceTimersByTime(TOTAL_RECHECK_MS) // never signs in -> latch
    expect(onLatch).toHaveBeenCalledTimes(1)
    state.signedIn = false
    expect(decide(false)).toBe('latch') // cap consumed; no second recheck chain
    expect(vi.getTimerCount()).toBe(0)
  })

  it('a new 401 decision supersedes a pending recheck chain (single timer invariant)', () => {
    const { decide, onLatch } = harness({ callbackInFlight: true })
    expect(decide(false)).toBe('recheck')
    expect(decide(true)).toBe('latch') // supersedes: old chain cancelled
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(TOTAL_RECHECK_MS)
    expect(onLatch).toHaveBeenCalledTimes(1) // only the superseding latch fired
  })

  it('cancel() clears the pending chain without killing the instance', () => {
    const { state, recovery, decide, onRetry, onLatch } = harness({ callbackInFlight: true })
    expect(decide(false)).toBe('recheck')
    recovery.cancel()
    vi.advanceTimersByTime(TOTAL_RECHECK_MS)
    expect(onRetry).not.toHaveBeenCalled()
    expect(onLatch).not.toHaveBeenCalled()
    state.signedIn = true
    expect(decide(false)).toBe('retry') // still usable after cancel
  })

  it('dispose() is terminal: pending chain dies and later 401s are inert', () => {
    const { recovery, decide, onRetry, onLatch } = harness({ callbackInFlight: true })
    expect(decide(false)).toBe('recheck')
    recovery.dispose()
    vi.advanceTimersByTime(TOTAL_RECHECK_MS)
    expect(decide(false)).toBe('disposed')
    expect(onRetry).not.toHaveBeenCalled()
    expect(onLatch).not.toHaveBeenCalled()
  })

  it('fails closed on construction without its two probes', () => {
    expect(() => createTokenRaceRecovery({})).toThrow(TypeError)
    expect(() => createTokenRaceRecovery()).toThrow(TypeError)
  })
})
