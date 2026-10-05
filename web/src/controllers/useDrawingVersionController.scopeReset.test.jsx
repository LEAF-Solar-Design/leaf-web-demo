// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import useDrawingVersionController from './useDrawingVersionController.js'
import { DrawingIdentityProvider, useDrawingIdentity } from '../drawing/DrawingIdentityProvider.jsx'
import { seedDrawingIdentity } from '../drawing/drawingIdentity.js'
import { WORKBENCH_ID_KEY } from '../site/workbenchId.js'

const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const intake = (id) => ({ dwg: id + '-v1.dxf', layers: ['panels'] })
const version = (id, head = 2) => ({ drawing_id: id, version: head, head, latest: 3, intake: intake(id) })
let identity, controller, originalAddress
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  sessionStorage.removeItem(WORKBENCH_ID_KEY)
  if (originalAddress !== undefined) window.history.replaceState(null, '', originalAddress)
  originalAddress = undefined
})
function mount(overrides = {}) {
  originalAddress ??= window.location.pathname + window.location.search + window.location.hash
  window.history.replaceState(null, '', '/app?drawing=u-upload')
  sessionStorage.setItem(WORKBENCH_ID_KEY, 'u-upload')
  const callbacks = { onApplyIntake: vi.fn(), onResetSelection: vi.fn(), onVersionEvent: vi.fn(), onError: vi.fn() }
  const adapters = {
    loadHead: vi.fn(async () => version('u-upload')),
    loadVersion: vi.fn(async () => version('u-upload', 1)),
    loadVersions: vi.fn(async () => ({ versions: [] })),
    undoVersion: vi.fn(async () => version('u-upload', 1)),
    redoVersion: vi.fn(async () => version('u-upload', 3)),
    ...overrides,
  }
  function Probe() {
    identity = useDrawingIdentity()
    controller = useDrawingVersionController({
      initialIntake: intake('u-upload'), initialDrawingState: { drawing_id: 'u-upload', version: 2, head: 2, latest: 3 },
      ...adapters, ...callbacks,
    })
    return null
  }
  render(<DrawingIdentityProvider mode="console" scene="app" search="?drawing=u-upload"
    publicDemo={false} liveDemo={false} readAuthToken={() => null}><Probe /></DrawingIdentityProvider>)
  expect(identity.drawingId).toBe('u-upload')
  act(() => { expect(identity.setFromQuery().drawingId).toBe('u-upload') })
  return { callbacks, adapters }
}
function clearCallbacks(h) { Object.values(h.callbacks).forEach((callback) => callback.mockClear()) }
function expectEmpty() {
  expect(controller).toMatchObject({
    intake: null, versionIntake: null, shown: null, drawingState: null,
    historyOpen: false, history: null, historyError: null, historyLoading: false,
    previewing: null, previewIntake: null, versionBusy: false, versionError: null,
    refreshFailure: null, refreshing: false, unreadableHead: null, mutationsBlocked: false,
    visibleLayers: {},
  })
}
function reset(h) {
  const oldCurrent = identity.isScopeCurrent
  act(() => { identity.reset() })
  expect(oldCurrent()).toBe(false)
  expect(identity).toMatchObject({ drawingId: null, source: null, origin: 'reset' })
  expect(window.location.pathname + window.location.search).toBe('/app')
  expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
  act(() => { expect(identity.setFromQuery().origin).toBe('reset') })
  expect(seedDrawingIdentity({ mode: 'console', search: window.location.search }))
    .toMatchObject({ drawingId: 'demo', source: 'rooftop_demo' })
  expectEmpty()
  clearCallbacks(h)
}
function expectNoCallbacks(h) { Object.values(h.callbacks).forEach((callback) => expect(callback).not.toHaveBeenCalled()) }
async function settle(pending, run, value) {
  let result
  await act(async () => { pending.resolve(value); result = await run })
  expect(result).toBeNull()
}
function selectV(h) {
  act(() => { identity.setFromUpload({ drawing_id: 'v-upload', tenant_kind: 'account' }) })
  act(() => { controller.actions.seatIntake(intake('v-upload'), { drawingId: 'v-upload', drawingState: version('v-upload') }) })
  clearCallbacks(h)
}
async function lateOperation(operation, adapter, prepare) {
  const pending = deferred()
  const h = mount({ [adapter]: vi.fn(() => pending.promise) })
  if (prepare) act(() => { prepare(controller.actions) })
  let run
  act(() => { run = operation(controller.actions) })
  expect(h.adapters[adapter]).toHaveBeenCalled()
  reset(h)
  await settle(pending, run, adapter === 'loadVersions' ? { versions: [{ version: 1 }] } : version('u-upload'))
  expectEmpty()
  expectNoCallbacks(h)
}

