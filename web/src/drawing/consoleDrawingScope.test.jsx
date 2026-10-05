// @vitest-environment jsdom
import React from 'react'
import { readFileSync } from 'node:fs'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { DrawingIdentityProvider, useDrawingIdentity, useDrawingScopeReset } from './DrawingIdentityProvider.jsx'
import { hasDrawingSelection, seedDrawingIdentity } from './drawingIdentity.js'
import { WORKBENCH_ID_KEY } from '../site/workbenchId.js'
import useDrawingVersionController from '../controllers/useDrawingVersionController.js'

const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8').replace(/\r\n/g, '\n')
const start = source.indexOf('    let alive = true', source.indexOf('// load session (intake'))
const end = source.indexOf('  }, [mock, isEditFixture, intakeRetryKey', start)
const body = source.slice(start, end)
const workspaceEnd = source.indexOf('  } = workspaceController')
const projectHook = source.slice(workspaceEnd, source.indexOf('  const [projectPane, setProjectPane]', workspaceEnd))
  .split('\n').find((line) => line.includes('useDrawingScopeReset('))
const observeProject = new Function('useDrawingScopeReset', 'openProjectId', projectHook)
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
let identity, restoreAddress
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  sessionStorage.removeItem(WORKBENCH_ID_KEY)
  if (restoreAddress !== undefined) window.history.replaceState(null, '', restoreAddress)
  restoreAddress = undefined
})
function mount(projectId = 'p') {
  restoreAddress = window.location.pathname + window.location.search + window.location.hash
  window.history.replaceState(null, '', '/app?drawing=u-upload')
  sessionStorage.setItem(WORKBENCH_ID_KEY, 'u-upload')
  function Probe({ openProjectId }) {
    identity = useDrawingIdentity()
    observeProject(useDrawingScopeReset, openProjectId)
    return null
  }
  const tree = (openProjectId) => <DrawingIdentityProvider mode="console" scene="app" search="?drawing=u-upload"
    publicDemo={false} liveDemo={false} readAuthToken={() => null}>
    <Probe openProjectId={openProjectId} />
  </DrawingIdentityProvider>
  const view = render(tree(projectId))
  return { update: (next) => view.rerender(tree(next)) }
}
function loader(overrides = {}) {
  const noop = vi.fn()
  const context = {
    REQUESTED_DRAWING_ID: identity.drawingId, DRAWING_SOURCE: identity.source,
    requestedDrawingIdRef: { current: identity.drawingId },
    isScopeCurrent: identity.isScopeCurrent, hasDrawingSelection,
    mock: false, isEditFixture: false, resetDrawing: vi.fn(),
    setDrawingLoad: vi.fn(), setLoadErr: vi.fn(), resetCatalogTransient: noop,
    clearToast: noop, setDrawer: noop, setTenant: noop, setTier: noop,
    setOrg: noop, clearAgentSession: noop, mockVersions: { reset: noop },
    seatIntake: vi.fn(), sessionActions: { checking: vi.fn(), activate: vi.fn(), requireAuth: vi.fn() },
    getSession: vi.fn(async () => ({ intake: { dwg: 'u-upload' } })),
    getDrawingVersions: vi.fn(async () => ({ head: 1 })), adoptOrgId: noop,
    humanizeError: String, is401: () => false, classifyDrawingLoadFailure: () => 'transient',
    ...overrides,
  }
  const run = () => new Function(...Object.keys(context), body)(...Object.values(context))
  return { context, run }
}
function assertReset() {
  expect(window.location.pathname + window.location.search).toBe('/app')
  expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBeNull()
  expect(identity).toMatchObject({ drawingId: null, source: null, origin: 'reset' })
  act(() => { expect(identity.setFromQuery()).toEqual({ drawingId: null, source: null, origin: 'reset' }) })
  expect(seedDrawingIdentity({ mode: 'console', search: window.location.search }))
    .toMatchObject({ drawingId: 'demo', source: 'rooftop_demo' })
}

it('URL307B-11 late boot version summary cannot seat U', async () => {
  mount()
  const versions = deferred()
  const h = loader({ getDrawingVersions: vi.fn(() => versions.promise) })
  const oldCleanup = h.run()
  await Promise.resolve()
  expect(h.context.getDrawingVersions).toHaveBeenCalledExactlyOnceWith(false, 'u-upload')
  expect(h.context.sessionActions.activate).toHaveBeenCalledOnce()
  act(() => { identity.reset() })
  assertReset()
  // Settle before oldCleanup; the requested id ref still names the old drawing.
  expect(h.context.requestedDrawingIdRef.current).toBe('u-upload')
  versions.resolve({ head: 4, latest: 4 })
  await versions.promise
  await Promise.resolve()
  expect(h.context.seatIntake).not.toHaveBeenCalled()
  expect(h.context.setDrawingLoad).toHaveBeenCalledExactlyOnceWith({ drawingId: 'u-upload', state: 'pending' })
  oldCleanup()
})

