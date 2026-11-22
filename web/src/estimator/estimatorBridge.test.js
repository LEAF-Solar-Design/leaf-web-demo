import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'

const authState = vi.hoisted(() => ({ guest: false, authority: 'human-a' }))
vi.mock('../controllers/session/consoleGate.js', () => ({ consoleKnownGuest: () => authState.guest }))
vi.mock('../auth.js', async original => ({ ...await original(),
  subscribeBeforeLogout: vi.fn(() => () => {}), subscribeTokenStored: vi.fn(() => () => {}),
}))
vi.mock('../api.js', async original => ({ ...await original(), subscribeUnauthorized: vi.fn(() => () => {}) }))
vi.mock('../lib/serviceAuthSession.js', async original => ({ ...await original(), authorityKey: () => authState.authority }))

import { createEstimatorBridge } from './estimatorBridge.js'
import { ESTIMATOR_ORIGINS, estimatorOriginAllowed } from './estimatorOrigins.js'
import { fetchEstimatorStatus, mintEstimatorHandoff } from './estimatorRoute.js'
import * as routes from './estimatorRoute.js'
import { useEstimatorSurfaceStatus, _resetEstimatorSurfaceStatusForTests } from './useEstimatorSurfaceStatus.js'
import { subscribeBeforeLogout, subscribeTokenStored } from '../auth.js'
import { subscribeUnauthorized } from '../api.js'
import { surfaceRailState } from '../site/productSurfaces.js'

const origin = 'https://estimator-staging.leafdesign.ai'
const chan = 'a'.repeat(43)
const subh = 'sh_' + 'b'.repeat(32)
const token = 'a'.repeat(40) + '.' + 'b'.repeat(200) + '.' + 'c'.repeat(342)
const result = { token, estimator_origin: origin, subh, expires_in: 60 }
const bridges = []
beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(1800000000000)
  authState.guest = false; authState.authority = 'human-a'
  for (const subscribe of [subscribeBeforeLogout, subscribeTokenStored, subscribeUnauthorized]) subscribe.mockClear()
})
afterEach(() => {
  cleanup(); _resetEstimatorSurfaceStatusForTests()
  for (const bridge of bridges.splice(0)) bridge.dispose()
  vi.useRealTimers(); vi.restoreAllMocks()
})

function harness(mint = vi.fn().mockResolvedValue(result)) {
  const peer = { postMessage: vi.fn() }
  const win = new EventTarget()
  win.location = { origin: 'https://platform-staging.leafdesign.ai' }
  const onState = vi.fn(), onRecreate = vi.fn()
  const bridge = createEstimatorBridge({ frame: { contentWindow: peer }, origin, win, mint, onState, onRecreate })
  bridges.push(bridge)
  const message = (data = {}, overrides = {}) => win.dispatchEvent(new MessageEvent('message', {
    origin, source: peer, data: { v: 1, type: 'estimator.ready', chan, seq: 1, ts: Date.now(), ...data }, ...overrides,
  }))
  return { bridge, peer, message, mint, onState, onRecreate }
}
const flush = async () => { await Promise.resolve(); await Promise.resolve() }

it('pins the identical frozen server/web origin map and refuses production and origin tricks', () => {
  expect(ESTIMATOR_ORIGINS).toEqual({
    'https://estimator-staging.leafdesign.ai': ['studio-staging', 'staging'],
    'https://estimator-studio-staging-973968415611.us-central1.run.app': ['studio-staging', 'staging'],
  })
  expect(Object.isFrozen(ESTIMATOR_ORIGINS)).toBe(true)
  for (const value of Object.values(ESTIMATOR_ORIGINS)) expect(Object.isFrozen(value)).toBe(true)
  expect(estimatorOriginAllowed(origin, 'https://platform.leafdesign.ai')).toBe(false)
  expect(estimatorOriginAllowed('http://localhost:8080', 'http://localhost:3000')).toBe(true)
  for (const value of ['http://localhost:8080', origin + '/', origin + '.evil.test', 'http://localhost:65536']) {
    expect(estimatorOriginAllowed(value, 'https://platform-staging.leafdesign.ai')).toBe(false)
  }
  expect(estimatorOriginAllowed('http://localhost:8080\n', 'http://localhost:3000')).toBe(false)
})

