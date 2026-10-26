// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

const DRAWING_KEY = 'leaf.cat.workbench.id.v1'
let originalAddress, identity

async function scopedAuth(sdk, createClient = async () => sdk) {
  vi.resetModules()
  for (const key of ['DOMAIN', 'CLIENT_ID', 'AUDIENCE']) vi.stubEnv(`VITE_AUTH0_${key}`, 'configured')
  vi.doMock('@auth0/auth0-spa-js', () => ({ createAuth0Client: createClient }))
  originalAddress = window.location.pathname + window.location.search + window.location.hash
  window.history.replaceState(null, '', '/app?surface=cad&code=x&state=y#drawing')
  return import('./auth.js')
}

it('does not start an Auth0 redirect after the SDK finishes loading in a retired scope', async () => {
  let complete
  const sdk = { loginWithRedirect: vi.fn() }
  const createClient = vi.fn(() => new Promise(resolve => { complete = resolve }))
  const auth = await scopedAuth(sdk, createClient)
  let current = true
  const pending = auth.login({ isCurrent: () => current })
  await vi.waitFor(() => expect(createClient).toHaveBeenCalledOnce())
  current = false
  complete(sdk)
  await pending
  expect(sdk.loginWithRedirect).not.toHaveBeenCalled()
})

it('does not navigate after Auth0 finishes preparing an obsolete redirect', async () => {
  let complete
  const sdk = { loginWithRedirect: vi.fn(options => new Promise(resolve => {
    complete = () => { options.openUrl('https://example.com/authorize'); resolve() }
  })) }
  const auth = await scopedAuth(sdk)
  let current = true
  const assign = vi.fn()
  const browserWindow = window
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) { return key === 'location' ? { pathname: '/app', search: '', hash: '', assign } : Reflect.get(target, key, target) },
  }))
  const pending = auth.login({ isCurrent: () => current })
  await vi.waitFor(() => expect(sdk.loginWithRedirect).toHaveBeenCalledOnce())
  current = false
  complete()
  await pending
  expect(assign).not.toHaveBeenCalled()
})

it.each(['exchange', 'token'])('does not store a token or restore old history after scope changes during %s', async step => {
  let complete
  const deferred = new Promise(resolve => { complete = resolve })
  const sdk = {
    handleRedirectCallback: vi.fn(() => step === 'exchange' ? deferred : Promise.resolve({ appState: { returnTo: '/old' } })),
    getTokenSilently: vi.fn(() => deferred),
  }
  const auth = await scopedAuth(sdk)
  const stored = vi.fn()
  const off = auth.subscribeTokenStored(stored)
  let current = true
  const pending = auth.handleRedirectCallback({ isCurrent: () => current })
  await vi.waitFor(() => expect(step === 'exchange' ? sdk.handleRedirectCallback : sdk.getTokenSilently).toHaveBeenCalledOnce())
  current = false
  window.history.replaceState(null, '', '/app?surface=solar')
  const replace = vi.spyOn(window.history, 'replaceState')
  complete(step === 'exchange' ? { appState: { returnTo: '/old' } } : 'old-token')
  expect(await pending).toBe(false)
  expect(replace).not.toHaveBeenCalled()
  expect(localStorage.getItem('leaf.jwt')).toBeNull()
  expect(stored).not.toHaveBeenCalled()
  expect(window.location.search).toBe('?surface=solar')
  off()
})

it('does not clean history on an SDK rejection from a retired callback', async () => {
  let reject
  const sdk = { handleRedirectCallback: vi.fn(() => new Promise((resolve, fail) => { reject = fail })) }
  const auth = await scopedAuth(sdk)
  let current = true
  const pending = auth.handleRedirectCallback({ isCurrent: () => current })
  await vi.waitFor(() => expect(sdk.handleRedirectCallback).toHaveBeenCalledOnce())
  current = false
  const replace = vi.spyOn(window.history, 'replaceState')
  reject(new Error('late callback failure'))
  expect(await pending).toBe(false)
  expect(replace).not.toHaveBeenCalled()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  vi.resetModules()
  vi.restoreAllMocks()
  vi.doUnmock('@auth0/auth0-spa-js')
  localStorage.removeItem('leaf.jwt')
  sessionStorage.removeItem(DRAWING_KEY)
  if (originalAddress !== undefined) window.history.replaceState(null, '', originalAddress)
  originalAddress = undefined
})
async function mountLogout(sdkEnabled) {
  vi.resetModules()
  vi.stubEnv('VITE_AUTH0_DOMAIN', sdkEnabled ? 'configured' : '')
  vi.stubEnv('VITE_AUTH0_CLIENT_ID', sdkEnabled ? 'configured' : '')
  vi.stubEnv('VITE_AUTH0_AUDIENCE', sdkEnabled ? 'configured' : '')
  const sdk = { logout: vi.fn(async () => {}) }
  const createClient = vi.fn(async () => sdk)
  vi.doMock('@auth0/auth0-spa-js', () => ({ createAuth0Client: createClient }))
  const auth = await import('./auth.js')
  const { DrawingIdentityProvider, useDrawingIdentity } = await import('./drawing/DrawingIdentityProvider.jsx')
  const { seedDrawingIdentity } = await import('./drawing/drawingIdentity.js')
  const realWindow = window
  originalAddress = realWindow.location.pathname + realWindow.location.search + realWindow.location.hash
  realWindow.history.replaceState(null, '', '/app?drawing=u-upload')
  localStorage.setItem('leaf.jwt', 'principal-a')
  sessionStorage.setItem(DRAWING_KEY, 'u-upload')
  const reload = vi.fn()
  const location = {
    get href() { return realWindow.location.href },
    get origin() { return realWindow.location.origin },
    get search() { return realWindow.location.search },
    reload,
  }
  vi.stubGlobal('window', new Proxy(realWindow, {
    get(target, key) { return key === 'location' ? location : Reflect.get(target, key, target) },
  }))
  function Probe() { identity = useDrawingIdentity(); return null }
  const offThrow = auth.subscribeBeforeLogout(() => { throw new Error('Observer refused') })
  render(<DrawingIdentityProvider mode="console" scene="app" search="?drawing=u-upload"
    publicDemo={false} liveDemo={false}><Probe /></DrawingIdentityProvider>)
  const oldCurrent = identity.isScopeCurrent
  const oldUpload = identity.setFromUpload
  const oldQuery = identity.setFromQuery
  const assertCleared = () => {
    expect(oldCurrent()).toBe(false)
    expect(oldUpload({ drawing_id: 'v-upload', tenant_kind: 'account' })).toBeNull()
    expect(oldQuery()).toBeNull()
    expect(realWindow.location.pathname + realWindow.location.search).toBe('/app')
    expect(sessionStorage.getItem(DRAWING_KEY)).toBeNull()
    expect(seedDrawingIdentity({ mode: 'console', search: realWindow.location.search }))
      .toMatchObject({ drawingId: 'demo', source: 'rooftop_demo' })
  }
  return { auth, sdk, createClient, reload, assertCleared, offThrow, origin: realWindow.location.origin }
}

