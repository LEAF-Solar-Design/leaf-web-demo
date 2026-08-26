// The R5 stage POST fail-closes server-side without turn authority (409
// stage_authority_invalid). This pins the controller's half of the fix: an
// injected authority provider is consulted once per fresh stage submission
// and its result rides the stageAuthorTool call — never on a poll/reconnect,
// and never surfaced as a client-side refusal when the provider comes back
// empty (the server still fail-closes on its own).
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
    expect(authorityProvider).toHaveBeenCalledWith('count panels near the ridge line')
    expect(stageAuthorTool).toHaveBeenCalledTimes(1)
    const opts = stageAuthorTool.mock.calls[0][3]
    expect(opts.authority).toEqual({ sessionId: 'session-1', turnId: 'turn-1' })
    expect(result.current.phase).toBe('succeeded')
  })

  it('proceeds with no authority (never inventing a client-side refusal) when the provider returns null', async () => {
    const authorityProvider = vi.fn(async () => null)
    const stageAuthorTool = vi.fn(async () => staged())
    const { result } = renderHook(() => useAuthorStageController({
      mock: false, storage: memoryStorage(), stageAuthorTool, authorityProvider,
    }))

    await act(async () => { await result.current.stage('count panels near the ridge line') })

    expect(authorityProvider).toHaveBeenCalledTimes(1)
    const opts = stageAuthorTool.mock.calls[0][3]
    expect(opts.authority).toBeNull()
    expect(result.current.phase).toBe('succeeded')
  })

  it('proceeds with no authority when the provider throws', async () => {
    const authorityProvider = vi.fn(async () => { throw new Error('mint failed') })
    const stageAuthorTool = vi.fn(async () => staged())
    const { result } = renderHook(() => useAuthorStageController({
      mock: false, storage: memoryStorage(), stageAuthorTool, authorityProvider,
    }))

    await act(async () => { await result.current.stage('count panels near the ridge line') })

    const opts = stageAuthorTool.mock.calls[0][3]
    expect(opts.authority).toBeNull()
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
})
