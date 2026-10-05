// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

const DRAWING_KEY = 'leaf.cat.workbench.id.v1'
let originalAddress, identity
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
