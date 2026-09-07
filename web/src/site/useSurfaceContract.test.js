// @vitest-environment jsdom
//
// Standardization slice 7b (web half). The load-bearing claims:
//   - an empty overlay renders BYTE-IDENTICAL (deep-equal, same frozen shape,
//     and for the pure merge, the SAME reference) to surfaceContract(id);
//   - an unknown slot in the overlay is ignored, with exactly one warning;
//   - the session fetch is bounded: one getSurfaceConfig call no matter how
//     many components mount the hook, and a rejection folds to defaults.
import { act, renderHook, waitFor } from '@testing-library/react'
import {
  afterEach, beforeEach, describe, expect, it, vi,
} from 'vitest'

vi.mock('../api.js', () => ({ getSurfaceConfig: vi.fn() }))

import { getSurfaceConfig } from '../api.js'
import { surfaceContract } from './productSurfaces.js'
import {
  _resetSurfaceConfigOverlayForTests,
  mergeSurfaceContract,
  refreshSurfaceConfigOverlay,
  touchedSurfaceConfigSlots,
  useSurfaceContract,
} from './useSurfaceContract.js'

beforeEach(() => {
  vi.clearAllMocks()
  _resetSurfaceConfigOverlayForTests()
})
afterEach(() => {
  _resetSurfaceConfigOverlayForTests()
})

describe('mergeSurfaceContract — pure merge, no hook', () => {
  it('is byte-identical (same reference) to surfaceContract(id) for an empty or missing overlay', () => {
    expect(mergeSurfaceContract('cad', {})).toBe(surfaceContract('cad'))
    expect(mergeSurfaceContract('cad', undefined)).toBe(surfaceContract('cad'))
    expect(mergeSurfaceContract('cad', null)).toBe(surfaceContract('cad'))
    expect(mergeSurfaceContract('cad', { cad: {} })).toBe(surfaceContract('cad'))
    expect(mergeSurfaceContract('sheets', {})).toBe(surfaceContract('sheets'))
  })

  it('ignores an unknown slot, with exactly one warning per surface id', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const base = surfaceContract('cad')
    const merged = mergeSurfaceContract('cad', { cad: { bogusSlot: 'nope' } })
    // The unknown key never lands on the merged object, so it deep-equals the
    // default even though (unlike the empty-overlay case) it is a new object.
    expect(merged).toEqual(base)
    expect(merged.bogusSlot).toBeUndefined()
    expect(warn).toHaveBeenCalledTimes(1)
    expect(warn.mock.calls[0][0]).toMatch(/unknown slot.*bogusSlot/)
    // A second overlay carrying the SAME unknown slot on the SAME id does not
    // warn again (bounded: one console line, not one per merge call).
    mergeSurfaceContract('cad', { cad: { bogusSlot: 'still-nope' } })
    expect(warn).toHaveBeenCalledTimes(1)
    warn.mockRestore()
  })

  it('deep-merges one level onto a declared slot and deep-freezes the result', () => {
    const merged = mergeSurfaceContract('sheets', { sheets: { chrome: { tab: true } } })
    expect(merged.chrome.tab).toBe(true)
    // Every OTHER field on the chrome slot survives the one-level merge.
    expect(merged.chrome.stageBranch).toBe(surfaceContract('sheets').chrome.stageBranch)
    expect(Object.isFrozen(merged)).toBe(true)
    expect(Object.isFrozen(merged.chrome)).toBe(true)
  })

  it('an unrelated surface id is untouched by another id\'s overlay', () => {
    const overlay = { sheets: { chrome: { tab: true } } }
    expect(mergeSurfaceContract('cad', overlay)).toBe(surfaceContract('cad'))
  })
})

describe('touchedSurfaceConfigSlots', () => {
  it('is empty for no overlay, and names only the slots actually set', () => {
    expect(touchedSurfaceConfigSlots('sheets', {})).toEqual([])
    expect(touchedSurfaceConfigSlots('sheets', { sheets: { chrome: { tab: true }, versions: {} } }))
      .toEqual(['chrome', 'versions'])
  })
})

describe('useSurfaceContract — the session fetch', () => {
  it('refresh re-fetches and merges the committed overlay for existing subscribers', async () => {
    getSurfaceConfig.mockResolvedValueOnce({ surfaces: {} })
      .mockResolvedValueOnce({ surfaces: { cad: { authoring: false } } })
    const { result } = renderHook(() => useSurfaceContract('cad'))
    await act(async () => { await Promise.resolve() })
    expect(result.current).toBe(surfaceContract('cad'))
    await act(async () => { await refreshSurfaceConfigOverlay() })
    expect(getSurfaceConfig).toHaveBeenCalledTimes(2)
    expect(result.current.authoring).toBe(false)
    expect(result.current.chrome).toBe(surfaceContract('cad').chrome)
  })

  it('a pre-commit fetch cannot overwrite the refreshed overlay when it settles late', async () => {
    let finishOld
    getSurfaceConfig.mockReturnValueOnce(new Promise((resolve) => { finishOld = resolve }))
      .mockResolvedValueOnce({ surfaces: { cad: { authoring: false } } })
    const { result } = renderHook(() => useSurfaceContract('cad'))
    await act(async () => { await refreshSurfaceConfigOverlay() })
    await act(async () => { finishOld({ surfaces: { cad: { authoring: true } } }) })
    expect(result.current.authoring).toBe(false)
  })

  it('fetches once per session no matter how many hooks mount, and merges the result', async () => {
    getSurfaceConfig.mockResolvedValue({
      surfaces: { sheets: { chrome: { tab: true } } },
      source: { sha256: 'abcdef0123456789', authored_at: '2026-09-04T00:00:00Z' },
    })
    const a = renderHook(() => useSurfaceContract('sheets'))
    const b = renderHook(() => useSurfaceContract('cad'))
    // Before the fetch settles, both read the frozen defaults (byte-identical).
    expect(a.result.current).toBe(surfaceContract('sheets'))
    expect(b.result.current).toBe(surfaceContract('cad'))
    await waitFor(() => expect(a.result.current.chrome.tab).toBe(true))
    expect(b.result.current).toBe(surfaceContract('cad')) // untouched by sheets' overlay
    expect(getSurfaceConfig).toHaveBeenCalledTimes(1)
    // A third mount after the fetch already settled reuses the same result —
    // still exactly one network call for the whole session.
    const c = renderHook(() => useSurfaceContract('sheets'))
    expect(c.result.current.chrome.tab).toBe(true)
    expect(getSurfaceConfig).toHaveBeenCalledTimes(1)
  })

  it('a failed fetch folds to the frozen defaults, never throwing into render', async () => {
    getSurfaceConfig.mockRejectedValue(new Error('GET /api/surface-config -> 500'))
    const { result } = renderHook(() => useSurfaceContract('cad'))
    expect(result.current).toBe(surfaceContract('cad'))
    await act(async () => { await Promise.resolve() })
    expect(result.current).toBe(surfaceContract('cad'))
  })
})