it('URL307B-21 empty identity issues no drawing requests', () => {
  mount()
  act(() => { identity.reset() })
  assertReset()
  const h = loader()
  const active = { state: 'active' }
  h.context.sessionActions.activate.mockImplementation(() => { active.state = 'changed' })
  const dispose = h.run()
  expect(h.context.resetDrawing).toHaveBeenCalledOnce()
  expect(h.context.setLoadErr).toHaveBeenCalledExactlyOnceWith(null)
  expect(h.context.setDrawingLoad).toHaveBeenCalledExactlyOnceWith({ drawingId: null, state: 'idle' })
  expect(h.context.getSession).not.toHaveBeenCalled()
  expect(h.context.getDrawingVersions).not.toHaveBeenCalled()
  expect(h.context.sessionActions.checking).not.toHaveBeenCalled()
  expect(h.context.sessionActions.activate).not.toHaveBeenCalled()
  expect(h.context.sessionActions.requireAuth).not.toHaveBeenCalled()
  expect(h.context.seatIntake).not.toHaveBeenCalled()
  expect(active.state).toBe('active')
  dispose()
})

it('URL307B-30 App mounts the reset hook after workspace selection', () => {
  expect(projectHook.trim()).toBe('useDrawingScopeReset(openProjectId)')
  expect(source.split('useDrawingScopeReset(openProjectId)').length - 1).toBe(1)
  const h = mount(null)
  h.update('p')
  expect(identity.drawingId).toBe('u-upload')
  expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBe('u-upload')
  const oldCurrent = identity.isScopeCurrent
  h.update('q')
  expect(oldCurrent()).toBe(false)
  assertReset()
})

const completionStart = source.indexOf('const seatCompletedVersion = useCallback')
const completionEnd = source.indexOf('completedVersionRef.current = seatCompletedVersion', completionStart)
const completionBody = source.slice(completionStart, completionEnd) + '\nreturn seatCompletedVersion'
const completed = { drawing_id: 'u-upload', version: 3 }
const completedView = (id = 'u-upload') => ({
  drawing_id: id, version: 3, head: 3, latest: 3,
  intake: { dwg: id + '-v3.dxf', layers: ['panels'] },
})
function mountCompletion() {
  restoreAddress ??= window.location.pathname + window.location.search + window.location.hash
  window.history.replaceState(null, '', '/app?drawing=u-upload')
  sessionStorage.setItem(WORKBENCH_ID_KEY, 'u-upload')
  const h = {
    getDrawingIntake: vi.fn(async () => completedView()), loadHead: vi.fn(async () => completedView()),
    seat: vi.fn(), showToast: vi.fn(), unreadable: vi.fn(),
    mockVersions: { isSeeded: vi.fn(() => true), seedBase: vi.fn(), applyDelete: vi.fn() },
    callbacks: { onApplyIntake: vi.fn(), onResetSelection: vi.fn(), onVersionEvent: vi.fn(), onError: vi.fn() },
  }
  function Probe({ openProjectId }) {
    identity = useDrawingIdentity()
    observeProject(useDrawingScopeReset, openProjectId)
    h.controller = useDrawingVersionController({
      initialIntake: completedView().intake,
      initialDrawingState: { drawing_id: 'u-upload', version: 2, head: 2, latest: 3 },
      loadHead: h.loadHead, ...h.callbacks,
    })
    const context = {
      useCallback: (callback) => callback, isScopeCurrent: identity.isScopeCurrent,
      intake: h.controller.intake, mock: false, mockVersions: h.mockVersions,
      getDrawingIntake: h.getDrawingIntake, showToast: h.showToast,
      markRefreshFailure: h.controller.actions.markRefreshFailure,
      recordCommittedUnreadableHead: (value) => {
        h.unreadable(value)
        return h.controller.actions.recordCommittedUnreadableHead(value)
      },
      seatVersion: (value, id, notice) => {
        h.seat(value, id, notice)
        return h.controller.actions.seatVersion(value, { drawingId: id })
      },
    }
    h.complete = new Function(...Object.keys(context), completionBody)(...Object.values(context))
    return null
  }
  const tree = (openProjectId) => <DrawingIdentityProvider mode="console" scene="app" search="?drawing=u-upload"
    publicDemo={false} liveDemo={false} readAuthToken={() => null}><Probe openProjectId={openProjectId} /></DrawingIdentityProvider>
  const view = render(tree('p'))
  h.switchProject = () => {
    view.rerender(tree('q'))
    assertReset()
    expectCompletionEmpty(h)
    Object.values(h.callbacks).forEach((callback) => callback.mockClear())
  }
  return h
}
function expectCompletionEmpty(h) {
  expect(h.controller).toMatchObject({
    intake: null, versionIntake: null, shown: null, drawingState: null,
    historyOpen: false, history: null, historyError: null, historyLoading: false,
    previewing: null, previewIntake: null, versionBusy: false, versionError: null,
    refreshFailure: null, refreshing: false, unreadableHead: null, mutationsBlocked: false,
    visibleLayers: {},
  })
}
function expectNoCompletion(h) {
  expect(h.seat).not.toHaveBeenCalled()
  expect(h.showToast).not.toHaveBeenCalled()
  expect(h.unreadable).not.toHaveBeenCalled()
  Object.values(h.callbacks).forEach((callback) => expect(callback).not.toHaveBeenCalled())
}
async function obsoleteRejection(h) {
  const pending = deferred()
  h.getDrawingIntake.mockImplementationOnce(() => pending.promise)
  let run
  act(() => { run = h.complete(completed, {}) })
  h.switchProject()
  await act(async () => {
    pending.reject(new Error('Obsolete completed intake'))
    expect(await run).toBe(false)
  })
  expectCompletionEmpty(h)
  expectNoCompletion(h)
}

