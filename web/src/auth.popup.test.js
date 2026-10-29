// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

let popup, sdk, createClient, open, configFetch
beforeEach(() => {
  vi.resetModules()
  for (const key of ['DOMAIN', 'CLIENT_ID', 'AUDIENCE']) vi.stubEnv(`VITE_AUTH0_${key}`, 'configured')
  vi.stubEnv('VITE_API_BASE', '')
  configFetch = vi.fn().mockRejectedValue(new Error('Config unavailable'))
  vi.stubGlobal('fetch', configFetch)
  vi.doMock('./telemetry.js', () => ({ track: vi.fn(), flushNow: vi.fn() }))
  localStorage.removeItem('leaf.jwt')
  popup = { closed: false, close: vi.fn() }
  sdk = {
    loginWithPopup: vi.fn().mockResolvedValue(undefined),
    getTokenSilently: vi.fn().mockResolvedValue('popup-test-token'),
    loginWithRedirect: vi.fn().mockResolvedValue(undefined),
    handleRedirectCallback: vi.fn().mockResolvedValue({ appState: { returnTo: '/' } }),
  }
  createClient = vi.fn().mockResolvedValue(sdk)
  vi.doMock('@auth0/auth0-spa-js', () => ({ createAuth0Client: createClient }))
  open = vi.spyOn(window, 'open').mockReturnValue(popup)
})
afterEach(() => {
  localStorage.removeItem('leaf.jwt')
  vi.restoreAllMocks()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  vi.doUnmock('@auth0/auth0-spa-js')
  vi.doUnmock('./telemetry.js')
  window.history.replaceState({}, '', '/')
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

const runtimeAudience = 'https://api-staging.leafdesign.ai'

function expectClientAudience(audience) {
  expect(createClient).toHaveBeenCalledWith(expect.objectContaining({
    authorizationParams: { audience, redirect_uri: window.location.origin },
  }))
}

it('uses the runtime audience for client creation, popup, redirect and both token exchanges', async () => {
  vi.stubEnv('VITE_API_BASE', 'https://server.example')
  configFetch.mockResolvedValue({ ok: true, json: async () => ({ audience: runtimeAudience }) })
  const auth = await import('./auth.js')
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: true })
  expectClientAudience(runtimeAudience)
  expect(sdk.loginWithPopup).toHaveBeenCalledWith({
    authorizationParams: { audience: runtimeAudience, redirect_uri: window.location.origin },
  }, { popup, timeoutInSeconds: 120 })
  await auth.login()
  expect(sdk.loginWithRedirect).toHaveBeenCalledWith(expect.objectContaining({
    authorizationParams: { audience: runtimeAudience },
  }))
  window.history.replaceState({}, '', '/?code=test&state=test')
  expect(await auth.handleRedirectCallback()).toBe(true)
  expect(sdk.getTokenSilently).toHaveBeenCalledTimes(2)
  for (const [options] of sdk.getTokenSilently.mock.calls) {
    expect(options).toEqual({ authorizationParams: { audience: runtimeAudience } })
  }
  expect(configFetch).toHaveBeenCalledExactlyOnceWith('https://server.example/api/site/auth-config', {
    signal: expect.any(AbortSignal),
  })
})

it('shares one config fetch and SDK creation between concurrent client callers', async () => {
  let release
  configFetch.mockReturnValue(new Promise((resolve) => { release = resolve }))
  const auth = await import('./auth.js')
  auth.preloadAuthClient()
  const first = auth.loginWithPopupFromClick()
  const second = auth.loginWithPopupFromClick()
  expect(configFetch).toHaveBeenCalledOnce()
  expect(createClient).not.toHaveBeenCalled()
  release({ ok: true, json: async () => ({ audience: runtimeAudience }) })
  expect(await Promise.all([first, second])).toEqual([{ ok: true }, { ok: true }])
  expect(createClient).toHaveBeenCalledOnce()
  expectClientAudience(runtimeAudience)
})

