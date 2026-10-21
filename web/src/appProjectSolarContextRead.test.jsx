import { useEffect, useState } from 'react'
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useProjectSolarScope } from './solar/useProjectSolarScope.js'
import { useAppProjectSolarRuntime } from './solar/useAppProjectSolarRuntime.js'
import { createSolarProjectClient } from './solar/solarProjectClient.js'
import { projectSolarAppReason } from './solar/solarProjectAppModel.js'

const U = n => `${n.toString().repeat(8)}-${n.toString().repeat(4)}-${n.toString().repeat(4)}-${n.toString().repeat(4)}-${n.toString().repeat(12)}`
const binding = { kind: 'project', organization_id: U(1), project_id: U(2), drawing_id: U(3), input_version_id: U(4) }
const context = { context: { organization_id: U(1), project_id: U(2), drawing_id: U(3),
  input_version_id: U(4), head_version_id: U(4), is_head: true, intake_sha256: 'a'.repeat(64) },
  intake: { dwg: 'canonical.dwg', layers: ['Panels'], polylines: [{ handle: 'CA', layer: 'Panels', pts: [[0, 0], [1, 1]] }] } }
const lease = { holder: 'same holder', fence: '1', acquired: '2026-01-01T00:00:00Z', expires: '2099-01-01T00:00:00Z' }
const json = data => ({ ok: true, status: 200, json: async () => data })
function store(snapshot) {
  const listeners = new Set()
  return { getSnapshot: () => snapshot,
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener) } }
}

afterEach(() => { cleanup(); vi.useRealTimers() })

function mount(freshWorkspace) {
  const requests = []
  const fetchImpl = vi.fn((url, options) => {
    if (url.includes('/context?')) {
      requests.push({ url, signal: options.signal })
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => resolve(json(context)), 300)
        options.signal.addEventListener('abort', () => {
          clearTimeout(timer)
          reject(new DOMException('Aborted', 'AbortError'))
        }, { once: true })
      })
    }
    if (url.endsWith('/checkout')) return Promise.resolve(json({ organization_id: U(1), project_id: U(2), drawing_id: U(3),
      checkout: lease, ...(options.method === 'POST' ? { acquired: true, checkout_capability: 'lco1.' + 'a'.repeat(64) } : {}) }))
    throw new Error(`Unexpected request: ${url}`)
  })
  const client = createSolarProjectClient({ fetchImpl })
  const createClient = () => client
  const workspace = store({ orgId: U(1), openProjectId: U(2), canonicalVersionId: U(4) })
  const session = store({ status: 'active', generation: 1 })
  const completedVersionRef = { current: null }
  const seen = []
  const hook = renderHook(() => {
    const [, rerender] = useState(0)
    useEffect(() => {
      const timer = setInterval(() => rerender(value => value + 1), 50)
      return () => clearInterval(timer)
    }, [])
    // App constructs these inputs each render. Its workspace adapter also returns
    // a new snapshot facade over the same controller methods on every render.
    const solar = useProjectSolarScope({ projectMode: true,
      runContext: { orgId: U(1), projectId: U(2), drawingArtifactId: U(3), drawingId: U(4), drawingVersion: null },
      session: { authenticated: true, actorId: 'hosted-pro-user', generation: 1 } }, { createClient })
    useEffect(() => {
      if (!solar.ready) return undefined
      const timer = setInterval(() => { void solar.projectClient.readCheckout() }, 100)
      return () => clearInterval(timer)
    }, [solar.ready, solar.projectClient])
    const runtime = useAppProjectSolarRuntime({ projectMode: true, projectSolar: solar,
      workspaceController: freshWorkspace ? { ...workspace.getSnapshot(), ...workspace } : workspace,
      sessionController: session, completedVersionRef, hostEnabled: true, externalBusy: false })
    seen.push({ runtime, solar })
    return { runtime, solar }
  })
  return { ...hook, requests, client, fetchImpl, seen }
}

it.each([false, true])('W1C8 completes a delayed canonical context through parent renders and client emits (fresh workspace: %s)', async freshWorkspace => {
  vi.useFakeTimers()
  const h = mount(freshWorkspace)
  const firstCapture = h.result.current.solar.isCurrent
  expect(h.result.current.runtime.contextView).toBeNull()
  for (let i = 0; i < 12; i++) await act(async () => { await vi.advanceTimersByTimeAsync(50) })
  expect(h.fetchImpl.mock.calls.filter(([url]) => url.endsWith('/checkout'))).toHaveLength(6)
  expect(h.result.current.solar.isCurrent).not.toBe(firstCapture)
  expect(h.requests.length).toBeGreaterThan(0)
  expect(h.requests.length).toBeLessThanOrEqual(2)
  expect(h.requests.some(request => !request.signal.aborted)).toBe(true)
  expect(h.requests[0].url).toBe(`/api/projects/${U(2)}/drawing-versions/${U(4)}/context?drawing_id=${U(3)}`)
  expect(h.result.current.solar.scope.binding).toEqual(binding)
  expect(h.result.current.runtime.contextView).toEqual(context)
  expect(h.result.current.runtime.contextCurrent()).toBe(true)
  expect(h.result.current.runtime.notice).toBeNull()
  expect(h.result.current.runtime.notice).not.toBe(projectSolarAppReason('scope_pending'))
  expect(h.seen.some(value => value.solar.ready && value.runtime.contextView === null)).toBe(true)
  h.unmount()
})

it('W1C8 uses the latest capture when checkout rotates during the same context read', async () => {
  vi.useFakeTimers()
  const h = mount(true)
  const previous = h.result.current.solar.isCurrent
  await act(async () => { await h.client.acquireCheckout({ holder: lease.holder }) })
  expect(previous()).toBe(false)
  expect(h.result.current.solar.isCurrent()).toBe(true)
  for (let i = 0; i < 8; i++) await act(async () => { await vi.advanceTimersByTimeAsync(50) })
  expect(h.requests.length).toBeLessThanOrEqual(2)
  expect(h.result.current.runtime.contextView).toEqual(context)
  expect(h.result.current.runtime.contextCurrent()).toBe(true)
  h.unmount()
})

it('W1C8 keeps a completed context visible after a checkout capture rotation without refetching', async () => {
  vi.useFakeTimers()
  const h = mount(true)
  for (let i = 0; i < 8; i++) await act(async () => { await vi.advanceTimersByTimeAsync(50) })
  expect(h.result.current.runtime.contextView).toEqual(context)
  const reads = h.requests.length
  const previous = h.result.current.solar.isCurrent
  await act(async () => { await h.client.acquireCheckout({ holder: lease.holder }) })
  expect(previous()).toBe(false)
  expect(h.requests).toHaveLength(reads)
  expect(h.result.current.runtime.contextView).toEqual(context)
  expect(h.result.current.runtime.contextCurrent()).toBe(true)
  h.unmount()
})