it('URL307B-12 late preview cannot restore U', async () => {
  await lateOperation((actions) => actions.previewVersion(1), 'loadVersion')
})

it('URL307B-25 version changes never write the drawing URL', async () => {
  const h = mount()
  const scopeToken = identity.scopeToken
  const write = vi.spyOn(window.history, 'replaceState')
  await act(async () => { await controller.actions.previewVersion(1) })
  expect(controller.previewing).toEqual({ version: 1 })
  await act(async () => { await controller.actions.backToHead() })
  expect(controller.previewing).toBeNull()
  expect(controller.head).toBe(2)
  expect(identity.drawingId).toBe('u-upload')
  expect(identity.scopeToken).toBe(scopeToken)
  act(() => { expect(identity.setFromQuery().drawingId).toBe('u-upload') })
  expect(window.location.pathname + window.location.search).toBe('/app?drawing=u-upload')
  expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBe('u-upload')
  expect(seedDrawingIdentity({ mode: 'console', search: window.location.search }).drawingId).toBe('u-upload')
  expect(write).not.toHaveBeenCalled()
  expect(h.callbacks.onError).not.toHaveBeenCalled()
})

it('URL307B-31 late history cannot reopen the old drawing', async () => {
  await lateOperation((actions) => actions.toggleHistory(), 'loadVersions')
  cleanup()
  const old = deferred(), newer = deferred()
  const h = mount({ loadVersions: vi.fn().mockImplementationOnce(() => old.promise).mockImplementationOnce(() => newer.promise) })
  let run
  act(() => { run = controller.actions.toggleHistory() })
  reset(h)
  selectV(h)
  let nextRun
  act(() => { nextRun = controller.actions.loadHistory() })
  expect(controller.historyLoading).toBe(true)
  await settle(old, run, { versions: [{ version: 1 }] })
  expect(controller.historyLoading).toBe(true)
  expect(controller.historyOpen).toBe(false)
  expect(controller.history).toBeNull()
  expect(controller.drawingState.drawing_id).toBe('v-upload')
  expectNoCallbacks(h)
  await act(async () => { newer.resolve({ versions: [{ version: 2 }] }); await nextRun })
  expect(controller.historyLoading).toBe(false)
})

it('URL307B-32 late undo cannot seat the old drawing', async () => {
  await lateOperation((actions) => actions.undo(), 'undoVersion')
  cleanup()
  const old = deferred(), newer = deferred()
  const h = mount({ undoVersion: vi.fn().mockImplementationOnce(() => old.promise).mockImplementationOnce(() => newer.promise) })
  let run
  act(() => { run = controller.actions.undo() })
  reset(h)
  selectV(h)
  let nextRun
  act(() => { nextRun = controller.actions.undo() })
  expect(controller.versionBusy).toBe(true)
  await settle(old, run, version('u-upload', 1))
  expect(controller.versionBusy).toBe(true)
  expect(controller.drawingState.drawing_id).toBe('v-upload')
  expect(controller.versionIntake).toBeNull()
  expectNoCallbacks(h)
  await act(async () => { newer.resolve(version('v-upload', 1)); await nextRun })
  expect(controller.versionBusy).toBe(false)
})

it('URL307B-33 late head refresh cannot seat the old drawing', async () => {
  await lateOperation((actions) => actions.refreshHead(), 'loadHead')
})

it('URL307B-34 late unreadable-head retry cannot seat the old drawing', async () => {
  await lateOperation((actions) => actions.retryUnreadableHead(), 'loadHead',
    (actions) => actions.recordCommittedUnreadableHead({ drawing_id: 'u-upload', version: 3 }))
  cleanup()
  const old = deferred(), newer = deferred()
  const h = mount({ loadHead: vi.fn().mockImplementationOnce(() => old.promise).mockImplementationOnce(() => newer.promise) })
  act(() => { controller.actions.recordCommittedUnreadableHead({ drawing_id: 'u-upload', version: 3 }) })
  let run
  act(() => { run = controller.actions.retryUnreadableHead() })
  reset(h)
  selectV(h)
  act(() => { controller.actions.recordCommittedUnreadableHead({ drawing_id: 'v-upload', version: 3 }) })
  let nextRun
  act(() => { nextRun = controller.actions.retryUnreadableHead() })
  const newerLock = controller.unreadableHead
  await settle(old, run, version('u-upload', 3))
  expect(controller.refreshing).toBe(true)
  expect(controller.unreadableHead).toBe(newerLock)
  expect(controller.drawingState.drawing_id).toBe('v-upload')
  expectNoCallbacks(h)
  await act(async () => { newer.resolve(version('v-upload', 3)); await nextRun })
  expect(controller.refreshing).toBe(false)
})

