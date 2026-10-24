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
vi.mock('../api.js', async () => ({
  ...await vi.importActual('../api.js'),
  getSurfaceConfig: vi.fn().mockResolvedValue({ surfaces: {} }),
}))

import { isAuthRedirectCallback, isSignedIn } from '../auth.js'
import { getSurfaceConfig } from '../api.js'
import { _resetSurfaceConfigOverlayForTests, useSurfaceConfigOverlay } from './useSurfaceContract.js'

beforeEach(() => {
  vi.clearAllMocks()
  getSurfaceConfig.mockResolvedValue({ surfaces: {} })
  _resetSurfaceConfigOverlayForTests()
  isAuthRedirectCallback.mockReturnValue(false)
  isSignedIn.mockReturnValue(false)
})
afterEach(() => {
  cleanup()
  _resetSurfaceConfigOverlayForTests()
})

it('a known guest makes no surface-config read', async () => {
  isSignedIn.mockReturnValue(false)
  const { result } = renderHook(() => useSurfaceConfigOverlay(false))
  await act(async () => {})
  expect(getSurfaceConfig.mock.calls.every(([skip]) => skip === true)).toBe(true)
  expect(result.current).toEqual({})
})

it('a signed-in session still reads', async () => {
  isSignedIn.mockReturnValue(true)
  renderHook(() => useSurfaceConfigOverlay(false))
  await act(async () => {})
  expect(getSurfaceConfig).toHaveBeenCalledWith(false)
})
