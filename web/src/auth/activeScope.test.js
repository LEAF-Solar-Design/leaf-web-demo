// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { setStoredOrgId } from '../api.js'
import { notifyActiveScopeChanged, readActiveScope, subscribeActiveScope } from './activeScope.js'
import { serviceSession } from '../lib/serviceAuthSession.js'

const token = (claims) => `header.${btoa(JSON.stringify(claims))}.signature`

it('uses only verified service issuer/subject/org and preserves injected human scopes', async () => {
  const service = serviceSession()
  const selector = '11111111-1111-4111-8111-111111111111'
  localStorage.setItem('leaf.jwt', token({ iss: 'human', sub: 'peer', org_id: 'peer-org' }))
  localStorage.setItem('leaf.org_id', 'peer-org')
  try {
    service.select(selector)
    expect(readActiveScope()).toBeNull()
    const foreign = { getItem: key => key === 'leaf.jwt' ? token({ iss: 'injected', sub: 'test' }) : null }
    expect(readActiveScope(foreign)).toBe(JSON.stringify(['injected', 'test', '']))
    const identity = { iss: 'https://leafautomation.us.auth0.com/', subject: 'machine@clients',
      sub: 'machine@clients', org_id: 'real-org', tenant_id: 'real-tenant', principal_type: 'service_admin' }
    await service.probe({ origin: window.location.origin, fetchImpl: async () => ({ ok: true, json: async () => ({
      authenticated: true, identity, binding_id: selector, expires_at: new Date(Date.now() + 60000).toISOString(),
    }) }) })
    const scope = JSON.stringify([identity.iss, identity.sub, identity.org_id])
    expect(readActiveScope()).toBe(scope)
    expect(localStorage.getItem('leaf.org_id')).toBe('peer-org')
    service.logout()
    expect(readActiveScope()).toBeNull()
  } finally { service.human() }
})

afterEach(() => { vi.restoreAllMocks(); localStorage.clear() })

it('returns null when signed out', () => {
  expect(readActiveScope()).toBeNull()
})

it('returns null without a usable subject or with malformed claims', () => {
  for (const value of ['opaque', 'header.!.signature', token(null), token({}), token({ sub: ' ' })]) {
    localStorage.setItem('leaf.jwt', value)
    expect(readActiveScope()).toBeNull()
  }
})

it('never throws when storage is unavailable', () => {
  expect(readActiveScope({ getItem() { throw new Error('Unavailable') } })).toBeNull()
  expect(readActiveScope(null)).toBeNull()
  vi.spyOn(globalThis, 'localStorage', 'get').mockImplementation(() => { throw new Error('Unavailable') })
  expect(readActiveScope()).toBeNull()
})

it('uses the stored org before the token org claim', () => {
  localStorage.setItem('leaf.jwt', token({ iss: 'studio', sub: 'alice', org_id: 'token-org' }))
  localStorage.setItem('leaf.org_id', 'stored-org')
  expect(readActiveScope()).toBe(JSON.stringify(['studio', 'alice', 'stored-org']))
})

it('uses the token org claim when no org is stored', () => {
  localStorage.setItem('leaf.jwt', token({ iss: 'studio', sub: 'alice', org_id: 'token-org' }))
  expect(readActiveScope()).toBe(JSON.stringify(['studio', 'alice', 'token-org']))
})

it('preserves an explicitly stored empty org', () => {
  localStorage.setItem('leaf.jwt', token({ iss: 'studio', sub: 'alice', org_id: 'token-org' }))
  localStorage.setItem('leaf.org_id', '')
  expect(readActiveScope()).toBe(JSON.stringify(['studio', 'alice', '']))
})

it('uses an empty org and issuer when neither is present', () => {
  localStorage.setItem('leaf.jwt', token({ sub: 'alice' }))
  expect(readActiveScope()).toBe(JSON.stringify(['', 'alice', '']))
})

it('notifies in the same tab after setStoredOrgId writes and removes the org', () => {
  localStorage.setItem('leaf.jwt', token({ iss: 'studio', sub: 'alice', org_id: 'token-org' }))
  const listener = vi.fn(() => readActiveScope())
  const unsubscribe = subscribeActiveScope(listener)
  try {
    setStoredOrgId('stored-org')
    setStoredOrgId(null)
    expect(listener.mock.results.map(result => result.value)).toEqual([
      JSON.stringify(['studio', 'alice', 'stored-org']),
      JSON.stringify(['studio', 'alice', 'token-org']),
    ])
  } finally { unsubscribe() }
  notifyActiveScopeChanged()
  expect(listener).toHaveBeenCalledTimes(2)
})

it('notifies for a cross-tab token change', () => {
  const listener = vi.fn()
  const unsubscribe = subscribeActiveScope(listener)
  try {
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' }))
    expect(listener).toHaveBeenCalledTimes(1)
  } finally { unsubscribe() }
})

it('notifies for a cross-tab org change', () => {
  const listener = vi.fn()
  const unsubscribe = subscribeActiveScope(listener)
  try {
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.org_id' }))
    expect(listener).toHaveBeenCalledTimes(1)
  } finally { unsubscribe() }
})

it('notifies for a cross-tab storage clear', () => {
  const listener = vi.fn()
  const unsubscribe = subscribeActiveScope(listener)
  try {
    window.dispatchEvent(new StorageEvent('storage', { key: null }))
    expect(listener).toHaveBeenCalledTimes(1)
  } finally { unsubscribe() }
})

it('ignores cross-tab changes to unrelated keys', () => {
  const listener = vi.fn()
  const unsubscribe = subscribeActiveScope(listener)
  try {
    window.dispatchEvent(new StorageEvent('storage', { key: 'unrelated' }))
    expect(listener).not.toHaveBeenCalled()
  } finally { unsubscribe() }
})

it('stops same-tab and cross-tab notifications after unsubscribe', () => {
  const listener = vi.fn()
  const unsubscribe = subscribeActiveScope(listener)
  unsubscribe()
  notifyActiveScopeChanged()
  window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' }))
  window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.org_id' }))
  window.dispatchEvent(new StorageEvent('storage', { key: null }))
  expect(listener).not.toHaveBeenCalled()
})
