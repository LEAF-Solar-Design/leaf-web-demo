import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ReadableStream } from 'node:stream/web'
import { TextDecoder, TextEncoder } from 'node:util'
import { createIntegrationsClient } from './client.js'
import { authHeaders, noteUnauthorized } from '../api.js'
import { serviceSession, serviceHeaders, invalidateServiceResponse } from '../lib/serviceAuthSession.js'

vi.mock('../api.js', () => ({ authHeaders: vi.fn(), noteUnauthorized: vi.fn() }))

const local = 'https://platform-staging.leafdesign.ai'
const upstream = 'https://integrations.example'
const projectId = '11111111-1111-4111-8111-111111111111'
const selector = '22222222-2222-4222-8222-222222222222'
const scope = {
  tenantId: projectId, projectId, platformUserId: projectId, bindingId: selector,
  projectRole: 'owner', externalAuthority: 'auth0', externalSubject: 'fixture-service@clients',
}

function response(status = 200) {
  const bytes = new TextEncoder().encode(JSON.stringify(scope))
  return {
    status, ok: status >= 200 && status < 300,
    headers: { get: (name) => name === 'content-type' ? 'application/json' : null },
    body: new ReadableStream({ start(controller) { controller.enqueue(bytes); controller.close() } }),
  }
}

function setup() {
  const fetchImpl = vi.fn(async () => response())
  return { fetchImpl, client: createIntegrationsClient({ origin: upstream, fetchImpl }),
    options: { signal: new AbortController().signal } }
}

async function verifiedService(value = selector) {
  const session = serviceSession()
  session.select(value)
  expect(await session.probe({ origin: local, fetchImpl: async () => ({ ok: true,
    json: async () => ({ authenticated: true, binding_id: value,
      expires_at: new Date(Date.now() + 60000).toISOString(),
      identity: { tenant_id: projectId, subject: scope.externalSubject, sub: scope.externalSubject,
        iss: 'https://issuer.example', org_id: null, principal_type: 'service' } }),
  }) })).toBe(true)
  authHeaders.mockReturnValue(serviceHeaders())
  return session.snapshot()
}

beforeEach(() => {
  vi.stubGlobal('TextEncoder', TextEncoder)
  vi.stubGlobal('TextDecoder', TextDecoder)
  vi.stubGlobal('location', { origin: local })
  const storage = new Map()
  vi.stubGlobal('sessionStorage', {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key),
  })
  authHeaders.mockReturnValue({ Authorization: 'Bearer fixture.invalid.signature' })
  // Integrations pass fatal:false, so api.js reports a resource 401 without
  // clearing session authority. The response still carries its captured owner.
  noteUnauthorized.mockImplementation(res => res)
})

afterEach(() => { serviceSession().human(); vi.clearAllMocks(); vi.unstubAllGlobals() })

it('sends verified service-session authority only to the same-origin bridge', async () => {
  await verifiedService()
  const { client, fetchImpl, options } = setup()
  await expect(client.getScope(projectId, options)).resolves.toEqual(scope)
  expect(fetchImpl).toHaveBeenCalledTimes(1)
  const [target, request] = fetchImpl.mock.calls[0]
  const url = new URL(target, local)
  expect(url.origin).toBe(local)
  expect(url.pathname).toBe(`/api/v1/projects/${projectId}/scope`)
  expect(request).toMatchObject({ credentials: 'same-origin', redirect: 'error', cache: 'no-store',
    headers: { Authorization: `LeafServiceSession ${selector}`, 'X-Leaf-Integration-Origin': upstream } })
  expect(request.headers).not.toHaveProperty('Cookie')
})

it('keeps ordinary bearer clients on their configured external transport', async () => {
  const { client, fetchImpl, options } = setup()
  await expect(client.getScope(projectId, options)).resolves.toEqual(scope)
  const [target, request] = fetchImpl.mock.calls[0]
  expect(target).toBe(`${upstream}/api/v1/projects/${projectId}/scope`)
  expect(request).toMatchObject({ credentials: 'omit', redirect: 'error', cache: 'no-store',
    headers: { Authorization: 'Bearer fixture.invalid.signature' } })
  expect(request.headers).not.toHaveProperty('X-Leaf-Integration-Origin')
})

it('does not dispatch an unverified public service selector', async () => {
  serviceSession().select(selector)
  authHeaders.mockReturnValue({ Authorization: `LeafServiceSession ${selector}` })
  const { client, fetchImpl, options } = setup()
  await expect(client.getScope(projectId, options)).rejects.toMatchObject({ status: 401 })
  expect(fetchImpl).not.toHaveBeenCalled()
})

it('refuses a selector that differs from the verified request snapshot', async () => {
  await verifiedService()
  authHeaders.mockReturnValue({ Authorization: `LeafServiceSession ${projectId}` })
  const { client, fetchImpl, options } = setup()
  await expect(client.getScope(projectId, options)).rejects.toMatchObject({ status: 401 })
  expect(fetchImpl).not.toHaveBeenCalled()
})

it('records the current response authority while keeping resource errors nonfatal', async () => {
  const authority = await verifiedService()
  const { client, fetchImpl, options } = setup()
  const unauthorized = response(401)
  fetchImpl.mockResolvedValue(unauthorized)
  await expect(client.getScope(projectId, options)).rejects.toMatchObject({ status: 401 })
  expect(serviceSession().snapshot()).toEqual(authority)
  expect(noteUnauthorized).toHaveBeenCalledWith(expect.anything(), 'integrations',
    `LeafServiceSession ${selector}`, { fatal: false })
  expect(invalidateServiceResponse(unauthorized, `LeafServiceSession ${selector}`)).toBe(true)
  expect(serviceSession().snapshot()).toMatchObject({ identity: null, selector: null })
})

it('does not let a late unauthorized response invalidate the replacement service session', async () => {
  await verifiedService()
  const { client, fetchImpl, options } = setup()
  let finish
  fetchImpl.mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const pending = client.getScope(projectId, options)
  expect(fetchImpl).toHaveBeenCalledOnce()
  const replacement = await verifiedService(projectId)
  const unauthorized = response(401)
  finish(unauthorized)
  await expect(pending).rejects.toMatchObject({ status: 401 })
  expect(invalidateServiceResponse(unauthorized, `LeafServiceSession ${selector}`)).toBe(false)
  expect(serviceSession().snapshot()).toEqual(replacement)
})

it('treats the configured upstream as a server binding assertion, never a service-browser destination', async () => {
  await verifiedService()
  const configured = 'https://another-integration.example'
  const { fetchImpl, options } = setup()
  const client = createIntegrationsClient({ origin: configured, fetchImpl })
  await expect(client.getScope(projectId, options)).resolves.toEqual(scope)
  const [target, request] = fetchImpl.mock.calls[0]
  expect(new URL(target, local).origin).toBe(local)
  expect(request.headers['X-Leaf-Integration-Origin']).toBe(configured)
  expect(request.credentials).toBe('same-origin')
})
