// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import useDrawingVersionController from './useDrawingVersionController.js'

afterEach(cleanup)
const base = { dwg: 'base.dwg', layers: ['Roof', 'Hidden'] }
const historical = { dwg: 'historical.dwg', layers: ['Roof', 'Hidden'] }
const state = { drawing_id: 'base', version: 3, head: 3, latest: 5 }
const deferred = () => {
  let resolve
  const promise = new Promise(yes => { resolve = yes })
  return { promise, resolve }
}
let controller
function mount(adapters = {}) {
  const onApplyIntake = vi.fn(), onResetSelection = vi.fn()
  function Probe() {
    controller = useDrawingVersionController({ initialIntake: base, initialDrawingState: state,
      loadVersions: async () => ({ versions: [{ version: 1 }] }),
      loadVersion: async () => ({ intake: historical }), ...adapters,
      onApplyIntake, onResetSelection })
    return null
  }
  render(<Probe />)
  return { onApplyIntake, onResetSelection }
}

it('restores every snapshot field, including historical preview, history, failures and the unreadable-head lock', async () => {
  mount({ undoVersion: async () => { throw new Error('undo failed') },
    loadVersion: vi.fn().mockResolvedValueOnce({ intake: historical }).mockRejectedValueOnce(new Error('preview failed')) })
  const headIntake = { ...base, dwg: 'head.dwg' }
  act(() => { controller.actions.seatVersion({ ...state, intake: headIntake }) })
  await act(async () => { await controller.actions.undo() })
  act(() => { controller.actions.recordCommittedUnreadableHead({ version: 6 }) })
  await act(async () => { await controller.actions.toggleHistory(); await controller.actions.previewVersion(1) })
  await act(async () => { await controller.actions.previewVersion(2) })
  act(() => {
    controller.actions.markRefreshFailure({ drawing_id: 'base', message: 'refresh failed' })
    controller.actions.setVisibleLayers({ Roof: true, Hidden: false })
  })
  const snapshot = controller.captureSnapshot()
  expect(Object.isFrozen(snapshot)).toBe(true)
  expect(snapshot).toMatchObject({ previewing: { version: 1 }, historyOpen: true,
    versionIntake: headIntake, historyError: expect.any(Error),
    versionError: expect.any(Error), unreadableHead: { head: 6 },
    refreshFailure: { message: 'refresh failed' } })
  act(() => { controller.actions.seatIntake({ dwg: 'bridge', layers: ['Live'] }) })
  act(() => { controller.restoreSnapshot(snapshot) })
  for (const key of Object.keys(snapshot)) expect(controller[key]).toBe(snapshot[key])
  expect(controller.shown).toBe(historical)
  expect(controller.mutationsBlocked).toBe(true)
  expect(controller).toMatchObject({ versionBusy: false, historyLoading: false, refreshing: false })
})

it.each(['preview', 'refresh', 'history', 'undo'])('restore invalidates a late %s completion and settles busy flags', async operation => {
  const pending = deferred()
  const callbacks = mount({ loadVersion: () => pending.promise, loadHead: () => pending.promise,
    loadVersions: () => pending.promise, undoVersion: () => pending.promise })
  act(() => { controller.actions.markRefreshFailure({ drawing_id: 'base' }) })
  const snapshot = controller.captureSnapshot()
  let run
  act(() => {
    run = operation === 'preview' ? controller.actions.previewVersion(1)
      : operation === 'refresh' ? controller.actions.retryRefresh()
        : operation === 'history' ? controller.actions.loadHistory() : controller.actions.undo()
  })
  act(() => { controller.restoreSnapshot(snapshot) })
  callbacks.onApplyIntake.mockClear()
  callbacks.onResetSelection.mockClear()
  await act(async () => {
    pending.resolve({ intake: historical, ...state, versions: [{ version: 1 }] })
    expect(await run).toBeNull()
  })
  for (const key of Object.keys(snapshot)) expect(controller[key]).toBe(snapshot[key])
  expect(controller).toMatchObject({ versionBusy: false, historyLoading: false, refreshing: false })
  expect(callbacks.onApplyIntake).not.toHaveBeenCalled()
  expect(callbacks.onResetSelection).not.toHaveBeenCalled()
})

it('ignores null and foreign snapshots', () => {
  mount()
  const snapshot = controller.captureSnapshot()
  act(() => { controller.restoreSnapshot(null); controller.restoreSnapshot({ ...snapshot }) })
  expect(controller.intake).toBe(base)
  const foreign = controller.captureSnapshot()
  cleanup()
  mount()
  act(() => { controller.actions.seatIntake(historical) })
  act(() => { controller.restoreSnapshot(foreign) })
  expect(controller.shown).toBe(historical)
})

it('same-document updates keep hidden layers, add visible layers, and preserve history, preview and selection callbacks', async () => {
  const callbacks = mount()
  await act(async () => { await controller.actions.toggleHistory(); await controller.actions.previewVersion(1) })
  act(() => { controller.actions.setVisibleLayers({ Roof: true, Hidden: false }) })
  const before = controller.captureSnapshot()
  callbacks.onResetSelection.mockClear()
  const next = { dwg: 'next.dwg', layers: ['Hidden', 'New'] }
  act(() => { controller.updateIntake(next) })
  expect(controller.shown).toBe(next)
  expect(controller.visibleLayers).toEqual({ Hidden: false, New: true })
  expect(controller.history).toBe(before.history)
  expect(controller.historyOpen).toBe(true)
  expect(controller.previewing).toBe(before.previewing)
  expect(callbacks.onResetSelection).not.toHaveBeenCalled()
  expect(callbacks.onApplyIntake).toHaveBeenLastCalledWith(next, { source: 'update' })
})
