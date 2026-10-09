import { StrictMode, Suspense, startTransition, useLayoutEffect, useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, render, renderHook, waitFor } from '@testing-library/react'

vi.mock('./api.js', () => ({
  cloneProject: vi.fn(),
  deleteProject: vi.fn(),
  exportProject: vi.fn(),
  getOrgIdentities: vi.fn(),
  getProjectLifecycle: vi.fn(),
  inviteMember: vi.fn(),
  resetProject: vi.fn(),
  revokeMember: vi.fn(),
  setIdentityDisplayName: vi.fn(),
}))

import { getProjectLifecycle, inviteMember, setIdentityDisplayName } from './api.js'
import useProjectLifecycle from './useProjectLifecycle.js'

function snapshot(projectId) {
  return {
    project: { project_id: projectId, org_id: `org-${projectId}` },
    members: [{ membership_id: `m-${projectId}`, binding_id: `b-${projectId}`, role: 'owner' }],
    receipts: [], files: [],
    viewer: { membership_id: `m-${projectId}`, binding_id: `b-${projectId}`, role: 'owner', can_invite: true },
  }
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function lifecycleCalls(projectId) {
  return getProjectLifecycle.mock.calls.filter(([id]) => id === projectId).length
}

// Every pending microtask has run once a macrotask has: use it where a row
// must show that something did NOT happen yet.
function settle() {
  return new Promise((resolve) => { setTimeout(resolve, 0) })
}

const NEVER = new Promise(() => {})

// A consumer whose next render can be ABANDONED: it calls the hook and then
// suspends, so a transition into it never commits and the committed view stays.
function suspendable() {
  const seen = { current: null }
  let setProps
  function Probe() {
    const [props, set] = useState({ projectId: 'A', enabled: true, suspend: false })
    setProps = set
    const value = useProjectLifecycle(props.projectId, { enabled: props.enabled })
    if (props.suspend) throw NEVER
    seen.current = value
    return null
  }
  render(<Suspense fallback={null}><Probe /></Suspense>)
  return {
    seen,
    commit: (next) => act(() => { setProps({ suspend: false, ...next }) }),
    abandon: (next) => act(() => { startTransition(() => { setProps({ suspend: true, ...next }) }) }),
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  getProjectLifecycle.mockImplementation(async (projectId) => snapshot(projectId))
})

afterEach(cleanup)

async function mounted(props = { projectId: 'A' }, options = {}) {
  const rendered = renderHook(
    ({ projectId, enabled = true }) => useProjectLifecycle(projectId, { enabled }),
    { initialProps: props, ...options },
  )
  await waitFor(() => expect(rendered.result.current.status).toBe('ready'))
  return rendered
}

it('LCR1 a mutation resolving after unmount refetches nothing and still returns its result', async () => {
  const invite = deferred()
  inviteMember.mockReturnValue(invite.promise)
  const { result, unmount } = await mounted()
  expect(lifecycleCalls('A')).toBe(1)
  let pending
  act(() => { pending = result.current.actions.invite('b-new', 'editor') })
  unmount()
  await act(async () => { invite.resolve({ ok: true, membership_id: 'm-new' }) })
  await expect(pending).resolves.toEqual({ ok: true, membership_id: 'm-new' })
  expect(lifecycleCalls('A')).toBe(1)
})

it('LCR2 a mutation started for A that resolves after the hook moved to B neither refetches A nor discards B', async () => {
  const invite = deferred()
  inviteMember.mockReturnValue(invite.promise)
  const { result, rerender } = await mounted()
  let pending
  act(() => { pending = result.current.actions.invite('b-new', 'editor') })
  const slowB = deferred()
  getProjectLifecycle.mockImplementation((projectId) => (projectId === 'B' ? slowB.promise : Promise.resolve(snapshot(projectId))))
  rerender({ projectId: 'B' })
  await act(async () => { invite.resolve({ ok: true }) ; await pending })
  await act(async () => { slowB.resolve(snapshot('B')) })
  await waitFor(() => expect(result.current.status).toBe('ready'))
  expect(lifecycleCalls('A')).toBe(1)
  expect(result.current.project?.project_id).toBe('B')
  expect(result.current.members.map((m) => m.member_id)).toEqual(['m-B'])
})

it('LCR3 a mutation resolving after the hook was disabled refetches nothing and leaves it empty', async () => {
  const invite = deferred()
  inviteMember.mockReturnValue(invite.promise)
  const { result, rerender } = await mounted()
  let pending
  act(() => { pending = result.current.actions.invite('b-new', 'editor') })
  rerender({ projectId: 'A', enabled: false })
  await waitFor(() => expect(result.current.status).toBe('idle'))
  await act(async () => { invite.resolve({ ok: true }); await pending })
  expect(lifecycleCalls('A')).toBe(1)
  expect(result.current.status).toBe('idle')
  expect(result.current.project).toBeNull()
  expect(result.current.refreshing).toBe(false)
})

it('LCR4 a label save resolving after unmount refetches nothing', async () => {
  const save = deferred()
  setIdentityDisplayName.mockReturnValue(save.promise)
  const { result, unmount } = await mounted()
  let pending
  act(() => { pending = result.current.actions.setLabel('m-A', 'Owner A') })
  unmount()
  await act(async () => { save.resolve({ identity: { binding_id: 'b-A', label: 'Owner A' } }) })
  await expect(pending).resolves.toEqual({ identity: { binding_id: 'b-A', label: 'Owner A' } })
  expect(lifecycleCalls('A')).toBe(1)
})

it('LCR5 a mutation resolving while mounted on the same project stays unsettled until its refresh resolves', async () => {
  const invite = deferred()
  inviteMember.mockReturnValue(invite.promise)
  const { result } = await mounted()
  const refresh = deferred()
  getProjectLifecycle.mockImplementation(() => refresh.promise)
  let pending
  act(() => { pending = result.current.actions.invite('b-new', 'editor') })
  let settled = false
  const tracked = pending.then((value) => { settled = true; return value })
  await act(async () => { invite.resolve({ ok: true }); await settle() })
  expect(lifecycleCalls('A')).toBe(2)
  expect(settled).toBe(false)
  expect(result.current.refreshing).toBe(true)
  await act(async () => { refresh.resolve(snapshot('A')); await tracked })
  expect(settled).toBe(true)
  expect(lifecycleCalls('A')).toBe(2)
  expect(result.current.refreshing).toBe(false)
  expect(result.current.status).toBe('ready')
})

it('LCR6 under StrictMode a mounted hook still refetches after a mutation', async () => {
  const invite = deferred()
  inviteMember.mockReturnValue(invite.promise)
  const { result } = await mounted({ projectId: 'A' }, { wrapper: StrictMode })
  const before = lifecycleCalls('A')
  let pending
  act(() => { pending = result.current.actions.invite('b-new', 'editor') })
  await act(async () => { invite.resolve({ ok: true }); await pending })
  expect(lifecycleCalls('A')).toBe(before + 1)
})

it('LCR7 a rejected mutation rethrows and refetches nothing', async () => {
  inviteMember.mockRejectedValue(new Error('denied'))
  const { result } = await mounted()
  await act(async () => {
    await expect(result.current.actions.invite('b-new', 'editor')).rejects.toThrow('denied')
  })
  expect(lifecycleCalls('A')).toBe(1)
})

it('LCR8 a label save resolving while mounted on the same project refetches once', async () => {
  setIdentityDisplayName.mockResolvedValue({ identity: { binding_id: 'b-A', label: 'Owner A' } })
  const { result } = await mounted()
  await act(async () => { await result.current.actions.setLabel('m-A', 'Owner A') })
  expect(lifecycleCalls('A')).toBe(2)
})

it('LCR9 a disable render React abandons does not suppress the refresh the committed hook owes', async () => {
  const invite = deferred()
  inviteMember.mockReturnValue(invite.promise)
  const { seen, abandon } = suspendable()
  await waitFor(() => expect(seen.current.status).toBe('ready'))
  expect(lifecycleCalls('A')).toBe(1)
  let pending
  act(() => { pending = seen.current.actions.invite('b-new', 'editor') })
  await abandon({ projectId: 'A', enabled: false })
  expect(seen.current.status).toBe('ready')
  expect(seen.current.project?.project_id).toBe('A')
  await act(async () => { invite.resolve({ ok: true }); await pending })
  expect(lifecycleCalls('A')).toBe(2)
  expect(seen.current.status).toBe('ready')
})

it('LCR10 an enable render React abandons does not let a disabled hook refetch', async () => {
  const invite = deferred()
  inviteMember.mockReturnValue(invite.promise)
  const { seen, commit, abandon } = suspendable()
  await waitFor(() => expect(seen.current.status).toBe('ready'))
  let pending
  act(() => { pending = seen.current.actions.invite('b-new', 'editor') })
  await commit({ projectId: 'A', enabled: false })
  await waitFor(() => expect(seen.current.status).toBe('idle'))
  await abandon({ projectId: 'A', enabled: true })
  expect(seen.current.status).toBe('idle')
  await act(async () => { invite.resolve({ ok: true }); await pending; await settle() })
  expect(lifecycleCalls('A')).toBe(1)
  expect(seen.current.status).toBe('idle')
  expect(seen.current.project).toBeNull()
})

it('LCR11 a project-change render React abandons does not suppress the refresh the committed hook owes', async () => {
  const invite = deferred()
  inviteMember.mockReturnValue(invite.promise)
  const { seen, abandon } = suspendable()
  await waitFor(() => expect(seen.current.status).toBe('ready'))
  expect(lifecycleCalls('A')).toBe(1)
  let pending
  act(() => { pending = seen.current.actions.invite('b-new', 'editor') })
  await abandon({ projectId: 'B', enabled: true })
  expect(seen.current.project?.project_id).toBe('A')
  await act(async () => { invite.resolve({ ok: true }); await pending })
  expect(lifecycleCalls('A')).toBe(2)
  expect(lifecycleCalls('B')).toBe(0)
  expect(seen.current.project?.project_id).toBe('A')
})

it('LCR12 a label save resolving while mounted on the same project stays unsettled until its refresh resolves', async () => {
  const save = deferred()
  setIdentityDisplayName.mockReturnValue(save.promise)
  const { result } = await mounted()
  const refresh = deferred()
  getProjectLifecycle.mockImplementation(() => refresh.promise)
  let pending
  act(() => { pending = result.current.actions.setLabel('m-A', 'Owner A') })
  let settled = false
  const tracked = pending.then((value) => { settled = true; return value })
  await act(async () => { save.resolve({ identity: { binding_id: 'b-A', label: 'Owner A' } }); await settle() })
  expect(lifecycleCalls('A')).toBe(2)
  expect(settled).toBe(false)
  expect(result.current.refreshing).toBe(true)
  await act(async () => { refresh.resolve(snapshot('A')); await tracked })
  expect(settled).toBe(true)
  expect(lifecycleCalls('A')).toBe(2)
  expect(result.current.refreshing).toBe(false)
})

it('LCR13 a mutation resolving inside the commit that removes the hook refetches nothing', async () => {
  const invite = deferred()
  inviteMember.mockReturnValue(invite.promise)
  const seen = { current: null }
  const order = []
  let setShown
  function Consumer() {
    seen.current = useProjectLifecycle('A')
    return null
  }
  // The parent outlives the consumer. Its layout effect runs inside the commit
  // that removes the consumer: after the consumer's layout cleanup and before
  // its passive cleanup, which React leaves to a later task.
  function Parent() {
    const [shown, set] = useState(true)
    setShown = set
    useLayoutEffect(() => {
      if (shown) return
      order.push('removed')
      invite.resolve({ ok: true })
      // Hold the thread past the scheduler's 5 ms slice, so React yields to the
      // settled mutation before it runs the passive cleanups.
      const until = performance.now() + 20
      while (performance.now() < until) { /* busy */ }
    }, [shown])
    return shown ? <Consumer /> : null
  }
  render(<Parent />)
  await waitFor(() => expect(seen.current.status).toBe('ready'))
  expect(lifecycleCalls('A')).toBe(1)
  let pending
  act(() => { pending = seen.current.actions.invite('b-new', 'editor') })
  const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT
  globalThis.IS_REACT_ACT_ENVIRONMENT = false
  try {
    // Outside act(), so React's own scheduler runs the commit and, in a later
    // task, the passive cleanups: the order a non-urgent update has in a browser.
    startTransition(() => { setShown(false) })
    await expect(pending).resolves.toEqual({ ok: true })
    await new Promise((resolve) => { setTimeout(resolve, 50) })
  } finally {
    globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment
  }
  expect(order).toEqual(['removed'])
  expect(lifecycleCalls('A')).toBe(1)
})