it('URL307B-35 late refresh retry cannot seat the old drawing', async () => {
  await lateOperation((actions) => actions.retryRefresh(), 'loadHead',
    (actions) => actions.markRefreshFailure({ drawing_id: 'u-upload' }))
  cleanup()
  const old = deferred(), newer = deferred()
  const h = mount({ loadHead: vi.fn().mockImplementationOnce(() => old.promise).mockImplementationOnce(() => newer.promise) })
  act(() => { controller.actions.markRefreshFailure({ drawing_id: 'u-upload' }) })
  let run
  act(() => { run = controller.actions.retryRefresh() })
  reset(h)
  selectV(h)
  act(() => { controller.actions.markRefreshFailure({ drawing_id: 'v-upload' }) })
  let nextRun
  act(() => { nextRun = controller.actions.retryRefresh() })
  const newerFailure = controller.refreshFailure
  await settle(old, run, version('u-upload'))
  expect(controller.refreshing).toBe(true)
  expect(controller.refreshFailure).toBe(newerFailure)
  expect(controller.drawingState.drawing_id).toBe('v-upload')
  expectNoCallbacks(h)
  await act(async () => { newer.resolve(version('v-upload')); await nextRun })
  expect(controller.refreshing).toBe(false)
})

it('URL307B-36 late restore completion cannot rearm the old drawing', async () => {
  await lateOperation((actions) => actions.recordRestore({ drawing_id: 'u-upload', head: 3, latest: 3 }), 'loadHead')
})

it('URL307B-37 stale rejection cannot alter the new scope', async () => {
  const old = deferred()
  const h = mount({ loadVersion: vi.fn(() => old.promise) })
  let run
  act(() => { run = controller.actions.previewVersion(1) })
  reset(h)
  selectV(h)
  const before = controller
  let result
  await act(async () => { old.reject(new Error('Obsolete preview')); result = await run })
  expect(result).toBeNull()
  expect(controller).toBe(before)
  expect(identity.drawingId).toBe('v-upload')
  expect(window.location.pathname + window.location.search).toBe('/app?drawing=v-upload')
  expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBe('v-upload')
  expectNoCallbacks(h)
})

it('standalone reset invalidates pending work without an identity provider', async () => {
  const pending = deferred()
  const onApplyIntake = vi.fn()
  const onError = vi.fn()
  function Probe() {
    controller = useDrawingVersionController({
      initialIntake: intake('u-upload'),
      initialDrawingState: { drawing_id: 'u-upload', head: 2, latest: 3 },
      loadVersion: () => pending.promise, onApplyIntake, onError,
    })
    return null
  }
  render(<Probe />)
  let run
  act(() => { run = controller.actions.previewVersion(1) })
  act(() => { controller.actions.reset() })
  await settle(pending, run, version('u-upload', 1))
  expectEmpty()
  expect(onApplyIntake).not.toHaveBeenCalled()
  expect(onError).not.toHaveBeenCalled()
})

it('URL307B-41 scope invalidation precedes layout reset', async () => {
  const old = deferred()
  const h = mount({ loadVersions: vi.fn(() => old.promise) })
  let run
  act(() => { run = controller.actions.loadHistory() })
  const oldCurrent = identity.isScopeCurrent
  const resets = h.callbacks.onResetSelection.mock.calls.length
  await act(async () => {
    identity.reset()
    expect(oldCurrent()).toBe(false)
    expect(h.callbacks.onResetSelection).toHaveBeenCalledTimes(resets)
    old.resolve({ versions: [{ version: 1 }] })
    expect(await run).toBeNull()
    expect(h.callbacks.onResetSelection).toHaveBeenCalledTimes(resets)
  })
  expectEmpty()
  expect(identity).toMatchObject({ drawingId: null, source: null, origin: 'reset' })
  expect(window.location.pathname + window.location.search).toBe('/app')
  expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
  act(() => { expect(identity.setFromQuery().origin).toBe('reset') })
  expect(seedDrawingIdentity({ mode: 'console', search: window.location.search }))
    .toMatchObject({ drawingId: 'demo', source: 'rooftop_demo' })
})

