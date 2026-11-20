// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { bootstrapServiceAuth, serviceReauth } from './auth.js'
import { serviceScope, serviceSession, serviceState } from './lib/serviceAuthSession.js'
import { stageCheckoutAuthReturn } from './checkoutIdentity.js'

const RETURN_KEY = 'leaf.service.reauth_return'
const CHECKOUT_RETURN_KEY = 'leaf.checkout_auth_return'
const CHECKOUT_COMPLETE_KEY = 'leaf.checkout_auth_complete'
const SELECTOR_KEY = 'leaf.service.selector'
const OLD_SELECTOR = '11111111-1111-4111-8111-111111111111'
const NEW_SELECTOR = '22222222-2222-4222-8222-222222222222'
const identity = {
  iss: 'https://leafautomation.us.auth0.com/',
  subject: 'machine@clients',
  sub: 'machine@clients',
  org_id: 'service-org',
  tenant_id: 'tenant',
  principal_type: 'service_admin',
}
const route = '/app?project=solar&drawing=roof#design'
let originalAddress

beforeEach(() => {
  serviceSession().human()
  sessionStorage.clear()
  originalAddress = window.location.pathname + window.location.search + window.location.hash
  window.history.replaceState({}, '', route)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  serviceSession().human()
  sessionStorage.clear()
  window.history.replaceState({}, '', originalAddress)
})

function mockProof(principal = identity, { ok = true } = {}) {
  const fetcher = vi.fn(async url => {
    expect(url).toBe('/api/auth/agent/session')
    return {
      ok,
      status: ok ? 200 : 401,
      json: async () => ({
        authenticated: true,
        binding_id: serviceState().selector,
        expires_at: new Date(Date.now() + 60000).toISOString(),
        identity: principal,
      }),
    }
  })
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}

function mockNavigation() {
  const browserWindow = window
  const assign = vi.fn()
  vi.stubGlobal('window', new Proxy(browserWindow, {
    get(target, key) {
      if (key === 'location') return {
        href: target.location.href,
        origin: target.location.origin,
        pathname: target.location.pathname,
        search: target.location.search,
        hash: target.location.hash,
        assign,
      }
      return Reflect.get(target, key, target)
    },
  }))
  return assign
}

function recordReturn(overrides = {}) {
  const record = {
    v: 1,
    created_at_ms: Date.now(),
    return_to: route,
    scope: JSON.stringify([identity.iss, identity.sub, identity.org_id]),
    ...overrides,
  }
  sessionStorage.setItem(RETURN_KEY, JSON.stringify(record))
  stageCheckoutAuthReturn()
  return record
}

async function prepareReturn(overrides = {}) {
  recordReturn(overrides)
  window.history.replaceState({}, '', '/')
  serviceSession().select(NEW_SELECTOR)
  return mockProof()
}

it('does nothing in human mode', () => {
  const assign = mockNavigation()
  expect(serviceReauth()).toBe(false)
  expect(sessionStorage.length).toBe(0)
  expect(assign).not.toHaveBeenCalled()
})

it('does nothing for a retired service scope', () => {
  serviceSession().select(OLD_SELECTOR)
  const before = { ...sessionStorage }
  const assign = mockNavigation()
  expect(serviceReauth({ isCurrent: () => false })).toBe(false)
  expect({ ...sessionStorage }).toEqual(before)
  expect(assign).not.toHaveBeenCalled()
})

it('records the verified scope and exact route, stages checkout, and retains the old session', async () => {
  serviceSession().select(OLD_SELECTOR)
  mockProof()
  expect(await bootstrapServiceAuth()).toBe(true)
  const scope = serviceScope()
  const before = serviceState()
  const human = vi.spyOn(serviceSession(), 'human')
  const logout = vi.spyOn(serviceSession(), 'logout')
  vi.spyOn(Date, 'now').mockReturnValue(Date.now())
  const assign = mockNavigation()
  expect(serviceReauth()).toBe(true)
  expect(JSON.parse(sessionStorage.getItem(RETURN_KEY))).toEqual({
    v: 1, created_at_ms: Date.now(), return_to: route, scope,
  })
  expect(JSON.parse(sessionStorage.getItem(CHECKOUT_RETURN_KEY))).toEqual({
    v: 1, created_at_ms: Date.now(),
  })
  expect(assign).toHaveBeenCalledExactlyOnceWith('/api/auth/agent/login')
  expect(sessionStorage.getItem(SELECTOR_KEY)).toBe(OLD_SELECTOR)
  expect(serviceState()).toEqual(before)
  expect(human).not.toHaveBeenCalled()
  expect(logout).not.toHaveBeenCalled()
})

it('records a null scope before identity has been verified', () => {
  serviceSession().select(OLD_SELECTOR)
  mockNavigation()
  expect(serviceReauth()).toBe(true)
  expect(JSON.parse(sessionStorage.getItem(RETURN_KEY)).scope).toBeNull()
  expect(sessionStorage.getItem(SELECTOR_KEY)).toBe(OLD_SELECTOR)
})

