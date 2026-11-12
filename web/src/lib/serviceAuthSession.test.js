// @vitest-environment node
const { test } = await import(process.env.VITEST ? 'vitest' : 'node:test')
import assert from 'node:assert/strict'
import { createServiceAuthSession, serviceSession, serviceState, serviceHeaders,
  authorityStorage, serviceRequestAuthority, invalidateServiceResponse, rememberServiceResponse } from './serviceAuthSession.js'

const selector = '11111111-1111-4111-8111-111111111111'
const other = '22222222-2222-4222-8222-222222222222'
const origin = 'https://studio.example'
const identity = { subject: 'machine@clients', sub: 'machine@clients',
  iss: 'https://leafautomation.us.auth0.com/', org_id: 'org-a',
  principal_type: 'service_admin', tenant_id: 'tenant-a' }
function storage() {
  const values = new Map()
  return { get length() { return values.size }, key: i => [...values.keys()][i] ?? null,
    getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key), values }
}
const proof = (value = identity, expires = Date.now() + 60000) => ({ ok: true,
  json: async () => ({ authenticated: true, identity: value, binding_id: selector, expires_at: new Date(expires).toISOString() }) })

test('a selector, saved selector, and failed joint proof confer no identity', async () => {
  const tab = storage()
  tab.setItem('leaf.jwt', 'human-token')
  const session = createServiceAuthSession({ storage: tab })
  session.select(selector)
  assert.equal(session.snapshot().identity, null)
  const reloaded = createServiceAuthSession({ storage: tab })
  assert.equal(reloaded.snapshot().mode, true)
  assert.equal(reloaded.snapshot().identity, null)
  assert.equal(await reloaded.probe({ origin, fetchImpl: async () => ({ ok: false, status: 401 }) }), false)
  assert.equal(reloaded.snapshot().identity, null)
  assert.equal(reloaded.snapshot().mode, true)
  assert.equal(tab.getItem('leaf.jwt'), 'human-token')
})

test('only a bounded successful server proof supplies sanitized real identity', async () => {
  const tab = storage()
  const session = createServiceAuthSession({ storage: tab })
  session.select(selector)
  let request
  assert.equal(await session.probe({ origin, fetchImpl: async (url, init) => {
    request = { url, init }
    return proof({ ...identity, access_token: 'must-not-store' })
  } }), true)
  assert.equal(request.url, '/api/auth/agent/session')
  assert.equal(request.init.credentials, 'same-origin')
  assert.equal(request.init.headers.Authorization, `LeafServiceSession ${selector}`)
  assert.deepEqual(session.snapshot().identity, identity)
  assert.ok(Object.isFrozen(session.snapshot().identity))
  assert.equal([...tab.values.values()].some(value => value.includes(identity.sub)), false)
  session.logout()
})

test('expiry removes authority before observers run and keeps explicit service mode', async () => {
  let now = 1000
  const session = createServiceAuthSession({ storage: storage(), now: () => now })
  session.select(selector)
  await session.probe({ origin, fetchImpl: async () => proof(identity, 2000) })
  let observed
  session.subscribe(() => { observed = session.snapshot().identity })
  now = 2001
  assert.equal(session.snapshot().identity, null)
  assert.equal(observed, null)
  assert.equal(session.snapshot().mode, true)
})

test('invalid body, unbounded response body, and foreign origin fail closed', async () => {
  for (const fetchImpl of [async () => proof({ ...identity, sub: 'different' }),
    async () => ({ ok: true, json: () => new Promise(() => {}) })]) {
    const session = createServiceAuthSession({ storage: storage() })
    session.select(selector)
    assert.equal(await session.probe({ origin, fetchImpl, timeoutMs: 5 }), false)
    assert.equal(session.snapshot().identity, null)
  }
  const session = createServiceAuthSession({ storage: storage() })
  session.select(selector)
  let fetched = false
  await assert.rejects(session.probe({ origin, apiBase: 'https://other.example', fetchImpl: async () => { fetched = true } }), /same-origin/)
  assert.equal(fetched, false)
  assert.equal(session.snapshot().identity, null)
})

