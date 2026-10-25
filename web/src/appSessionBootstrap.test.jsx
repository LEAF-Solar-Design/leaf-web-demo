import { useEffect, useMemo, useRef, useState } from 'react'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { authHeaders, createProjectSolarClient, getSession, getSessionIdentity } from './api.js'
import { isSignedIn } from './auth.js'
import { shouldAutoDemo } from './demoState.js'
import { humanizeError } from './errorHumanize.js'
import { hasDrawingSelection } from './drawing/drawingIdentity.js'
import { classifyDrawingLoadFailure } from './drawing/loadFailure.js'
import useSessionController from './controllers/session/useSessionController.js'
import { useProjectSolarScope } from './solar/useProjectSolarScope.js'

// Execute App's actual bootstrap effects, like appProjectSolarDrawing's AST
// seam, without replacing its session state machine or mounting WebGL. Both
// identity and legacy intake use the real api.js transport against fetch.
const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
function find(predicate, root = tree) {
  const found = []
  function walk(node) {
    if (!node || typeof node.type !== 'string') return
    if (predicate(node)) found.push(node)
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value?.type) walk(value)
    }
  }
  walk(root)
  return found
}
function one(predicate, root) {
  const nodes = find(predicate, root)
  expect(nodes, 'unique production bootstrap extraction').toHaveLength(1)
  return nodes[0]
}
const text = node => source.slice(node.start, node.end)
const declaration = name => one(node => node.type === 'VariableDeclarator' && node.id.name === name)
const effectCalling = name => one(node => node.type === 'CallExpression' && node.callee.name === 'useEffect'
  && find(child => child.type === 'CallExpression' && child.callee.name === name, node).length === 1)
const proof = effectCalling('getSessionIdentity')
const drawing = effectCalling('getSession')
const bootstrapStart = one(node => node.type === 'VariableDeclaration'
  && node.declarations.some(item => item.id.name === 'sessionAuthorization')).start
const bootstrapSource = source.slice(bootstrapStart, drawing.end)
const noop = () => {}
const current = () => true
const is401 = error => error?.status === 401
const mockVersions = { reset: noop, seedBase: noop }
const U = n => `${String(n).repeat(8)}-${String(n).repeat(4)}-${String(n).repeat(4)}-${String(n).repeat(4)}-${String(n).repeat(12)}`
const identity = { tenant_id: U(1), org_id: U(1), tier: 'hosted_pro' }
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

function useBootstrap({ drawingId = 'legacy', mock = false, orgId = U(1), projectId = U(2), retry = 0,
  getDrawingVersions = async () => ({ head: 1 }) } = {}) {
  const session = useSessionController({ endSession: noop })
  const [tenant, setTenant] = useState(null)
  const [tier, setTier] = useState(null)
  const [org, setOrg] = useState(null)
  const [signedIn, setSignedIn] = useState(isSignedIn)
  const [loadErr, setLoadErr] = useState(null)
  const [drawingLoad, setDrawingLoad] = useState(null)
  const requestedDrawingIdRef = useRef(drawingId)
  requestedDrawingIdRef.current = drawingId
  const env = {
    useEffect, useMemo, useRef, useState, mock, orgId, openProjectId: projectId,
    session, sessionActions: session.actions, intakeRetryKey: retry,
    authHeaders, getSessionIdentity, getSession, setTenant, setTier, setOrg,
    setSignedIn, isSignedIn, adoptOrgId: noop, setLoadErr, humanizeError,
    is401, authConfigured: true, shouldAutoDemo, setMock: noop,
    REQUESTED_DRAWING_ID: drawingId, DRAWING_SOURCE: drawingId,
    requestedDrawingIdRef, isScopeCurrent: current, resetDrawing: noop,
    hasDrawingSelection, setDrawingLoad, resetCatalogTransient: noop,
    clearToast: noop, setDrawer: noop, clearAgentSession: noop, mockVersions,
    seatIntake: noop, isEditFixture: false, getDrawingVersions,
    classifyDrawingLoadFailure, knownGuest: false,
  }
  const identityFailure = Function(...Object.keys(env), `${bootstrapSource}; return identityFailure`)(...Object.values(env))
  // The production App projection feeds the real Solar scope owner.
  const solarSessionEnv = { mock, session, projectSolarAccountScope: 'actor-a' }
  const solarSession = Function(...Object.keys(solarSessionEnv),
    `return (${text(declaration('projectSolarSession').init)})`)(...Object.values(solarSessionEnv))
  const solar = useProjectSolarScope({ projectMode: !mock && !!projectId,
    runContext: { orgId, projectId, drawingArtifactId: U(3), drawingId: U(4), drawingVersion: null },
    session: solarSession }, { createClient: createProjectSolarClient })
  return { session, tenant, tier, org, signedIn, loadErr, drawingLoad, solar, identityFailure }
}

