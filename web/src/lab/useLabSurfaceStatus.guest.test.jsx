// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

vi.mock('../auth.js', async () => {
  const beforeLogoutListeners = new Set()
  const tokenStoredListeners = new Set()
  return {
    ...await vi.importActual('../auth.js'),
    authConfigured: true,
    isSignedIn: vi.fn(),
    isAuthRedirectCallback: vi.fn(),
    subscribeBeforeLogout: (listener) => {
      beforeLogoutListeners.add(listener)
      return () => beforeLogoutListeners.delete(listener)
    },
    subscribeTokenStored: (listener) => {
      tokenStoredListeners.add(listener)
      return () => tokenStoredListeners.delete(listener)
    },
  }
})
vi.mock('./labRoute.js', async () => ({
  ...await vi.importActual('./labRoute.js'),
  fetchLabStatus: vi.fn().mockResolvedValue({ state: 'on', posture: null }),
}))

import { isAuthRedirectCallback, isSignedIn } from '../auth.js'
import { fetchLabStatus } from './labRoute.js'
import { _resetLabSurfaceStatusForTests, useLabSurfaceStatus } from './useLabSurfaceStatus.js'

beforeEach(() => {
  _resetLabSurfaceStatusForTests()
  vi.clearAllMocks()
  fetchLabStatus.mockResolvedValue({ state: 'on', posture: null })
  isAuthRedirectCallback.mockReturnValue(false)
  isSignedIn.mockReturnValue(false)
})
afterEach(() => {
  cleanup()
  _resetLabSurfaceStatusForTests()
})

it('a known guest makes no lab status read', async () => {
  isSignedIn.mockReturnValue(false)
  const { result } = renderHook(() => useLabSurfaceStatus(false))
  await act(async () => {})
  expect(fetchLabStatus).not.toHaveBeenCalled()
  expect(result.current).toEqual({ state: 'signed-out', posture: null })
})

it('a signed-in session still probes', async () => {
  isSignedIn.mockReturnValue(true)
  renderHook(() => useLabSurfaceStatus(false))
  await act(async () => {})
  expect(fetchLabStatus).toHaveBeenCalledTimes(1)
})
