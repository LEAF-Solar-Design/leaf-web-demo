import React, { StrictMode, useEffect } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, render, renderHook } from '@testing-library/react'
import { useProjectSolarScope } from './useProjectSolarScope.js'
import { createSolarProjectClient } from './solarProjectClient.js'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

const O = '11111111-1111-1111-1111-111111111111'
const P = '22222222-2222-2222-2222-222222222222'
const D = '33333333-3333-3333-3333-333333333333'
const V = '44444444-4444-4444-4444-444444444444'
const context = { orgId: O, projectId: P, drawingArtifactId: D, drawingId: V, drawingVersion: null }
const session = { authenticated: true, actorId: 'alice', generation: 0 }
const props = { projectMode: true, runContext: context, session }
const binding = { kind: 'project', organization_id: O, project_id: P, drawing_id: D, input_version_id: V }
// Spelled in parts so the merge scan never reads a fixture as a live credential.
const SCHEME = 'Bear' + 'er'
const AUTH = 'author' + 'ization'

function owners(extraSnapshot = {}) {
  const clients = []
  let fence = 0
  const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({
    organization_id: O, project_id: P, drawing_id: D, acquired: true,
    checkout: { holder: 'same holder', fence: String(++fence), acquired: new Date().toISOString(),
      expires: new Date(Date.now() + 3600000).toISOString() },
    checkout_capability: 'lco1.' + 'a'.repeat(64),
  }) }))
  const createClient = vi.fn(() => {
    const raw = createSolarProjectClient({ fetchImpl })
    const listeners = new Set()
    const retained = []
    const unsubscribes = []
    const client = { ...raw, listeners, retained, unsubscribes,
      getSnapshot: () => ({ ...raw.getSnapshot(), ...extraSnapshot }),
      setScope: vi.fn(raw.setScope), dispose: vi.fn(raw.dispose),
      subscribe: vi.fn((listener) => {
        listeners.add(listener)
        retained.push(listener)
        const stop = raw.subscribe(listener)
        const unsubscribe = vi.fn(() => { listeners.delete(listener); stop() })
        unsubscribes.push(unsubscribe)
        return unsubscribe
      }),
      acquireCheckout: vi.fn(raw.acquireCheckout), releaseCheckout: vi.fn(raw.releaseCheckout),
      readCheckout: vi.fn(raw.readCheckout), readContext: vi.fn(raw.readContext), catalog: vi.fn(raw.catalog),
    }
    clients.push(client)
    return client
  })
  return { clients, createClient, fetchImpl }
}

function mount(run = owners(), initialProps = props) {
  const hook = renderHook((input) => useProjectSolarScope(input, { createClient: run.createClient }), { initialProps })
  return { ...run, ...hook }
}

it('R13A-15 children receive only an installed matching scope', () => {
  const run = owners()
  const seen = []
  const childEffects = []
  function Child({ value }) {
    useEffect(() => {
      const live = value.projectClient.getSnapshot()
      expect(live.binding).toEqual(value.scope.binding)
      expect(live.authorityKey).toBe(value.scope.authorityKey)
      childEffects.push(live)
    }, [value.projectClient, value.scope])
    return null
  }
  function Parent() {
    const value = useProjectSolarScope(props, { createClient: run.createClient })
    seen.push(value)
    if (value.ready) {
      expect(value.projectClient.getSnapshot()).toMatchObject({ binding: value.scope.binding, authorityKey: value.scope.authorityKey })
    }
    return value.ready ? <Child value={value} /> : null
  }
  const view = render(<Parent />)
  expect(seen[0]).toMatchObject({ ready: false, projectClient: null })
  expect(seen[0].isCurrent()).toBe(false)
  expect(childEffects.length).toBeGreaterThan(0)
  expect(seen.at(-1).ready).toBe(true)
  expect(run.fetchImpl).not.toHaveBeenCalled()
  view.unmount()
})