describe('message boundary', () => {
  it.each([
    [{}, { origin: 'https://evil.test' }], [{}, { source: null }],
    [{ ts: 1800000000000 - 30001 }, {}], [{ ts: 1800000000000 + 30001 }, {}],
    [{ seq: true }, {}], [{ v: 2 }, {}], [{ chan: chan + '\n' }, {}],
    [{ extra: 'x'.repeat(9000) }, {}], [{ type: 'estimator.need_token' }, {}],
  ])('drops an invalid message without minting %#', async (data, overrides) => {
    const h = harness()
    h.message(data, overrides)
    await flush()
    expect(h.mint).not.toHaveBeenCalled()
    expect(h.peer.postMessage).not.toHaveBeenCalled()
    expect(h.bridge.dropped()).toBe(1)
  })

  it('posts exactly one token only to the peer and rejects non-increasing sequence', async () => {
    const h = harness()
    h.message()
    await flush()
    expect(h.mint).toHaveBeenCalledExactlyOnceWith(chan, expect.any(AbortSignal))
    expect(h.peer.postMessage).toHaveBeenCalledExactlyOnceWith({ v: 1, type: 'studio.token', chan,
      seq: 1, ts: Date.now(), token }, origin)
    h.message()
    h.message({ type: 'estimator.session', state: 'active', subh, seq: 1 })
    h.message({ type: 'estimator.session', state: 'active', subh, chan: 'z'.repeat(43), seq: 2 })
    expect(h.bridge.dropped()).toBe(3)
    expect(h.onRecreate).not.toHaveBeenCalled()
    h.message({ type: 'estimator.session', state: 'active', subh, seq: 2 })
    expect(h.onState).toHaveBeenCalledWith('ready')
    expect(h.peer.postMessage).toHaveBeenCalledTimes(1)
  })

  it.each([chan, 'z'.repeat(43)])('a second ready can only recreate, even with a fresh channel %s', async nextChan => {
    const h = harness()
    h.message(); await flush()
    h.message({ chan: nextChan, seq: 2 }); await flush()
    expect(h.onRecreate).toHaveBeenCalledOnce()
    expect(h.mint).toHaveBeenCalledOnce()
    expect(h.peer.postMessage).toHaveBeenCalledOnce()
  })

  it('a second ready during mint cancels the pending token', async () => {
    let resolve
    const h = harness(vi.fn(() => new Promise(done => { resolve = done })))
    h.message()
    h.message({ seq: 2 })
    resolve(result); await flush()
    expect(h.onRecreate).toHaveBeenCalledOnce()
    expect(h.peer.postMessage).not.toHaveBeenCalled()
    expect(h.mint.mock.calls[0][1].aborted).toBe(true)
  })

  it('ready at or after 15 seconds cannot mint', async () => {
    const h = harness()
    await vi.advanceTimersByTimeAsync(15000)
    h.message(); await flush()
    expect(h.mint).not.toHaveBeenCalled()
    expect(h.onState).toHaveBeenCalledWith('failed')
  })

  it.each(['failed', 'ended', 'active'])('session %s or subject mismatch recreates', async state => {
    const h = harness()
    h.message(); await flush()
    h.message({ type: 'estimator.session', state, seq: 2, subh: 'sh_' + 'c'.repeat(32) })
    expect(h.onRecreate).toHaveBeenCalledOnce()
  })

  it('end waits for an authenticated acknowledgement and suppresses a late mint', async () => {
    let resolve
    const h = harness(vi.fn(() => new Promise(done => { resolve = done })))
    h.message()
    const settled = vi.fn()
    const ending = h.bridge.end().then(settled)
    expect(h.bridge.end()).toBe(h.bridge.end())
    expect(h.peer.postMessage).toHaveBeenCalledWith(expect.objectContaining({ type: 'studio.end', chan }), origin)
    resolve(result); await flush()
    expect(h.peer.postMessage.mock.calls.some(([message]) => message.type === 'studio.token')).toBe(false)
    h.message({ type: 'estimator.session', state: 'ended', seq: 2 }, { origin: 'https://evil.test' })
    await flush(); expect(settled).not.toHaveBeenCalled()
    h.message({ type: 'estimator.session', state: 'ended', seq: 2 })
    await ending
    expect(settled).toHaveBeenCalledOnce()
  })

  it('end releases at 1500 ms if the peer never acknowledges', async () => {
    const h = harness()
    h.message(); await flush()
    const settled = vi.fn()
    h.bridge.end().then(settled)
    await vi.advanceTimersByTimeAsync(1499)
    expect(settled).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(settled).toHaveBeenCalledOnce()
  })

  it.each([403, 404, 429, 503])('mint status %s remains a bounded UI failure', async status => {
    const h = harness(vi.fn().mockRejectedValue(Object.assign(new Error('private'), { status })))
    h.message(); await flush()
    expect(h.onState).toHaveBeenCalledWith(status === 403 || status === 404 ? 'denied' : status === 429 ? 'throttled' : 'failed')
    expect(h.peer.postMessage).not.toHaveBeenCalled()
  })
})

