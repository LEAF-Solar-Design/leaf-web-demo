import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({ status: null, listeners: new Set(), authority: 'human-a' }))
vi.mock('./useEstimatorSurfaceStatus.js', () => ({ useEstimatorSurfaceStatus: () => state.status }))
vi.mock('./estimatorRoute.js', () => ({ mintEstimatorHandoff: vi.fn() }))
vi.mock('../auth.js', () => ({ subscribeBeforeLogout: listener => {
  state.listeners.add(listener); return () => state.listeners.delete(listener)
} }))
vi.mock('../lib/serviceAuthSession.js', () => ({ authorityKey: () => state.authority }))
vi.mock('../site/groundWindow.js', () => ({ NO_OCCLUDERS: [], useGroundWindow: () => null, windowStyle: () => ({}) }))

import EstimatorGround from './EstimatorGround.jsx'
import { mintEstimatorHandoff } from './estimatorRoute.js'

const origin = 'https://estimator-staging.leafdesign.ai'
const projectId = '22222222-2222-4222-8222-222222222222'
const chan = 'a'.repeat(43)
const subh = 'sh_' + 'b'.repeat(32)
const token = 'a'.repeat(40) + '.' + 'b'.repeat(200) + '.' + 'c'.repeat(342)
const props = { active: true, projectId, accountScope: 'account:a', signedIn: true }
let realWindow

beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(1800000000000)
  state.authority = 'human-a'
  state.status = { state: 'on', bindings: [{ project_id: projectId, estimator_origin: origin }] }
  mintEstimatorHandoff.mockReset().mockResolvedValue({ token, subh, estimator_origin: origin, expires_in: 60 })
  realWindow = window
  vi.stubGlobal('window', new Proxy(realWindow, {
    get(target, key) {
      if (key === 'location') return { origin: 'https://platform-staging.leafdesign.ai' }
      const value = Reflect.get(target, key, target)
      return typeof value === 'function' ? value.bind(target) : value
    },
  }))
})
afterEach(() => { cleanup(); state.listeners.clear(); vi.unstubAllGlobals(); vi.useRealTimers(); vi.restoreAllMocks() })

const iframe = () => document.querySelector('iframe')
async function message(frame, data = {}, overrides = {}) {
  await act(async () => {
    realWindow.dispatchEvent(new MessageEvent('message', { source: frame.contentWindow, origin,
      data: { v: 1, type: 'estimator.ready', chan, seq: 1, ts: Date.now(), ...data }, ...overrides }))
  })
}

it('an unbound project never renders a frame or mints', () => {
  render(<EstimatorGround {...props} projectId="33333333-3333-4333-8333-333333333333" />)
  expect(screen.getByText('No estimator is linked to this project')).toBeVisible()
  expect(iframe()).toBeNull()
  expect(mintEstimatorHandoff).not.toHaveBeenCalled()
})

it('uses a bare bootstrap URL, minimal sandbox, and retains the same frame across tabs', async () => {
  const { rerender } = render(<EstimatorGround {...props} />)
  const frame = iframe()
  expect(frame.src).toBe(origin + '/studio/enter')
  expect(frame).toHaveAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-downloads allow-popups allow-popups-to-escape-sandbox')
  expect(frame).toHaveAttribute('referrerpolicy', 'no-referrer')
  expect(frame).toHaveAttribute('allow', '')
  expect(frame).toHaveAttribute('title', 'Project estimator')
  await message(frame)
  await message(frame, { type: 'estimator.session', state: 'active', subh, seq: 2 })
  rerender(<EstimatorGround {...props} active={false} />)
  expect(iframe()).toBe(frame)
  expect(frame.closest('section')).toHaveAttribute('hidden')
  rerender(<EstimatorGround {...props} />)
  expect(iframe()).toBe(frame)
  expect(mintEstimatorHandoff).toHaveBeenCalledOnce()
})

it('sign-out holds the mounted frame and the logout promise until ended', async () => {
  const { rerender } = render(<EstimatorGround {...props} />)
  const frame = iframe()
  await message(frame)
  const post = vi.spyOn(frame.contentWindow, 'postMessage')
  rerender(<EstimatorGround {...props} signedIn={false} />)
  expect(iframe()).toBe(frame)
  expect(frame).toHaveAttribute('hidden')
  expect(post).toHaveBeenCalledWith(expect.objectContaining({ type: 'studio.end' }), origin)
  const settled = vi.fn()
  let pending
  act(() => { pending = Promise.all([...state.listeners].map(listener => listener())).then(settled) })
  await act(async () => { await vi.advanceTimersByTimeAsync(1499) })
  expect(settled).not.toHaveBeenCalled()
  expect(iframe()).toBe(frame)
  await message(frame, { type: 'estimator.session', state: 'ended', seq: 2 })
  await act(async () => { await pending })
  expect(iframe()).toBeNull()
  expect(settled).toHaveBeenCalledOnce()
})