it('URL307B-05 explicit logout clears before fallback reload', async () => {
  const h = await mountLogout(false)
  const order = []
  const off = h.auth.subscribeBeforeLogout(() => {
    expect(localStorage.getItem('leaf.jwt')).toBe('principal-a')
    h.assertCleared()
    order.push('before')
  })
  h.reload.mockImplementation(() => {
    h.assertCleared()
    expect(localStorage.getItem('leaf.jwt')).toBeNull()
    order.push('reload')
  })
  let run
  act(() => {
    run = h.auth.logout()
    h.assertCleared()
    expect(h.reload).not.toHaveBeenCalled()
  })
  await act(async () => { await run })
  expect(identity).toMatchObject({ drawingId: null, source: null, origin: 'reset' })
  act(() => { expect(identity.setFromQuery().origin).toBe('reset') })
  expect(order).toEqual(['before', 'reload'])
  expect(h.reload).toHaveBeenCalledOnce()
  expect(h.createClient).not.toHaveBeenCalled()
  off(); h.offThrow()
})

it('URL307B-06 explicit logout clears before SDK navigation', async () => {
  const h = await mountLogout(true)
  // The session controller can delete the token before calling real logout.
  localStorage.removeItem('leaf.jwt')
  const detached = vi.fn()
  h.auth.subscribeBeforeLogout(detached)()
  h.sdk.logout.mockImplementation(async (options) => {
    h.assertCleared()
    expect(options).toEqual({ logoutParams: { returnTo: h.origin } })
  })
  let run
  act(() => {
    run = h.auth.logout()
    h.assertCleared()
    expect(h.sdk.logout).not.toHaveBeenCalled()
  })
  await act(async () => { await run })
  expect(identity).toMatchObject({ drawingId: null, source: null, origin: 'reset' })
  act(() => { expect(identity.setFromQuery().origin).toBe('reset') })
  expect(h.createClient).toHaveBeenCalledOnce()
  expect(h.sdk.logout).toHaveBeenCalledOnce()
  expect(h.reload).not.toHaveBeenCalled()
  expect(detached).not.toHaveBeenCalled()
  h.offThrow()
})

it.each([false, true])('S21 logout clears all command-bar draft scopes before navigation (SDK=%s)', async (sdkEnabled) => {
  const h = await mountLogout(sdkEnabled)
  const { composerDraftKey, createComposerDraft } = await import('./lib/composerDraft.js')
  const keys = [
    composerDraftKey('guest', '/try'),
    composerDraftKey('account:a', '/app'),
    composerDraftKey('account:b', '/try'),
  ]
  for (const key of keys) localStorage.setItem(key, 'draw a 20 ft fence')
  localStorage.setItem('leaf.inflightAuthor.v1', 'author pointer')
  localStorage.setItem('leaf.unrelated', 'keep')
  const pending = createComposerDraft({ storage: localStorage, accountScope: 'account:a', route: '/app' })
  pending.update('a pending edit must not return after logout')
  const assertDraftsCleared = () => {
    for (const key of keys) expect(localStorage.getItem(key)).toBeNull()
    expect(localStorage.getItem('leaf.inflightAuthor.v1')).toBeNull()
    expect(localStorage.getItem('leaf.unrelated')).toBe('keep')
    pending.flush()
    for (const key of keys) expect(localStorage.getItem(key)).toBeNull()
  }
  h.reload.mockImplementation(assertDraftsCleared)
  h.sdk.logout.mockImplementation(async () => assertDraftsCleared())
  try {
    await act(async () => { await h.auth.logout() })
    assertDraftsCleared()
    expect(sdkEnabled ? h.sdk.logout : h.reload).toHaveBeenCalledOnce()
  } finally {
    pending.dispose({ save: false })
    for (const key of keys) localStorage.removeItem(key)
    localStorage.removeItem('leaf.inflightAuthor.v1')
    localStorage.removeItem('leaf.unrelated')
    h.offThrow()
  }
})