it('mint transports only chan in JSON and never adds secrets to URLs or storage', async () => {
  const fetchImpl = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => result })
  const storage = vi.spyOn(Storage.prototype, 'setItem')
  const history = vi.spyOn(window.history, 'replaceState')
  const projectId = '22222222-2222-4222-8222-222222222222'
  expect(await mintEstimatorHandoff(projectId, chan, { fetchImpl })).toEqual(result)
  const [url, init] = fetchImpl.mock.calls[0]
  expect(url).toMatch(new RegExp(`/api/projects/${projectId}/estimator/handoff$`))
  expect(url).not.toContain(chan)
  expect(init.body).toBe(JSON.stringify({ chan }))
  expect(init.method).toBe('POST')
  expect(storage).not.toHaveBeenCalled()
  expect(history).not.toHaveBeenCalled()
})

it.each([401, 403, 404, 503])('status %s fails closed', async code => {
  const status = await fetchEstimatorStatus({ fetchImpl: vi.fn().mockResolvedValue({ status: code, ok: false }) })
  expect(status.state).toBe(code === 404 ? 'off' : code === 503 ? 'unavailable' : 'signed-out')
  expect(status.bindings).toEqual([])
})

it('one status probe serves multiple consumers and known guests make no request', async () => {
  const fetcher = vi.spyOn(routes, 'fetchEstimatorStatus').mockResolvedValue({ state: 'on', bindings: [{ project_id: 'bound' }] })
  authState.guest = true
  const guest = renderHook(() => useEstimatorSurfaceStatus(false))
  expect(guest.result.current.state).toBe('signed-out')
  expect(fetcher).not.toHaveBeenCalled()
  guest.unmount()
  _resetEstimatorSurfaceStatusForTests()
  authState.guest = false
  renderHook(() => useEstimatorSurfaceStatus(false))
  const second = renderHook(() => useEstimatorSurfaceStatus(false))
  await act(async () => {})
  expect(fetcher).toHaveBeenCalledOnce()
  expect(second.result.current.state).toBe('on')
  expect(surfaceRailState('estimator', null, 'app')).toBe(true)
})

it.each([subscribeBeforeLogout, subscribeUnauthorized])('sign-out fences a pending status probe: %s', async subscribe => {
  let complete
  const fetcher = vi.spyOn(routes, 'fetchEstimatorStatus')
    .mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
    .mockResolvedValue({ state: 'on', bindings: [{ project_id: 'bound' }] })
  const hook = renderHook(() => useEstimatorSurfaceStatus(false))
  act(() => { subscribe.mock.calls[0][0]() })
  await act(async () => { complete({ state: 'on', bindings: [{ project_id: 'bound' }] }) })
  expect(hook.result.current.state).toBe('signed-out')
  expect(surfaceRailState('estimator', null, 'app')).toBe(false)
  act(() => { authState.authority = 'human-b'; subscribeTokenStored.mock.calls[0][0]() })
  await act(async () => {})
  expect(hook.result.current.state).toBe('on')
  expect(fetcher).toHaveBeenCalledTimes(2)
  act(() => {
    authState.authority = null
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' }))
  })
  expect(hook.result.current.state).toBe('signed-out')
})