it.each([
  { projectId: '33333333-3333-4333-8333-333333333333' },
  { accountScope: 'account:b' }, { signedIn: false },
])('scope transition keeps the old frame only until the 1.5 s end timeout: %j', async change => {
  const { rerender } = render(<EstimatorGround {...props} />)
  const frame = iframe()
  await message(frame)
  const post = vi.spyOn(frame.contentWindow, 'postMessage')
  rerender(<EstimatorGround {...props} {...change} />)
  expect(iframe()).toBe(frame)
  expect(post).toHaveBeenCalledWith(expect.objectContaining({ type: 'studio.end' }), origin)
  await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
  expect(iframe()).toBeNull()
  expect(mintEstimatorHandoff).toHaveBeenCalledOnce()
})

it('late mint after an authority change never posts a token', async () => {
  let complete
  mintEstimatorHandoff.mockImplementation(() => new Promise(resolve => { complete = resolve }))
  const { rerender } = render(<EstimatorGround {...props} />)
  const frame = iframe()
  const post = vi.spyOn(frame.contentWindow, 'postMessage')
  await message(frame)
  state.authority = 'human-b'
  rerender(<EstimatorGround {...props} />)
  await act(async () => { complete({ token, subh, estimator_origin: origin, expires_in: 60 }) })
  expect(post.mock.calls.some(([data]) => data.type === 'studio.token')).toBe(false)
})

it('a project change starts the next handshake only when the tab is opened again', async () => {
  const nextProject = '33333333-3333-4333-8333-333333333333'
  state.status.bindings.push({ project_id: nextProject, estimator_origin: origin })
  const { rerender } = render(<EstimatorGround {...props} />)
  const frame = iframe()
  await message(frame)
  rerender(<EstimatorGround {...props} projectId={nextProject} />)
  await message(frame, { type: 'estimator.session', state: 'ended', seq: 2 })
  expect(iframe()).toBeNull()
  rerender(<EstimatorGround {...props} projectId={nextProject} active={false} />)
  rerender(<EstimatorGround {...props} projectId={nextProject} />)
  expect(iframe()).not.toBeNull()
  expect(iframe()).not.toBe(frame)
  await message(iframe())
  expect(mintEstimatorHandoff).toHaveBeenLastCalledWith(nextProject, chan, expect.any(Object))
})

it('only three automatic recreates per five minutes, then manual Retry', async () => {
  render(<EstimatorGround {...props} />)
  for (let index = 0; index < 4; index += 1) {
    const frame = iframe()
    await message(frame)
    await message(frame, { seq: 2 })
    expect(iframe()).not.toBe(frame)
  }
  expect(iframe()).toBeNull()
  expect(screen.getByText('The estimator did not respond. Retry')).toBeVisible()
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Retry' })) })
  expect(iframe()).not.toBeNull()
  await act(async () => { await vi.advanceTimersByTimeAsync(300001) })
  // The ready timeout is independently actionable; the recreate window expires.
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Retry' })) })
  const frame = iframe()
  await message(frame); await message(frame, { seq: 2 })
  expect(iframe()).not.toBeNull()
  expect(iframe()).not.toBe(frame)
})

it('subject mismatch recreates and a silent frame shows Retry after fifteen seconds', async () => {
  render(<EstimatorGround {...props} />)
  const frame = iframe()
  await message(frame)
  await message(frame, { type: 'estimator.session', state: 'active', subh: 'sh_' + 'c'.repeat(32), seq: 2 })
  expect(iframe()).not.toBe(frame)
  await act(async () => { await vi.advanceTimersByTimeAsync(15000) })
  expect(screen.getByText('The estimator did not respond. Retry')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Retry' })).toBeVisible()
})

it('429 disables manual Retry for sixty seconds', async () => {
  mintEstimatorHandoff.mockRejectedValue(Object.assign(new Error(), { status: 429 }))
  render(<EstimatorGround {...props} />)
  await message(iframe())
  expect(screen.getByRole('button', { name: 'Retry' })).toBeDisabled()
  await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
  expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled()
})

it('does not write the token into storage, DOM, URL or surface context', async () => {
  const store = vi.spyOn(Storage.prototype, 'setItem')
  const history = vi.spyOn(realWindow.history, 'replaceState')
  render(<EstimatorGround {...props} />)
  const post = vi.spyOn(iframe().contentWindow, 'postMessage')
  await message(iframe())
  expect(post).toHaveBeenCalledWith(expect.objectContaining({ token }), origin)
  expect(store).not.toHaveBeenCalled()
  expect(history).not.toHaveBeenCalled()
  expect(document.documentElement.outerHTML).not.toContain(token)
  expect(document.documentElement.outerHTML).not.toContain(chan)
  expect(iframe().src).toBe(origin + '/studio/enter')
})