let fetchMock
beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('leaf.jwt', 'valid-token')
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

it('pins proof and drawing to separate effects, with token and workspace fences', () => {
  expect(proof).not.toBe(drawing)
  expect(text(proof)).not.toMatch(/REQUESTED_DRAWING_ID|DRAWING_SOURCE|getSession\(/)
  for (const effect of [proof, drawing]) expect(text(effect)).toContain('sessionProof.activate(')
  expect(text(drawing)).not.toMatch(/setTenant|setTier|setOrg/)
  const deps = text(declaration('sessionProofScope').init.arguments[1])
  for (const key of ['mock', 'orgId', 'openProjectId', 'sessionAuthorization', 'session.tokenRevision', 'session.recoveries']) {
    expect(deps).toContain(key)
  }
})

it.each(['400', 'non-json', 'empty'])('identity activates Project Solar when legacy intake is %s', async failure => {
  fetchMock.mockImplementation(async url => {
    if (url.endsWith('/api/session/identity')) return json(identity)
    if (url.includes('/api/session?')) return failure === '400'
      ? json({ error: { error_code: 'BAD_PARAMS' } }, 400)
      : new Response('<html>not intake</html>')
    throw new Error(`unexpected request: ${url}`)
  })
  const { result } = renderHook(() => useBootstrap({ drawingId: failure === 'empty' ? null : 'legacy' }))
  await waitFor(() => expect(result.current.session.status).toBe('active'))
  await waitFor(() => expect(result.current.solar.ready).toBe(true))
  expect(result.current).toMatchObject({ tenant: U(1), org: U(1), tier: 'hosted_pro' })
  expect(result.current.solar.scope).toMatchObject({ mode: 'project', reason: null })
  expect(result.current.solar.scope.authorityKey).toMatch(/^project-solar:/)
  expect(result.current.solar.projectClient.getSnapshot().authorityKey).toBe(result.current.solar.scope.authorityKey)
  const request = fetchMock.mock.calls.find(([url]) => url.endsWith('/api/session/identity'))
  const expectedHeaders = {}; expectedHeaders.Authorization = `Bearer ${'valid-token'}`
  expect(request[1]).toMatchObject({ cache: 'no-store', headers: expectedHeaders })
  const legacy = fetchMock.mock.calls.filter(([url]) => url.includes('/api/session?'))
  expect(legacy).toHaveLength(failure === 'empty' ? 0 : 1)
  if (failure === 'empty') expect(result.current.drawingLoad.state).toBe('idle')
  else await waitFor(() => expect(result.current.drawingLoad.state).toBe('failed'))
})

it('a legacy 200 activates Solar before identity finishes, and the first proof wins', async () => {
  const pending = deferred()
  fetchMock.mockImplementation(async url => url.endsWith('/api/session/identity')
    ? pending.promise : json({ intake: { dwg: 'legacy' }, ...identity }))
  const { result } = renderHook(() => useBootstrap())
  await waitFor(() => expect(result.current.drawingLoad.state).toBe('seated'))
  await waitFor(() => expect(result.current.solar.ready).toBe(true))
  expect(result.current.session.status).toBe('active')
  expect(result.current).toMatchObject({ tenant: U(1), org: U(1), tier: 'hosted_pro', loadErr: null })
  const generation = result.current.session.generation
  const authorityKey = result.current.solar.scope.authorityKey
  await act(async () => { pending.resolve(json({ ...identity, tier: 'hosted_starter' })) })
  expect(result.current.session.generation).toBe(generation)
  expect(result.current.tier).toBe('hosted_pro')
  expect(result.current.solar.scope.authorityKey).toBe(authorityKey)
})

it('an identity 200 activates before legacy intake finishes, and its proof wins', async () => {
  const pending = deferred()
  fetchMock.mockImplementation(async url => url.endsWith('/api/session/identity')
    ? json(identity) : pending.promise)
  const { result } = renderHook(() => useBootstrap())
  await waitFor(() => expect(result.current.solar.ready).toBe(true))
  expect(result.current.drawingLoad.state).toBe('pending')
  const generation = result.current.session.generation
  await act(async () => { pending.resolve(json({ intake: { dwg: 'legacy' }, ...identity, tier: 'hosted_starter' })) })
  await waitFor(() => expect(result.current.drawingLoad.state).toBe('seated'))
  expect(result.current.session.generation).toBe(generation)
  expect(result.current.tier).toBe('hosted_pro')
})

it.each(['404', 'network', 'non-json', 'timeout'])('identity %s offers recovery and leaves legacy proof available', async failure => {
  const legacy = deferred()
  fetchMock.mockImplementation(async url => {
    if (url.includes('/api/session?')) return legacy.promise
    if (failure === '404') return json({ detail: 'not found' }, 404)
    if (failure === 'non-json') return new Response('<html>fallback</html>')
    throw Object.assign(new Error(failure), { name: failure === 'timeout' ? 'AbortError' : 'TypeError' })
  })
  const { result } = renderHook(() => useBootstrap())
  await act(async () => {})
  expect(result.current).toMatchObject({ loadErr: null, session: { status: 'checking', sources: [] } })
  await act(async () => { legacy.resolve(json({ intake: { dwg: 'legacy' }, ...identity })) })
  await waitFor(() => expect(result.current.solar.ready).toBe(true))
  expect(result.current.loadErr).toBeNull()
})

it.each(['404', 'network', 'non-json', 'timeout'])('identity %s with no legacy request exposes a recovery error', async failure => {
  fetchMock.mockImplementation(async () => {
    if (failure === '404') return json({}, 404)
    if (failure === 'non-json') return new Response('<html>fallback</html>')
    throw new Error(failure)
  })
  const { result } = renderHook(() => useBootstrap({ drawingId: null }))
  await act(async () => {})
  expect(result.current).toMatchObject({ loadErr: null, session: { status: 'checking', sources: [] } })
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(result.current.identityFailure.message).toEqual(expect.any(String))
})

it.each(['identity', 'legacy'])('a %s 401 requires auth and a later other-route 200 cannot reopen it', async refused => {
  const pending = deferred()
  const getDrawingVersions = vi.fn(async () => ({ head: 1 }))
  fetchMock.mockImplementation(async url => {
    const route = url.endsWith('/api/session/identity') ? 'identity' : 'legacy'
    return route === refused ? json({ detail: 'expired' }, 401) : json(await pending.promise)
  })
  const { result } = renderHook(() => useBootstrap({ getDrawingVersions }))
  await waitFor(() => expect(result.current.session.status).toBe('required'))
  expect(result.current.session.sources.map(source => source.split('?')[0]))
    .toContain(refused === 'identity' ? '/api/session/identity' : '/api/session')
  expect(localStorage.getItem('leaf.jwt')).toBeNull()
  await act(async () => { pending.resolve({ intake: { dwg: 'legacy' }, ...identity }) })
  expect(result.current.session.status).toBe('required')
  expect(result.current.solar.scope.authorityKey).toBeNull()
  if (refused === 'identity') {
    expect(result.current.drawingLoad.state).not.toBe('seated')
    expect(getDrawingVersions).not.toHaveBeenCalled()
  }
})

it('an identity 401 while legacy versions are pending prevents seating', async () => {
  const refusal = deferred(), versions = deferred()
  const getDrawingVersions = vi.fn(() => versions.promise)
  fetchMock.mockImplementation(async url => url.endsWith('/api/session/identity')
    ? json(await refusal.promise, 401)
    : json({ intake: { dwg: 'legacy' }, ...identity }))
  const { result } = renderHook(() => useBootstrap({ getDrawingVersions }))
  await waitFor(() => expect(getDrawingVersions).toHaveBeenCalledExactlyOnceWith(false, 'legacy'))
  expect(result.current.session.status).toBe('active')
  expect(result.current.drawingLoad.state).toBe('pending')
  await act(async () => { refusal.resolve({ detail: 'expired' }) })
  await waitFor(() => expect(result.current.session.status).toBe('required'))
  expect(result.current.session.sources).toContain('/api/session/identity')
  expect(localStorage.getItem('leaf.jwt')).toBeNull()
  await act(async () => { versions.resolve({ head: 1 }) })
  expect(result.current.session.status).toBe('required')
  expect(result.current.solar.scope.authorityKey).toBeNull()
  expect(result.current.drawingLoad.state).not.toBe('seated')
  expect(getDrawingVersions).toHaveBeenCalledTimes(1)
})

it.each(['200', '401'])('a stale legacy %s after sign-out cannot reopen or relabel the latch', async status => {
  const pending = deferred()
  fetchMock.mockImplementation(url => url.includes('/api/session?') ? pending.promise : new Promise(() => {}))
  const { result } = renderHook(() => useBootstrap())
  await act(async () => { await result.current.session.actions.signOut() })
  await act(async () => { pending.resolve(json({ intake: { dwg: 'legacy' }, ...identity }, Number(status))) })
  expect(result.current.session).toMatchObject({ status: 'required', reason: 'signed_out', generation: 0 })
  expect(result.current.tenant).toBeNull()
  expect(result.current.solar.scope.authorityKey).toBeNull()
})

it.each(['200', '401'])('a stale legacy %s cannot affect a refreshed proof before React cleanup', async status => {
  const pending = deferred(), fresh = deferred()
  let legacyCalls = 0, identityCalls = 0
  fetchMock.mockImplementation(url => {
    if (url.includes('/api/session?')) return ++legacyCalls === 1 ? pending.promise : new Promise(() => {})
    return ++identityCalls === 1 ? new Promise(() => {}) : fresh.promise
  })
  const { result, rerender } = renderHook(() => useBootstrap())
  localStorage.setItem('leaf.jwt', 'new-token')
  await act(async () => { pending.resolve(json({ intake: { dwg: 'legacy' }, ...identity }, Number(status))) })
  expect(result.current.session).toMatchObject({ status: 'checking', generation: 0 })
  expect(localStorage.getItem('leaf.jwt')).toBe('new-token')
  rerender()
  await act(async () => { fresh.resolve(json(identity)) })
  await waitFor(() => expect(result.current.solar.ready).toBe(true))
  const legacy = fetchMock.mock.calls.filter(([url]) => url.includes('/api/session?'))
  expect(legacy).toHaveLength(2)
  expect(legacy[1][1].headers.Authorization).toBe('Bearer new-token')
})

it('superseded legacy scope cannot activate a new project', async () => {
  const body = deferred()
  fetchMock.mockImplementation(async () => json(await body.promise))
  const { result, rerender } = renderHook(props => useBootstrap(props), { initialProps: { projectId: U(2) } })
  rerender({ projectId: U(5), drawingId: null })
  await act(async () => { body.resolve({ intake: { dwg: 'legacy' }, ...identity }) })
  // Only the new project's identity request can activate; the old legacy
  // response cannot add another generation or seat its drawing.
  await waitFor(() => expect(result.current.session.status).toBe('active'))
  expect(result.current.session.generation).toBe(1)
  expect(result.current.drawingLoad.state).toBe('idle')
})

it.each(['200', '401'])('a stale identity %s after sign-out cannot reopen or relabel the latch', async status => {
  const pending = deferred()
  fetchMock.mockReturnValue(pending.promise)
  const { result } = renderHook(() => useBootstrap({ drawingId: null }))
  await act(async () => { await result.current.session.actions.signOut() })
  await act(async () => { pending.resolve(json(identity, Number(status))) })
  expect(result.current.session).toMatchObject({ status: 'required', reason: 'signed_out', generation: 0 })
  expect(result.current.solar.scope.authorityKey).toBeNull()
  expect(result.current.tenant).toBeNull()
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it.each(['200', '401'])('a stale identity %s for an old token cannot affect the new proof', async status => {
  const oldProof = deferred(), newProof = deferred()
  fetchMock.mockReturnValueOnce(oldProof.promise).mockReturnValueOnce(newProof.promise)
  const { result, rerender } = renderHook(() => useBootstrap({ drawingId: null }))
  // The response can arrive before React observes the token write or cleans
  // up the old effect. The transport and proof both compare the actual token.
  localStorage.setItem('leaf.jwt', 'new-token')
  await act(async () => { oldProof.resolve(json(identity, Number(status))) })
  expect(result.current.session.status).toBe('checking')
  expect(result.current.session.generation).toBe(0)
  expect(localStorage.getItem('leaf.jwt')).toBe('new-token')
  rerender()
  await act(async () => { newProof.resolve(json({ ...identity, tier: 'hosted_starter' })) })
  await waitFor(() => expect(result.current.session.status).toBe('active'))
  expect(result.current.tier).toBe('hosted_starter')
  expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe('Bearer new-token')
})

it.each(['200', '401'])('a superseded identity %s cannot overwrite an already active refreshed session', async status => {
  const oldProof = deferred(), freshProof = deferred()
  fetchMock.mockReturnValueOnce(oldProof.promise).mockReturnValueOnce(freshProof.promise)
  const { result, rerender } = renderHook(() => useBootstrap({ drawingId: null }))
  localStorage.setItem('leaf.jwt', 'refreshed-token')
  rerender()
  await act(async () => { freshProof.resolve(json({ ...identity, tier: 'hosted_starter' })) })
  await waitFor(() => expect(result.current.solar.ready).toBe(true))
  const authorityKey = result.current.solar.scope.authorityKey
  const generation = result.current.session.generation
  await act(async () => { oldProof.resolve(json(identity, Number(status))) })
  expect(result.current.session.status).toBe('active')
  expect(result.current.session.generation).toBe(generation)
  expect(result.current.tier).toBe('hosted_starter')
  expect(result.current.solar.scope.authorityKey).toBe(authorityKey)
  expect(localStorage.getItem('leaf.jwt')).toBe('refreshed-token')
})

it('project and tenant switches discard superseded proofs and re-prove identity', async () => {
  const first = deferred(), second = deferred(), third = deferred()
  fetchMock.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise).mockReturnValueOnce(third.promise)
  const { result, rerender } = renderHook(props => useBootstrap({ drawingId: null, ...props }), {
    initialProps: { projectId: U(2), orgId: U(1) },
  })
  rerender({ projectId: U(5), orgId: U(1) })
  await act(async () => { first.resolve(json(identity)) })
  expect(result.current.session.status).toBe('checking')
  await act(async () => { second.resolve(json(identity)) })
  await waitFor(() => expect(result.current.session.status).toBe('active'))
  rerender({ projectId: U(5), orgId: U(6) })
  expect(result.current.session.status).toBe('checking')
  await act(async () => { third.resolve(json({ ...identity, tenant_id: U(6), org_id: U(6) })) })
  await waitFor(() => expect(result.current.solar.ready).toBe(true))
  expect(result.current.tenant).toBe(U(6))
  expect(result.current.solar.scope.binding.organization_id).toBe(U(6))
  expect(fetchMock).toHaveBeenCalledTimes(3)
})

it('identity 401 uses transport invalidation and keeps required latched on Retry', async () => {
  fetchMock.mockResolvedValueOnce(json({ detail: 'expired' }, 401))
    .mockImplementation(async () => json(identity))
  const { result, rerender } = renderHook(props => useBootstrap({ drawingId: null, ...props }), {
    initialProps: { retry: 0 },
  })
  await waitFor(() => expect(result.current.session.status).toBe('required'))
  expect(localStorage.getItem('leaf.jwt')).toBeNull()
  rerender({ retry: 1 })
  await act(async () => {})
  expect(result.current.session.status).toBe('required')
  expect(result.current.solar.scope.authorityKey).toBeNull()
})

it('mock mode makes no identity request and cannot clear a live refusal', async () => {
  const { result, rerender } = renderHook(props => useBootstrap({ drawingId: null, ...props }), {
    initialProps: { mock: true },
  })
  expect(fetchMock).not.toHaveBeenCalled()
  act(() => { result.current.session.actions.requireAuth('signout', { tokenInvalidated: true }) })
  rerender({ mock: true })
  expect(result.current.session.status).toBe('required')
  expect(result.current.solar.scope.authorityKey).toBeNull()
})
