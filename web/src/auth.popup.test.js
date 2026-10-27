// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

let popup, sdk, createClient, open
beforeEach(() => {
  vi.resetModules()
  for (const key of ['DOMAIN', 'CLIENT_ID', 'AUDIENCE']) vi.stubEnv(`VITE_AUTH0_${key}`, 'configured')
  localStorage.removeItem('leaf.jwt')
  popup = { closed: false, close: vi.fn() }
  sdk = {
    loginWithPopup: vi.fn().mockResolvedValue(undefined),
    getTokenSilently: vi.fn().mockResolvedValue('popup-test-token'),
  }
  createClient = vi.fn().mockResolvedValue(sdk)
  vi.doMock('@auth0/auth0-spa-js', () => ({ createAuth0Client: createClient }))
  open = vi.spyOn(window, 'open').mockReturnValue(popup)
})
afterEach(() => {
  localStorage.removeItem('leaf.jwt')
  vi.restoreAllMocks()
  vi.unstubAllEnvs()
  vi.doUnmock('@auth0/auth0-spa-js')
})

it('opens synchronously before loading the SDK, stores the token and notifies listeners', async () => {
  const auth = await import('./auth.js')
  const stored = vi.fn()
  const off = auth.subscribeTokenStored(stored)
  const pending = auth.loginWithPopupFromClick()
  expect(open).toHaveBeenCalledExactlyOnceWith('', 'leaf-auth', 'popup,width=480,height=680')
  expect(createClient).not.toHaveBeenCalled()
  expect(sdk.loginWithPopup).not.toHaveBeenCalled()
  expect(await pending).toEqual({ ok: true })
  expect(sdk.loginWithPopup).toHaveBeenCalledExactlyOnceWith({
    authorizationParams: { audience: 'configured', redirect_uri: window.location.origin },
  }, { popup, timeoutInSeconds: 120 })
  expect(sdk.getTokenSilently).toHaveBeenCalledExactlyOnceWith({ authorizationParams: { audience: 'configured' } })
  expect(localStorage.getItem('leaf.jwt')).toBe('popup-test-token')
  expect(stored).toHaveBeenCalledExactlyOnceWith('popup-test-token')
  off()
})

it('returns blocked immediately without starting the SDK when the popup is null', async () => {
  open.mockReturnValue(null)
  const auth = await import('./auth.js')
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: false, reason: 'blocked' })
  expect(createClient).not.toHaveBeenCalled()
})

it.each([
  ['popup_closed', 'cancelled'],
  ['cancelled', 'cancelled'],
  ['timeout', 'failed'],
  ['access_denied', 'failed'],
])('handles SDK %s and closes the popup', async (error, reason) => {
  sdk.loginWithPopup.mockRejectedValue({ error })
  const auth = await import('./auth.js')
  const stored = vi.fn()
  const off = auth.subscribeTokenStored(stored)
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: false, reason })
  expect(popup.close).toHaveBeenCalledOnce()
  expect(localStorage.getItem('leaf.jwt')).toBeNull()
  expect(stored).not.toHaveBeenCalled()
  off()
})

it('does not close a popup that is already closed', async () => {
  popup.closed = true
  sdk.loginWithPopup.mockRejectedValue({ error: 'popup_closed' })
  const auth = await import('./auth.js')
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: false, reason: 'cancelled' })
  expect(popup.close).not.toHaveBeenCalled()
})

it('handles client loading and token exchange failures without throwing', async () => {
  createClient.mockRejectedValueOnce(new Error('SDK unavailable'))
  const auth = await import('./auth.js')
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: false, reason: 'failed' })
  expect(popup.close).toHaveBeenCalledOnce()
  sdk.getTokenSilently.mockRejectedValueOnce(new Error('Exchange failed'))
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: false, reason: 'failed' })
  expect(popup.close).toHaveBeenCalledTimes(2)
})

it('preloads the client without opening a popup and tolerates failure', async () => {
  createClient.mockRejectedValueOnce(new Error('SDK unavailable'))
  const auth = await import('./auth.js')
  expect(auth.preloadAuthClient()).toBeUndefined()
  await vi.waitFor(() => expect(createClient).toHaveBeenCalledOnce())
  expect(open).not.toHaveBeenCalled()
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: true })
})

it('does not open a window or load the SDK when unconfigured', async () => {
  vi.stubEnv('VITE_AUTH0_DOMAIN', '')
  const auth = await import('./auth.js')
  auth.preloadAuthClient()
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: false, reason: 'unconfigured' })
  expect(open).not.toHaveBeenCalled()
  expect(createClient).not.toHaveBeenCalled()
})
