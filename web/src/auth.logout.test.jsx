// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

const DRAWING_KEY = 'leaf.cat.workbench.id.v1'
let originalAddress, identity

it('bootstraps joint proof without borrowing a human token and logs out locally before failed revocation', async () => {
  vi.resetModules()
  const auth = await import('./auth.js')
  const { serviceSession, serviceState, authorityStorage } = await import('./lib/serviceAuthSession.js')
  const { authHeaders, config, getSessionIdentity } = await import('./api.js')
  const service = serviceSession()
  const selector = '11111111-1111-4111-8111-111111111111'
  const realWindow = window
  originalAddress = realWindow.location.pathname + realWindow.location.search + realWindow.location.hash
  realWindow.history.replaceState({}, '', `/?service_session=${selector}&demo=1&keep=yes#target`)
  localStorage.setItem('leaf.jwt', 'human-token')
  localStorage.setItem('leaf.org_id', 'human-org')
  let completeProof, rejectRevoke
  const fetcher = vi.fn((url, init) => {
    if (url.endsWith('/api/auth/agent/session')) return new Promise(resolve => { completeProof = resolve })
    if (url.endsWith('/api/auth/agent/logout')) {
      expect(auth.isSignedIn()).toBe(false)
      expect(serviceState().identity).toBeNull()
      expect(init.headers.Authorization).toBe(`LeafServiceSession ${selector}`)
      return new Promise((_, reject) => { rejectRevoke = reject })
    }
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const before = vi.fn()
  const off = auth.subscribeBeforeLogout(before)
  const reload = vi.fn()
  try {
    const pending = auth.bootstrapServiceAuth()
    expect(auth.isSignedIn()).toBe(false)
    expect(authHeaders()).toEqual({})
    expect(config.mockDefault).toBe(false)
    expect(auth.authConfigured).toBe(true)
    await expect(getSessionIdentity(false)).rejects.toMatchObject({ status: 401 })
    expect(fetcher).toHaveBeenCalledOnce()
    expect(realWindow.location.search).toBe('?keep=yes')
    expect(realWindow.location.hash).toBe('#target')
    expect(fetcher.mock.calls[0][1]).toMatchObject({ credentials: 'same-origin', headers: { Authorization: `LeafServiceSession ${selector}` } })
    completeProof({ ok: true, json: async () => ({ authenticated: true, binding_id: selector,
      expires_at: new Date(Date.now() + 60000).toISOString(), identity: { subject: 'machine', sub: 'machine',
        iss: 'issuer', org_id: 'service-org', tenant_id: 'tenant', principal_type: 'service_admin' } }) })
    expect(await pending).toBe(true)
    expect(auth.isSignedIn()).toBe(true)
    expect(authHeaders()).toMatchObject({ Authorization: `LeafServiceSession ${selector}`, 'X-Tenant-Id': 'tenant' })
    const scoped = authorityStorage()
    scoped.setItem('leaf.inflightAuthor.v1', 'service pointer')
    vi.stubGlobal('window', new Proxy(realWindow, {
      get(target, key) { return key === 'location' ? { origin: realWindow.location.origin, reload } : Reflect.get(target, key, target) },
    }))
    const logout = auth.logout()
    expect(before).toHaveBeenCalledOnce()
    expect(serviceState().identity).toBeNull()
    expect(scoped.getItem('leaf.inflightAuthor.v1')).toBeNull()
    expect(localStorage.getItem('leaf.jwt')).toBe('human-token')
    expect(localStorage.getItem('leaf.org_id')).toBe('human-org')
    rejectRevoke(new Error('offline'))
    await logout
    expect(reload).toHaveBeenCalledOnce()
    expect(serviceState().mode).toBe(true)
    expect(auth.isSignedIn()).toBe(false)
  } finally { off(); service.human(); localStorage.removeItem('leaf.org_id') }
})

it('does not probe service auth on an ordinary human boot', async () => {
  vi.resetModules()
  const auth = await import('./auth.js')
  const { serviceSession } = await import('./lib/serviceAuthSession.js')
  serviceSession().human()
  originalAddress = window.location.pathname + window.location.search + window.location.hash
  window.history.replaceState({}, '', '/app?keep=human')
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  expect(await auth.bootstrapServiceAuth()).toBe(false)
  expect(fetcher).not.toHaveBeenCalled()
  expect(window.location.search).toBe('?keep=human')
})

it('defers SiteRoot module evaluation and proofBridge installation until joint proof resolves', async () => {
  vi.resetModules()
  const order = []
  vi.doMock('./site/SiteRoot.jsx', () => {
    order.push('evaluate')
    return { default: () => <div data-testid="verified-site-root" /> }
  })
  vi.doMock('./proofBridge.js', () => ({ installSurfaceConfigProofBridge: () => { order.push('bridge') } }))
  const selector = '11111111-1111-4111-8111-111111111111'
  originalAddress = window.location.pathname + window.location.search + window.location.hash
  window.history.replaceState({}, '', `/?service_session=${selector}`)
  let complete
  const fetcher = vi.fn(() => new Promise(resolve => { complete = resolve }))
  vi.stubGlobal('fetch', fetcher)
  const { serviceSession } = await import('./lib/serviceAuthSession.js')
  try {
    const { default: StudioRoot } = await import('./main.jsx')
    expect(fetcher).toHaveBeenCalledOnce()
    expect(order).toEqual([])
    const view = render(<StudioRoot />)
    expect(view.queryByTestId('verified-site-root')).toBeNull()
    await act(async () => {
      complete({ ok: true, json: async () => ({ authenticated: true, binding_id: selector,
        expires_at: new Date(Date.now() + 60000).toISOString(), identity: { subject: 'machine', sub: 'machine',
          iss: 'issuer', org_id: null, tenant_id: 'tenant', principal_type: 'service_admin' } }) })
    })
    await view.findByTestId('verified-site-root')
    expect(order).toEqual(['evaluate', 'bridge'])
  } finally {
    cleanup(); serviceSession().human()
    vi.doUnmock('./site/SiteRoot.jsx'); vi.doUnmock('./proofBridge.js')
  }
})

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