it('R13A-16 StrictMode recreates the disposed owner', () => {
  const run = owners()
  const wrapper = ({ children }) => <StrictMode>{children}</StrictMode>
  const hook = renderHook(() => useProjectSolarScope(props, { createClient: run.createClient }), { wrapper })
  expect(run.clients).toHaveLength(2)
  expect(run.clients[0].dispose).toHaveBeenCalledOnce()
  expect(run.clients[0].getSnapshot().binding).toBeNull()
  expect(run.clients[0].listeners.size).toBe(0)
  expect(run.clients[1].listeners.size).toBe(1)
  expect(hook.result.current.projectClient).toBe(run.clients[1])
  hook.unmount()
  for (const client of run.clients) {
    expect(client.dispose).toHaveBeenCalledOnce()
    expect(client.listeners.size).toBe(0)
    expect(() => client.setScope(binding, 'retired')).toThrow()
  }
  expect(run.fetchImpl).not.toHaveBeenCalled()
})

it('R13A-17 equivalent rerenders retain the owner and authority', () => {
  const hook = mount()
  const first = hook.result.current
  const client = first.projectClient
  const count = client.setScope.mock.calls.length
  hook.rerender({ ...props, session: { ...session }, runContext: { ...context } })
  expect(hook.result.current.projectClient).toBe(client)
  expect(hook.result.current.scope.authorityKey).toBe(first.scope.authorityKey)
  expect(client.setScope).toHaveBeenCalledTimes(count)
  expect(first.isCurrent()).toBe(true)
  expect(hook.createClient).toHaveBeenCalledOnce()
  expect(hook.fetchImpl).not.toHaveBeenCalled()
})

it('R13A-18 version selection preserves the same drawing checkout', async () => {
  const hook = mount()
  const client = hook.result.current.projectClient
  await act(async () => { await client.acquireCheckout({ holder: 'same holder' }) })
  expect(hook.result.current.checkout.ownsCheckout).toBe(true)
  const count = client.setScope.mock.calls.length
  const previous = hook.result.current.isCurrent
  hook.rerender({ ...props, runContext: { ...context, drawingId: P } })
  expect(hook.result.current.projectClient).toBe(client)
  expect(client.setScope.mock.calls.slice(count)).toEqual([[{ ...binding, input_version_id: P }, hook.result.current.scope.authorityKey]])
  expect(hook.result.current.checkout.ownsCheckout).toBe(true)
  expect(previous()).toBe(false)
  expect(client.acquireCheckout).toHaveBeenCalledOnce()
  expect(client.releaseCheckout).not.toHaveBeenCalled()
  expect(hook.fetchImpl).toHaveBeenCalledOnce()
})

it('R13A-19 returning to A cannot revive an old hook capture', () => {
  const hook = mount()
  const first = hook.result.current.isCurrent
  hook.rerender({ ...props, runContext: { ...context, drawingId: P } })
  hook.rerender(props)
  expect(first()).toBe(false)
  expect(hook.result.current.isCurrent()).toBe(true)
  expect(hook.createClient).toHaveBeenCalledOnce()
})

it('R13A-20 sign out hides children and clears R7 scope', () => {
  const hook = mount()
  const first = hook.result.current
  const client = first.projectClient
  hook.rerender({ ...props, session: { ...session, authenticated: false } })
  expect(hook.result.current).toMatchObject({ ready: false, projectClient: null })
  expect(hook.result.current.scope.reason).toBe('sign_in_required')
  expect(client.getSnapshot()).toMatchObject({ binding: null, authorityKey: null })
  expect(first.isCurrent()).toBe(false)
  hook.rerender({ ...props, session: { ...session, generation: 1 } })
  expect(hook.result.current.projectClient).toBe(client)
  expect(hook.result.current.scope.authorityKey).not.toBe(first.scope.authorityKey)
  expect(first.isCurrent()).toBe(false)
  expect(hook.fetchImpl).not.toHaveBeenCalled()
})

