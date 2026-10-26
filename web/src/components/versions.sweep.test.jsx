// @vitest-environment jsdom
import { useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import VersionHistory from './VersionHistory.jsx'
import useDrawingVersionController from '../controllers/useDrawingVersionController.js'
import { restoreDrawingVersion } from '../api.js'
import { MSG_NETWORK, MSG_SERVICE } from '../errorHumanize.js'

vi.mock('../api.js', () => ({
  config: { mockDefault: false },
  getDrawingVersions: vi.fn(),
  restoreDrawingVersion: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

const data = { drawing_id: 'drawing-1', head: 3, latest: 3, versions: [{ v: 3 }, { v: 1 }] }
const head = { layers: ['Head'] }
const historical = { layers: ['Earlier'] }
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function mountController(adapters = {}) {
  return renderHook(() => useDrawingVersionController({
    initialIntake: head,
    initialDrawingState: { drawing_id: 'drawing-1', version: 3, head: 3, latest: 3 },
    ...adapters,
  }))
}

async function restoreWith(error) {
  restoreDrawingVersion.mockRejectedValue(error)
  render(<VersionHistory data={data} onClose={() => {}} onPreview={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: 'Restore' }))
  fireEvent.click(screen.getByRole('button', { name: 'Restore v1' }))
  return screen.findByRole('alert')
}

it('S1-75 explains an offline restore without exposing the fetch error', async () => {
  const alert = await restoreWith(new TypeError('Failed to fetch'))
  expect(alert.textContent).toBe(MSG_NETWORK)
  expect(screen.getByRole('button', { name: 'Restore v1' }).disabled).toBe(false)
})

it('S1-75 hides transport details on a rejected restore', async () => {
  const error = new Error('POST /api/drawings/private-id/versions/1/restore -> 403')
  const alert = await restoreWith(error)
  expect(alert.textContent).toBe(MSG_SERVICE)
  expect(error.message).toContain('private-id')
})

it('S1-142 keeps the latest preview when an earlier request finishes last', async () => {
  const slow = deferred()
  const newer = { layers: ['Later'] }
  const onApplyIntake = vi.fn()
  const { result } = mountController({
    loadVersion: vi.fn().mockReturnValueOnce(slow.promise).mockResolvedValueOnce({ intake: newer }),
    onApplyIntake,
  })
  let earlier
  act(() => { earlier = result.current.actions.previewVersion(1) })
  await act(async () => { await result.current.actions.previewVersion(2) })
  await act(async () => {
    slow.resolve({ intake: historical })
    expect(await earlier).toBeNull()
  })
  expect(result.current.shown).toBe(newer)
  expect(result.current.previewing).toEqual({ version: 2 })
  expect(onApplyIntake).toHaveBeenCalledTimes(1)
})

it('S1-142 discards a pending preview after Back to head', async () => {
  const slow = deferred()
  const { result } = mountController({
    loadVersion: () => slow.promise,
    loadHead: async () => ({ ...data, version: 3, intake: head }),
  })
  let earlier
  act(() => { earlier = result.current.actions.previewVersion(1) })
  await act(async () => { await result.current.actions.backToHead() })
  await act(async () => {
    slow.resolve({ intake: historical })
    expect(await earlier).toBeNull()
  })
  expect(result.current.shown).toBe(head)
  expect(result.current.previewing).toBeNull()
})

it('S1-142 ignores errors from a superseded preview', async () => {
  const slow = deferred()
  const onError = vi.fn()
  const { result } = mountController({
    loadVersion: vi.fn().mockReturnValueOnce(slow.promise).mockResolvedValueOnce({ intake: historical }),
    onError,
  })
  let earlier
  act(() => { earlier = result.current.actions.previewVersion(1) })
  await act(async () => { await result.current.actions.previewVersion(2) })
  await act(async () => {
    slow.reject(new Error('Failed to fetch'))
    await earlier
  })
  expect(result.current.historyError).toBeNull()
  expect(onError).not.toHaveBeenCalled()
})

it('S1-143 retains each refresh retry failure until a successful retry seats the drawing', async () => {
  const error = new TypeError('Failed to fetch')
  const loadHead = vi.fn().mockRejectedValue(error)
  const { result } = mountController({ loadHead })
  act(() => result.current.actions.markRefreshFailure({ drawing_id: 'drawing-1', version: 3 }))
  for (const retryAttempts of [1, 2]) {
    await act(async () => { await result.current.actions.retryRefresh() })
    expect(result.current.refreshFailure).toMatchObject({ error, retryAttempts, version: 3 })
    expect(result.current.refreshing).toBe(false)
    expect(result.current.shown).toBe(head)
  }
  loadHead.mockResolvedValue({ ...data, version: 3, intake: historical })
  await act(async () => { await result.current.actions.retryRefresh() })
  expect(result.current.refreshFailure).toBeNull()
  expect(result.current.shown).toBe(historical)
})

it('S1-143 retains each unreadable drawing retry failure without unlocking editing', async () => {
  const error = new Error('GET /api/drawings/private-id -> 503')
  const loadHead = vi.fn().mockRejectedValue(error)
  const { result } = mountController({ loadHead })
  act(() => result.current.actions.recordCommittedUnreadableHead({ drawing_id: 'drawing-1', version: 4 }))
  const message = result.current.unreadableHead.message
  for (const retryAttempts of [1, 2]) {
    await act(async () => { await result.current.actions.retryUnreadableHead() })
    expect(result.current.unreadableHead).toMatchObject({ error, retryAttempts, head: 4, message })
    expect(result.current.mutationsBlocked).toBe(true)
    expect(result.current.refreshing).toBe(false)
  }
  loadHead.mockResolvedValue({ drawing_id: 'drawing-1', version: 4, head: 4, latest: 4, intake: historical })
  await act(async () => { await result.current.actions.retryUnreadableHead() })
  expect(result.current.unreadableHead).toBeNull()
  expect(result.current.mutationsBlocked).toBe(false)
})

function HistoryHarness() {
  const [open, setOpen] = useState(false)
  return <>
    <button onClick={() => setOpen(true)}>History</button>
    {open && <VersionHistory data={data} onPreview={() => {}} onClose={() => setOpen(false)} />}
  </>
}

function openHistory() {
  render(<HistoryHarness />)
  const opener = screen.getByRole('button', { name: 'History' })
  opener.focus()
  fireEvent.click(opener)
  const close = screen.getByRole('button', { name: 'Close version history' })
  close.focus()
  return { opener, close }
}

it('S1-219 returns focus to History after Escape inside the drawer', () => {
  const { opener, close } = openHistory()
  fireEvent.keyDown(close, { key: 'Escape' })
  expect(screen.queryByRole('dialog', { name: 'Version history' })).toBeNull()
  expect(document.activeElement).toBe(opener)
})

it('S1-219 returns focus when the focused Close control dismisses history', () => {
  const { opener, close } = openHistory()
  fireEvent.click(close)
  expect(screen.queryByRole('dialog', { name: 'Version history' })).toBeNull()
  expect(document.activeElement).toBe(opener)
})