it('restores the same principal route and checkout once after verifying the new selector', async () => {
  recordReturn()
  window.history.replaceState({}, '', `/?service_session=${NEW_SELECTOR}`)
  const fetcher = mockProof()
  const replace = vi.spyOn(window.history, 'replaceState')
  expect(await bootstrapServiceAuth()).toBe(true)
  expect(fetcher.mock.calls[0][1]).toMatchObject({
    credentials: 'same-origin', headers: { Authorization: `LeafServiceSession ${NEW_SELECTOR}` },
  })
  expect(replace).toHaveBeenLastCalledWith({}, document.title, route)
  expect(window.location.pathname + window.location.search + window.location.hash).toBe(route)
  expect(sessionStorage.getItem(CHECKOUT_RETURN_KEY)).toBeNull()
  expect(JSON.parse(sessionStorage.getItem(CHECKOUT_COMPLETE_KEY))).toMatchObject({ v: 1 })
  expect(sessionStorage.getItem(RETURN_KEY)).toBeNull()
  window.history.replaceState({}, '', '/other')
  replace.mockClear()
  expect(await bootstrapServiceAuth()).toBe(true)
  expect(replace).not.toHaveBeenCalled()
})

it.each([
  { iss: 'different-issuer' },
  { sub: 'different-machine', subject: 'different-machine' },
  { org_id: 'different-org' },
])('does not pass the route or checkout to a different principal: %j', async changes => {
  await prepareReturn()
  mockProof({ ...identity, ...changes })
  const replace = vi.spyOn(window.history, 'replaceState')
  expect(await bootstrapServiceAuth()).toBe(true)
  expect(replace).not.toHaveBeenCalled()
  expect(window.location.pathname).toBe('/')
  expect(sessionStorage.getItem(CHECKOUT_RETURN_KEY)).toBeNull()
  expect(sessionStorage.getItem(CHECKOUT_COMPLETE_KEY)).toBeNull()
  expect(sessionStorage.getItem(RETURN_KEY)).toBeNull()
})

it.each([
  ['stale', { created_at_ms: Date.now() - 15 * 60 * 1000 - 10000 }],
  ['future', { created_at_ms: Date.now() + 60000 }],
  ['null scope', { scope: null }],
  ['invalid version', { v: 2 }],
  ['invalid timestamp', { created_at_ms: null }],
  ['object timestamp', { created_at_ms: { valueOf: null, toString: null } }],
])('discards a %s record without restoring the URL or checkout', async (_, overrides) => {
  await prepareReturn(overrides)
  const replace = vi.spyOn(window.history, 'replaceState')
  expect(await bootstrapServiceAuth()).toBe(true)
  expect(replace).not.toHaveBeenCalled()
  expect(sessionStorage.getItem(RETURN_KEY)).toBeNull()
  expect(sessionStorage.getItem(CHECKOUT_RETURN_KEY)).toBeNull()
  expect(sessionStorage.getItem(CHECKOUT_COMPLETE_KEY)).toBeNull()
})

it.each(['{bad json', 'null', '[]', null])('discards malformed or missing record %j', async raw => {
  await prepareReturn()
  if (raw === null) sessionStorage.removeItem(RETURN_KEY)
  else sessionStorage.setItem(RETURN_KEY, raw)
  const replace = vi.spyOn(window.history, 'replaceState')
  expect(await bootstrapServiceAuth()).toBe(true)
  expect(replace).not.toHaveBeenCalled()
  expect(sessionStorage.getItem(RETURN_KEY)).toBeNull()
  expect(sessionStorage.getItem(CHECKOUT_RETURN_KEY)).toBeNull()
  expect(sessionStorage.getItem(CHECKOUT_COMPLETE_KEY)).toBeNull()
})

it.each(['//evil.example/x', 'https://evil.example/'])('restores unsafe return target %s to /', async returnTo => {
  await prepareReturn({ return_to: returnTo })
  const replace = vi.spyOn(window.history, 'replaceState')
  expect(await bootstrapServiceAuth()).toBe(true)
  expect(replace).toHaveBeenCalledExactlyOnceWith({}, document.title, '/')
  expect(sessionStorage.getItem(CHECKOUT_COMPLETE_KEY)).not.toBeNull()
  expect(sessionStorage.getItem(RETURN_KEY)).toBeNull()
})

it.each([0, 15 * 60 * 1000])('accepts a same-principal record at age %i ms', async age => {
  const now = Date.now()
  vi.spyOn(Date, 'now').mockReturnValue(now)
  await prepareReturn({ created_at_ms: now - age })
  const replace = vi.spyOn(window.history, 'replaceState')
  expect(await bootstrapServiceAuth()).toBe(true)
  expect(replace).toHaveBeenCalledExactlyOnceWith({}, document.title, route)
  expect(sessionStorage.getItem(CHECKOUT_COMPLETE_KEY)).not.toBeNull()
  expect(sessionStorage.getItem(RETURN_KEY)).toBeNull()
})

it('leaves both return markers intact when the probe fails', async () => {
  await prepareReturn()
  const record = sessionStorage.getItem(RETURN_KEY)
  const checkout = sessionStorage.getItem(CHECKOUT_RETURN_KEY)
  mockProof(identity, { ok: false })
  const replace = vi.spyOn(window.history, 'replaceState')
  expect(await bootstrapServiceAuth()).toBe(false)
  expect(replace).not.toHaveBeenCalled()
  expect(sessionStorage.getItem(RETURN_KEY)).toBe(record)
  expect(sessionStorage.getItem(CHECKOUT_RETURN_KEY)).toBe(checkout)
  expect(sessionStorage.getItem(CHECKOUT_COMPLETE_KEY)).toBeNull()
})