test('a late proof cannot undo logout or replace a newer selector', async () => {
  const session = createServiceAuthSession({ storage: storage() })
  session.select(selector)
  let complete
  const pending = session.probe({ origin, fetchImpl: () => new Promise(resolve => { complete = resolve }) })
  session.logout()
  complete(proof())
  assert.equal(await pending, false)
  assert.equal(session.snapshot().identity, null)
  session.select(other)
  assert.equal(session.invalidate({ selector, generation: 1 }), false)
  assert.equal(session.snapshot().selector, other)
})

test('tabs and explicitly injected storage never share proof or alter human JWTs', async () => {
  const tabA = storage(), tabB = storage()
  const a = createServiceAuthSession({ storage: tabA }), b = createServiceAuthSession({ storage: tabB })
  tabA.setItem('leaf.jwt', 'human-a')
  tabB.setItem('leaf.jwt', 'human-b')
  a.select(selector)
  await a.probe({ origin, fetchImpl: async () => proof() })
  assert.equal(b.snapshot().mode, false)
  assert.equal(b.snapshot().identity, null)
  b.select(selector)
  await b.probe({ origin, fetchImpl: async () => proof() })
  assert.equal(a.invalidate(b.snapshot()), false)
  b.logout()
  a.logout()
  assert.equal(a.snapshot().identity, null)
  assert.equal(a.snapshot().mode, true)
  assert.equal(tabA.getItem('leaf.jwt'), 'human-a')
  assert.equal(tabB.getItem('leaf.jwt'), 'human-b')
})

test('exact response generations fence same-selector renewal and isolate service persistence', async () => {
  const oldTab = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const oldLocal = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  const oldLocation = Object.getOwnPropertyDescriptor(globalThis, 'location')
  const tab = storage(), human = storage()
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: tab })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: human })
  Object.defineProperty(globalThis, 'location', { configurable: true, value: { origin } })
  try {
    human.setItem('leaf.jwt', 'human')
    human.setItem('leaf.org_id', 'human-org')
    const session = serviceSession()
    session.select(selector)
    assert.deepEqual(serviceHeaders(), {})
    assert.throws(() => serviceRequestAuthority(), { status: 401 })
    await session.probe({ origin, fetchImpl: async () => proof() })
    const sent = serviceState()
    const response = rememberServiceResponse({ status: 401 }, sent)
    const headers = serviceHeaders()
    assert.equal(headers.Authorization, `LeafServiceSession ${selector}`)
    assert.equal(headers['X-Tenant-Id'], 'tenant-a')
    assert.equal(serviceState(storage()).mode, false)
    const scoped = authorityStorage()
    scoped.setItem('leaf.org_id', 'org-a')
    assert.equal(human.getItem('leaf.org_id'), 'human-org')
    assert.equal(tab.getItem('leaf.org_id'), null)
    assert.equal(scoped.getItem('leaf.org_id'), 'org-a')
    await session.probe({ origin, fetchImpl: async () => proof() })
    assert.equal(invalidateServiceResponse(response, headers.Authorization), false)
    assert.ok(serviceState().identity)
    assert.equal(invalidateServiceResponse({}, undefined, serviceState()), false)
    assert.equal(invalidateServiceResponse({}, headers.Authorization, serviceState()), true)
    assert.equal(serviceState().identity, null)
    assert.deepEqual(serviceHeaders(), {})
    scoped.setItem('late', 'must-not-save')
    assert.equal(scoped.getItem('late'), null)
    assert.equal(human.getItem('leaf.jwt'), 'human')
    session.human()
  } finally {
    for (const [key, descriptor] of [['sessionStorage', oldTab], ['localStorage', oldLocal], ['location', oldLocation]]) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
})