async function rejectOld(old, run) {
  await act(async () => {
    old.reject(new Error('Obsolete operation'))
    expect(await run).toBeNull()
  })
}
function expectV(h, before) {
  expect(controller).toBe(before)
  expect(identity.drawingId).toBe('v-upload')
  expect(window.location.pathname + window.location.search).toBe('/app?drawing=v-upload')
  expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBe('v-upload')
  expectNoCallbacks(h)
}
async function rejectionWithBusy(operation, adapter, busy, prepare) {
  const old = deferred(), newer = deferred()
  const h = mount({ [adapter]: vi.fn().mockImplementationOnce(() => old.promise).mockImplementationOnce(() => newer.promise) })
  if (prepare) act(() => { prepare(controller.actions, 'u-upload') })
  let run
  act(() => { run = operation(controller.actions) })
  reset(h)
  selectV(h)
  if (prepare) act(() => { prepare(controller.actions, 'v-upload') })
  let nextRun
  act(() => { nextRun = operation(controller.actions) })
  clearCallbacks(h)
  const before = controller
  const newerFailure = controller.refreshFailure
  const newerLock = controller.unreadableHead
  expect(controller[busy]).toBe(true)
  await rejectOld(old, run)
  expectV(h, before)
  expect(controller[busy]).toBe(true)
  expect(controller.refreshFailure).toBe(newerFailure)
  expect(controller.unreadableHead).toBe(newerLock)
  await act(async () => {
    newer.resolve(adapter === 'loadVersions' ? { versions: [{ version: 2 }] } : version('v-upload', 3))
    await nextRun
  })
  expect(controller[busy]).toBe(false)
}

it('URL307B-42 late history rejection preserves new scope', async () => {
  await rejectionWithBusy((actions) => actions.loadHistory(), 'loadVersions', 'historyLoading')
})

it('URL307B-43 late undo rejection preserves new scope', async () => {
  await rejectionWithBusy((actions) => actions.undo(), 'undoVersion', 'versionBusy')
})

it('URL307B-44 late redo rejection preserves new scope', async () => {
  await rejectionWithBusy((actions) => actions.redo(), 'redoVersion', 'versionBusy')
})

it('URL307B-45 late head refresh rejection preserves new scope', async () => {
  const old = deferred()
  const h = mount({ loadHead: vi.fn(() => old.promise) })
  let run
  act(() => { run = controller.actions.refreshHead() })
  reset(h)
  selectV(h)
  const before = controller
  await rejectOld(old, run)
  expectV(h, before)
})

it('URL307B-46 late restore rejection precedes layout reset', async () => {
  const old = deferred()
  const h = mount({ loadHead: vi.fn(() => old.promise) })
  let run
  act(() => { run = controller.actions.recordRestore({ drawing_id: 'u-upload', head: 3, latest: 3 }) })
  const oldCurrent = identity.isScopeCurrent
  const resets = h.callbacks.onResetSelection.mock.calls.length
  h.callbacks.onError.mockClear()
  await act(async () => {
    identity.reset()
    expect(oldCurrent()).toBe(false)
    expect(h.callbacks.onResetSelection).toHaveBeenCalledTimes(resets)
    old.reject(new Error('Obsolete restore'))
    expect(await run).toBeNull()
    expect(h.callbacks.onResetSelection).toHaveBeenCalledTimes(resets)
    expect(h.callbacks.onError).not.toHaveBeenCalled()
  })
  expectEmpty()
  expect(identity).toMatchObject({ drawingId: null, source: null, origin: 'reset' })
  expect(window.location.pathname + window.location.search).toBe('/app')
  expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
  act(() => { expect(identity.setFromQuery().origin).toBe('reset') })
  expect(seedDrawingIdentity({ mode: 'console', search: window.location.search }))
    .toMatchObject({ drawingId: 'demo', source: 'rooftop_demo' })
})

it('URL307B-47 late refresh retry rejection preserves new scope', async () => {
  await rejectionWithBusy((actions) => actions.retryRefresh(), 'loadHead', 'refreshing',
    (actions, id) => actions.markRefreshFailure({ drawing_id: id }))
})

it('URL307B-48 late unreadable retry rejection preserves new scope', async () => {
  await rejectionWithBusy((actions) => actions.retryUnreadableHead(), 'loadHead', 'refreshing',
    (actions, id) => actions.recordCommittedUnreadableHead({ drawing_id: id, version: 3 }))
})

it('URL307B-49 retained failure setter cannot publish or clear', () => {
  const h = mount()
  const oldMarker = controller.actions.markRefreshFailure
  reset(h)
  selectV(h)
  const failure = { drawing_id: 'v-upload', version: 3 }
  act(() => { controller.actions.markRefreshFailure(failure) })
  const before = controller
  act(() => { oldMarker({ drawing_id: 'u-upload', version: 2 }) })
  expect(controller.refreshFailure).toBe(failure)
  expectV(h, before)
  act(() => { oldMarker(null) })
  expect(controller.refreshFailure).toBe(failure)
  expectV(h, before)
  expect(h.adapters.loadHead).not.toHaveBeenCalled()
})
