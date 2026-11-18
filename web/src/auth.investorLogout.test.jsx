// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { INVESTOR_ISSUER } from './investorDemo.js'
import { composerDraftKey } from './lib/composerDraft.js'
import { createSessionController } from './controllers/session/createSessionController.js'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  vi.doUnmock('@auth0/auth0-spa-js')
  vi.resetModules()
  localStorage.clear()
  sessionStorage.clear()
})

const cases = [[true, false, true], [true, true, true], [false, true, true], [true, false, false]]
it.each(cases)('clears drafts and ends the right session (investor=%s, controller=%s, hasToken=%s)', async (investor, viaController, hasToken) => {
  vi.resetModules()
  for (const key of ['DOMAIN', 'CLIENT_ID', 'AUDIENCE']) vi.stubEnv('VITE_AUTH0_' + key, 'configured')
  const sdkLogout = vi.fn()
  const createAuth0Client = vi.fn(async () => ({ logout: sdkLogout }))
  vi.doMock('@auth0/auth0-spa-js', () => ({ createAuth0Client }))
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false })))
  const { logout } = await import('./auth.js')
  const realWindow = window
  const origin = realWindow.location.origin
  const reload = vi.fn()
  vi.stubGlobal('window', new Proxy(realWindow, {
    get(target, key) { return key === 'location' ? { origin, reload } : Reflect.get(target, key, target) },
  }))
  const payload = btoa(JSON.stringify({ iss: investor ? INVESTOR_ISSUER : 'https://human.auth0.com/' })).replace(/=+$/, '')
  if (hasToken) localStorage.setItem('leaf.jwt', 'e30.' + payload + '.c2ln')
  if (investor) localStorage.setItem('leaf.investorDemo.org', 'workspace')
  const draft = composerDraftKey('account:visitor', '/app')
  localStorage.setItem(draft, 'private draft')
  localStorage.setItem('leaf.inflightAuthor.v1', 'private pointer')
  localStorage.setItem('leaf.org_id', 'workspace')
  localStorage.setItem('unrelated', 'keep')
  if (viaController) {
    const controller = createSessionController({ storage: localStorage, endSession: logout })
    await controller.actions.signOut()
    expect(controller.getSnapshot().reason).toBe('signed_out')
  } else {
    await logout()
  }
  expect(localStorage.getItem('leaf.jwt')).toBeNull()
  expect(localStorage.getItem('leaf.inflightAuthor.v1')).toBeNull()
  expect(localStorage.getItem(draft)).toBeNull()
  expect(localStorage.getItem('unrelated')).toBe('keep')
  if (investor) {
    expect(localStorage.getItem('leaf.org_id')).toBeNull()
    expect(localStorage.getItem('leaf.investorDemo.org')).toBeNull()
    expect(createAuth0Client).not.toHaveBeenCalled()
    expect(sdkLogout).not.toHaveBeenCalled()
    expect(reload).toHaveBeenCalledOnce()
  } else {
    expect(localStorage.getItem('leaf.org_id')).toBe('workspace')
    expect(sdkLogout).toHaveBeenCalledExactlyOnceWith({ logoutParams: { returnTo: origin } })
    expect(reload).not.toHaveBeenCalled()
  }
})