it('opens the popup synchronously while runtime config is still pending', async () => {
  let release
  configFetch.mockReturnValue(new Promise((resolve) => { release = resolve }))
  const auth = await import('./auth.js')
  const pending = auth.loginWithPopupFromClick()
  expect(open).toHaveBeenCalledExactlyOnceWith('', 'leaf-auth', 'popup,width=480,height=680')
  expect(createClient).not.toHaveBeenCalled()
  release({ ok: true, json: async () => ({ audience: runtimeAudience }) })
  expect(await pending).toEqual({ ok: true })
})

it('falls back to the build audience on a non-2xx response without reading its body', async () => {
  const json = vi.fn().mockResolvedValue({ audience: runtimeAudience })
  configFetch.mockResolvedValue({ ok: false, status: 503, json })
  const auth = await import('./auth.js')
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: true })
  expectClientAudience('configured')
  expect(json).not.toHaveBeenCalled()
})

it('freezes the build fallback after a network error', async () => {
  const auth = await import('./auth.js')
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: true })
  configFetch.mockResolvedValue({ ok: true, json: async () => ({ audience: runtimeAudience }) })
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: true })
  expectClientAudience('configured')
  expect(configFetch).toHaveBeenCalledOnce()
  expect(sdk.getTokenSilently).toHaveBeenLastCalledWith({ authorizationParams: { audience: 'configured' } })
})

it.each([null, {}, { audience: '' }, { audience: 123 }, { audience: 'http://api.example' },
  { audience: 'not-an-audience' }])('falls back for malformed config %j', async (body) => {
  configFetch.mockResolvedValue({ ok: true, json: async () => body })
  const auth = await import('./auth.js')
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: true })
  expectClientAudience('configured')
})

it('falls back when the response body is invalid JSON', async () => {
  configFetch.mockResolvedValue({ ok: true, json: async () => { throw new SyntaxError('Invalid JSON') } })
  const auth = await import('./auth.js')
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: true })
  expectClientAudience('configured')
})

it.each(['fetch', 'body'])('bounds %s to 1000 ms and ignores its late response', async (phase) => {
  vi.useFakeTimers()
  let release
  const delayed = new Promise((resolve) => { release = resolve })
  configFetch.mockReturnValue(phase === 'fetch'
    ? delayed : Promise.resolve({ ok: true, json: () => delayed }))
  const auth = await import('./auth.js')
  const pending = auth.loginWithPopupFromClick()
  const signal = configFetch.mock.calls[0][1].signal
  await vi.advanceTimersByTimeAsync(999)
  expect(signal.aborted).toBe(false)
  expect(createClient).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(1)
  expect(await pending).toEqual({ ok: true })
  expect(signal.aborted).toBe(true)
  expectClientAudience('configured')
  release(phase === 'fetch'
    ? { ok: true, json: async () => ({ audience: runtimeAudience }) }
    : { audience: runtimeAudience })
  await Promise.resolve()
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: true })
  expect(configFetch).toHaveBeenCalledOnce()
  expect(createClient).toHaveBeenCalledOnce()
  expect(sdk.getTokenSilently).toHaveBeenLastCalledWith({ authorizationParams: { audience: 'configured' } })
})

it('retries SDK creation after rejection while retaining the resolved audience', async () => {
  configFetch.mockResolvedValue({ ok: true, json: async () => ({ audience: runtimeAudience }) })
  createClient.mockRejectedValueOnce(new Error('SDK unavailable'))
  const auth = await import('./auth.js')
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: false, reason: 'failed' })
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: true })
  expect(createClient).toHaveBeenCalledTimes(2)
  for (const [options] of createClient.mock.calls) {
    expect(options.authorizationParams.audience).toBe(runtimeAudience)
  }
  expect(configFetch).toHaveBeenCalledOnce()
})

it('keeps the synchronous build gate even when runtime config could provide an audience', async () => {
  vi.stubEnv('VITE_AUTH0_AUDIENCE', '')
  configFetch.mockResolvedValue({ ok: true, json: async () => ({ audience: runtimeAudience }) })
  const auth = await import('./auth.js')
  expect(auth.authConfigured).toBe(false)
  auth.preloadAuthClient()
  expect(await auth.loginWithPopupFromClick()).toEqual({ ok: false, reason: 'unconfigured' })
  expect(open).not.toHaveBeenCalled()
  expect(configFetch).not.toHaveBeenCalled()
})