it('URL307B-38 late completed intake rejection publishes nothing', async () => {
  await obsoleteRejection(mountCompletion())
})

it('URL307B-39 retry after obsolete completion requests nothing', async () => {
  const h = mountCompletion()
  await obsoleteRejection(h)
  await act(async () => { expect(await h.controller.actions.retryRefresh()).toBeNull() })
  expect(h.loadHead).not.toHaveBeenCalled()
  expect(h.controller.refreshing).toBe(false)
  expectNoCompletion(h)
  cleanup()
  const next = mountCompletion()
  next.getDrawingIntake.mockRejectedValueOnce(new Error('Current completed intake'))
  await act(async () => { expect(await next.complete(completed, {})).toBe(false) })
  expect(next.controller.refreshFailure).toEqual(completed)
  const oldRetry = next.controller.actions.retryRefresh
  next.showToast.mockClear()
  next.switchProject()
  await act(async () => { expect(await oldRetry()).toBeNull() })
  expect(next.loadHead).not.toHaveBeenCalled()
  expect(next.controller.refreshing).toBe(false)
  expectCompletionEmpty(next)
  expectNoCompletion(next)
})

it('URL307B-40 late completed intake success publishes nothing', async () => {
  const h = mountCompletion()
  const pending = deferred()
  h.getDrawingIntake.mockImplementationOnce(() => pending.promise)
  let run
  act(() => { run = h.complete(completed, {}) })
  h.switchProject()
  await act(async () => { pending.resolve(completedView()); expect(await run).toBe(false) })
  expectCompletionEmpty(h)
  expectNoCompletion(h)
  act(() => { identity.setFromUpload({ drawing_id: 'v-upload', tenant_kind: 'account' }) })
  h.getDrawingIntake.mockResolvedValueOnce(completedView('v-upload'))
  await act(async () => {
    expect(await h.complete({ drawing_id: 'v-upload', version: 3 }, {})).toBe(true)
  })
  expect(h.seat).toHaveBeenCalledExactlyOnceWith(completedView('v-upload'), 'v-upload', 'Version 3 created')
  expect(h.controller.drawingState.drawing_id).toBe('v-upload')
  expect(identity.drawingId).toBe('v-upload')
  expect(window.location.pathname + window.location.search).toBe('/app?drawing=v-upload')
  expect(sessionStorage.getItem(WORKBENCH_ID_KEY)).toBe('v-upload')
})

it('URL307B-50 obsolete completion callback refuses entry', async () => {
  const h = mountCompletion()
  const oldComplete = h.complete
  h.switchProject()
  await act(async () => {
    expect(await oldComplete(completed, { result: { new_version_readable: false } })).toBe(false)
    expect(await oldComplete(completed, { result: { new_version_readable: true } })).toBe(false)
  })
  expect(h.getDrawingIntake).not.toHaveBeenCalled()
  Object.values(h.mockVersions).forEach((callback) => expect(callback).not.toHaveBeenCalled())
  expectCompletionEmpty(h)
  expectNoCompletion(h)
})