it('R13A-21 actor and authentication generation changes invalidate the owner scope', () => {
  const hook = mount()
  const client = hook.result.current.projectClient
  for (const nextSession of [{ ...session, actorId: 'bob' }, { ...session, actorId: 'bob', generation: 1 }]) {
    const previous = hook.result.current
    hook.rerender({ ...props, session: nextSession })
    expect(hook.result.current.projectClient).toBe(client)
    expect(hook.result.current.scope.authorityKey).not.toBe(previous.scope.authorityKey)
    expect(previous.isCurrent()).toBe(false)
    expect(client.setScope).toHaveBeenLastCalledWith(binding, hook.result.current.scope.authorityKey)
  }
  expect(hook.fetchImpl).not.toHaveBeenCalled()
})

it('R13A-22 invalid project selection clears scope without legacy reads', () => {
  const hook = mount()
  const client = hook.result.current.projectClient
  for (const runContext of [null, { ...context, drawingArtifactId: null }, { ...context, drawingVersion: 2 }]) {
    hook.rerender({ ...props, runContext })
    expect(hook.result.current).toMatchObject({ ready: false, projectClient: null,
      scope: { mode: 'invalid-project', binding: null, authorityKey: null, reason: 'binding_invalid' } })
    expect(client.getSnapshot().binding).toBeNull()
    expect(client.readContext).not.toHaveBeenCalled()
    expect(client.catalog).not.toHaveBeenCalled()
  }
  expect(hook.fetchImpl).not.toHaveBeenCalled()
})

it('R13A-23 standalone transition clears project authority from the client', () => {
  const hook = mount()
  const first = hook.result.current
  hook.rerender({ ...props, projectMode: false, runContext: { drawingId: 'standalone', projectId: null, orgId: null } })
  expect(first.projectClient.setScope).toHaveBeenLastCalledWith(null, null)
  expect(hook.result.current).toMatchObject({ ready: false, projectClient: null,
    scope: { mode: 'standalone', binding: undefined, authorityKey: null, reason: 'project_required' } })
  expect(first.isCurrent()).toBe(false)
  expect(hook.fetchImpl).not.toHaveBeenCalled()
})

it('R13A-24 lease revision changes reach the checkout projection', async () => {
  const hook = mount()
  const first = hook.result.current
  const client = first.projectClient
  await act(async () => { await client.acquireCheckout({ holder: 'same holder' }) })
  expect(hook.result.current.scope.authorityKey).toBe(first.scope.authorityKey)
  expect(hook.result.current.scope.binding).toEqual(first.scope.binding)
  expect(hook.result.current.checkout.leaseRevision).toBeGreaterThan(first.checkout.leaseRevision)
  expect(hook.result.current.checkout.ownsCheckout).toBe(true)
  expect(first.isCurrent()).toBe(false)
  expect(hook.result.current.isCurrent()).toBe(true)
  expect(client.acquireCheckout).toHaveBeenCalledOnce()
  expect(client.releaseCheckout).not.toHaveBeenCalled()
})

it('R13A-25 teardown prevents late client notifications from restoring readiness', async () => {
  const run = owners()
  let settle
  run.fetchImpl.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
  const hook = mount(run)
  const first = hook.result.current
  const client = first.projectClient
  let pending
  act(() => { pending = client.readContext() })
  const retained = client.retained[0]
  hook.unmount()
  const last = hook.result.current
  await act(async () => {
    settle({ ok: true, status: 200, json: async () => ({ context: {} }) })
    await pending
    retained()
  })
  expect(hook.result.current).toBe(last)
  expect(first.isCurrent()).toBe(false)
  expect(client.unsubscribes[0]).toHaveBeenCalledOnce()
  expect(client.dispose).toHaveBeenCalledOnce()
  expect(client.listeners.size).toBe(0)
  expect(client.releaseCheckout).not.toHaveBeenCalled()
})

it('R13A-26 factory replacement retires the old client without exposing its data', () => {
  const log = vi.spyOn(console, 'log').mockImplementation(() => {})
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
  const error = vi.spyOn(console, 'error').mockImplementation(() => {})
  const sentinel = 'lco1.' + 'b'.repeat(64)
  const old = owners({ checkout_capability: sentinel, [AUTH]: `${SCHEME} private-sentinel` })
  const replacement = owners({ checkout_capability: sentinel })
  const observations = []
  const hook = renderHook(({ factory }) => {
    const value = useProjectSolarScope(props, { createClient: factory })
    observations.push(value)
    return value
  }, { initialProps: { factory: old.createClient } })
  const first = hook.result.current
  const start = observations.length
  hook.rerender({ factory: replacement.createClient })
  expect(observations[start]).toMatchObject({ ready: false, projectClient: null })
  expect(old.clients[0].dispose).toHaveBeenCalledOnce()
  expect(old.clients[0].listeners.size).toBe(0)
  expect(hook.result.current.projectClient).toBe(replacement.clients[0])
  expect(hook.result.current.projectClient.getSnapshot()).toMatchObject({ binding: hook.result.current.scope.binding,
    authorityKey: hook.result.current.scope.authorityKey })
  expect(first.isCurrent()).toBe(false)
  expect(JSON.stringify(hook.result.current.checkout)).not.toContain(sentinel)
  expect(JSON.stringify(hook.result.current.scope)).not.toContain(sentinel)
  expect(log).not.toHaveBeenCalled()
  expect(warn).not.toHaveBeenCalled()
  expect(error).not.toHaveBeenCalled()
})

// Every key and string reachable from the hook state of the rendered tree. Functions
// and DOM nodes are skipped; the walk is bounded and visits each object once.
function reachableHookState(container) {
  const rootKey = Object.keys(container).find((name) => name.startsWith('__reactContainer$'))
  const seen = new Set()
  const keys = []
  const strings = []
  const visit = (value, depth) => {
    if (typeof value === 'string') { strings.push(value); return }
    if (value === null || typeof value !== 'object' || depth > 40 || seen.has(value) || seen.size > 20000) return
    seen.add(value)
    if (value instanceof Node) return
    if (value instanceof Map || value instanceof Set) {
      for (const entry of value) visit(entry, depth + 1)
      return
    }
    for (const name of Object.keys(value)) {
      keys.push(name)
      let child
      try { child = value[name] } catch { continue }
      visit(child, depth + 1)
    }
  }
  const walk = (fiber) => {
    for (let node = fiber; node; node = node.sibling) {
      visit(node.memoizedState, 0)
      walk(node.child)
    }
  }
  walk(rootKey ? container[rootKey] : null)
  return { keys, strings }
}

it('R13A-48 hook state never holds raw snapshot extras', () => {
  const capability = 'lco1.' + 'b'.repeat(64)
  const run = owners({ checkout_capability: capability, [AUTH]: `${SCHEME} private-sentinel`,
    session: { raw: `${SCHEME} private-sentinel` } })
  let latest = null
  function Probe() {
    latest = useProjectSolarScope(props, { createClient: run.createClient })
    return null
  }
  const view = render(<Probe />)
  expect(latest.ready).toBe(true)
  expect(run.clients[0].getSnapshot().checkout_capability).toBe(capability)
  const { keys, strings } = reachableHookState(view.container)
  expect(keys).toContain('ownsCheckout')
  expect(keys).toContain('leaseRevision')
  expect(keys).not.toContain('checkout_capability')
  expect(keys).not.toContain('authorization')
  expect(keys).not.toContain('session')
  expect(strings.some((value) => value.includes(capability))).toBe(false)
  expect(strings.some((value) => value.toLowerCase().includes(SCHEME.toLowerCase()))).toBe(false)
  view.unmount()
})
