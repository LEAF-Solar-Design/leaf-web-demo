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
const proofStart = source.indexOf('  const sessionProof = useMemo(')
const proofBody = source.slice(proofStart, source.indexOf('  useEffect(() => {', proofStart))
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
  const scope = {}
  const snapshot = { status: 'checking', tokenRevision: 0, recoveries: 0, reason: null }
  const context = {
    REQUESTED_DRAWING_ID: identity.drawingId, DRAWING_SOURCE: identity.source,
    requestedDrawingIdRef: { current: identity.drawingId },
    isScopeCurrent: identity.isScopeCurrent, hasDrawingSelection,
    mock: false, isEditFixture: false, resetDrawing: vi.fn(),
    setDrawingLoad: vi.fn(), setLoadErr: vi.fn(), resetCatalogTransient: noop,
    clearToast: noop, setDrawer: noop, setTenant: noop, setTier: noop,
    setOrg: noop, clearAgentSession: noop, mockVersions: { reset: noop },
    seatIntake: vi.fn(), sessionActions: { checking: vi.fn(), activate: vi.fn(() => { snapshot.status = 'active' }), requireAuth: vi.fn() },
    useMemo: fn => fn(), sessionProofScope: scope, sessionProofScopeRef: { current: scope },
    session: { ...snapshot, controller: { getSnapshot: () => snapshot } },
    sessionAuthorization: 'Bearer token', authHeaders: () => ({ Authorization: 'Bearer token' }),
    setSignedIn: noop, isSignedIn: () => true,
    getSession: vi.fn(async () => ({ intake: { dwg: 'u-upload' } })),
    getDrawingVersions: vi.fn(async () => ({ head: 1 })), adoptOrgId: noop,
    humanizeError: String, is401: () => false, classifyDrawingLoadFailure: () => 'transient',
    ...overrides,
  }
  context.sessionProof = new Function(...Object.keys(context), `${proofBody}; return sessionProof`)(...Object.values(context))
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
  // The legacy 200 proves sign-in before the secondary versions request finishes.
  expect(h.context.sessionActions.activate).toHaveBeenCalledExactlyOnceWith({ tenant: undefined, tier: undefined, org: undefined })
  expect(h.context.sessionActions.checking).not.toHaveBeenCalled()
  expect(h.context.sessionActions.requireAuth).not.toHaveBeenCalled()
  act(() => { identity.reset() })
  assertReset()
  // Settle before oldCleanup; the requested id ref still names the old drawing.
  expect(h.context.requestedDrawingIdRef.current).toBe('u-upload')
  versions.resolve({ head: 4, latest: 4 })
  await versions.promise
  await Promise.resolve()
  expect(h.context.seatIntake).not.toHaveBeenCalled()
  expect(h.context.setDrawingLoad).toHaveBeenCalledExactlyOnceWith({ drawingId: 'u-upload', state: 'pending' })
  expect(h.context.sessionActions.activate).toHaveBeenCalledOnce()
  expect(h.context.sessionActions.requireAuth).not.toHaveBeenCalled()
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

function extractCompletion(product = source) {
  const declaration = 'const seatCompletedVersion = useCallback'
  const assignment = 'completedVersionRef.current = seatCompletedVersion'
  expect(product.split(declaration)).toHaveLength(2)
  expect(product.split(assignment)).toHaveLength(2)
  const start = product.indexOf(declaration)
  const end = product.indexOf(assignment, start)
  expect(start).toBeGreaterThanOrEqual(0)
  expect(end).toBeGreaterThan(start)
  return product.slice(start, end) + '\nreturn seatCompletedVersion'
}
const completionBody = extractCompletion()
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
      projectModeRef: { current: false }, projectSolarRuntime: { complete: vi.fn() },
      intake: h.controller.intake, mock: false, mockVersions: h.mockVersions,
      getDrawingIntake: h.getDrawingIntake, showToast: h.showToast, onUndo: () => {},
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

const completionUndo = () => {}
function completionHarness(project, variant = {}, product = source) {
  const extracted = extractCompletion(product)
  expect(extracted).toContain('showToast, projectSolarRuntime.complete]')
  const work = []
  const record = (name, value) => vi.fn((...args) => { work.push([name, ...args]); return value })
  const runtime = { complete: vi.fn(async () => variant.runtimeResult ?? true) }
  const h = { work, runtime }
  const context = {
    useCallback: (callback, dependencies) => {
      expect(dependencies).toHaveLength(9)
      expect(dependencies[8]).toBe(runtime.complete)
      return callback
    },
    projectModeRef: { current: project }, projectSolarRuntime: runtime,
    intake: { dwg: 'base-intake' }, onUndo: completionUndo,
    mockVersions: {
      isSeeded: record('isSeeded', false), seedBase: record('seedBase'),
      applyDelete: vi.fn((...args) => {
        work.push(['applyDelete', ...args])
        if (variant.mockFailure) throw new Error('mock completion failed')
        return { version: completed.version }
      }),
    },
    getDrawingIntake: vi.fn(async (...args) => {
      work.push(['fetch', ...args])
      if (variant.readFailure) throw new Error('completion read failed')
      return completedView()
    }),
    seatVersion: record('seat'), showToast: record('toast'),
    markRefreshFailure: record('failure'), recordCommittedUnreadableHead: record('unreadable'),
  }
  Object.defineProperty(context, 'isScopeCurrent', { enumerable: true, get: () => {
    work.push(['scopeRead'])
    return record('scopeCheck', !variant.stale)
  } })
  Object.defineProperty(context, 'mock', { enumerable: true, get: () => {
    work.push(['mockRead'])
    return !!variant.mock
  } })
  // Resolve the callback's free bindings at execution, including the scope
  // accessor, so even entering the first legacy statement is observable.
  h.complete = new Function('context', 'with (context) {\n' + extracted + '\n}')(context)
  work.length = 0 // Dependency evaluation is construction, not dispatch.
  return h
}
function changedCompletion(from, to) {
  const extracted = extractCompletion()
  const block = extracted.slice(0, -'\nreturn seatCompletedVersion'.length)
  expect(block.split(from)).toHaveLength(2)
  expect(source.split(block)).toHaveLength(2)
  const changed = source.replace(block, block.replace(from, to))
  expect(changed).not.toBe(source)
  return changed
}
const canonicalDispatch = [
  '    let canonical = false',
  '    try {',
  "      canonical = Object.prototype.hasOwnProperty.call(options ?? {}, 'projectSolarCompletion')",
  '    } catch {',
  '      canonical = false',
  '    }',
  '    if (canonical) {',
  '      if (!projectModeRef.current) return false',
  '      let metadata',
  '      try {',
  '        metadata = options.projectSolarCompletion',
  '      } catch {',
  '        return false',
  '      }',
  '      return projectSolarRuntime.complete(envelope, metadata)',
  '    }',
].join('\n')
const legacyVariants = [
  {}, { readFailure: true }, { unreadable: true }, { stale: true }, { quiet: true },
  { mock: true }, { mock: true, mockFailure: true },
]
async function assertLegacyParity(envelope, variant = {}, product = source) {
  const standalone = completionHarness(false, variant, product)
  const project = completionHarness(true, variant, product)
  const value = typeof envelope === 'function' ? envelope(variant) : variant.unreadable
    ? { ...envelope, result: { ...envelope?.result, new_version_readable: false } } : envelope
  const options = 'options' in variant ? variant.options : variant.quiet ? { announce: false } : undefined
  const expected = await standalone.complete(completed, value, options)
  const actual = await project.complete(completed, value, options)
  expect(actual).toBe(expected)
  expect(project.work).toEqual(standalone.work)
  expect(project.work[0]).toEqual(['scopeRead'])
  expect(project.runtime.complete).not.toHaveBeenCalled()
  expect(standalone.runtime.complete).not.toHaveBeenCalled()
  const names = project.work.map(([name]) => name)
  expect(names).toContain('scopeCheck')
  expect(actual).toBe(!(variant.stale || variant.unreadable || variant.readFailure || variant.mockFailure))
  if (variant.stale) expect(names).toEqual(['scopeRead', 'scopeCheck'])
  else if (variant.unreadable) {
    expect(names).toContain('unreadable')
    expect(names).not.toContain('fetch')
  } else if (variant.mockFailure) {
    expect(names).toContain('failure')
    expect(names).not.toContain('fetch')
  } else {
    expect(project.work).toContainEqual(['fetch', !!variant.mock, completed.drawing_id, 'head'])
    if (variant.readFailure) expect(names).toContain('failure')
    else expect(project.work).toContainEqual(['seat', completedView(), completed.drawing_id,
      variant.quiet ? null : 'Version 3 created'])
  }
  if (variant.mock && !variant.stale) {
    expect(names).toContain('isSeeded')
    expect(names).toContain('seedBase')
    expect(names).toContain('applyDelete')
  }
  if (variant.quiet) expect(names).not.toContain('toast')
  return { project, standalone }
}

it('R13G-08 canonical completion uses the explicit channel', async () => {
  expect(source.split('completedVersionRef.current = seatCompletedVersion')).toHaveLength(2)
  const exercise = async (product) => {
    const outcome = { ok: true, tool: 'solar-settings', result: { canonical: true } }
    const meta = { tool: 'solar-settings', capture: 'same-meta' }
    for (const runtimeResult of [true, false]) {
      for (const project of [true, false]) {
        for (const metadata of [meta, null, undefined]) {
          const h = completionHarness(project, { runtimeResult }, product)
          expect(await h.complete(null, outcome, { projectSolarCompletion: metadata })).toBe(project ? runtimeResult : false)
          if (project) {
            expect(h.runtime.complete).toHaveBeenCalledExactlyOnceWith(outcome, metadata)
            expect(h.runtime.complete.mock.calls[0][0]).toBe(outcome)
            expect(h.runtime.complete.mock.calls[0][1]).toBe(metadata)
          } else expect(h.runtime.complete).not.toHaveBeenCalled()
          expect(h.work).toEqual([])
        }
      }
    }
    const inherited = completionHarness(true, {}, product)
    const options = Object.create({ projectSolarCompletion: meta })
    expect(await inherited.complete(completed, outcome, options)).toBe(false)
    expect(inherited.runtime.complete).not.toHaveBeenCalled()
    expect(inherited.work).toEqual([])
  }
  await exercise(source)
  const mutation = changedCompletion(canonicalDispatch + '\n', '')
  await expect(exercise(mutation)).rejects.toThrow()
})

it("R13G-08b a known non-Solar completion in a project runs main's legacy body", async () => {
  const exercise = async (product) => {
    for (const envelope of [
      { ok: true, tool: 'drawing.write', result: { new_version: completed } },
      { ok: true, result: { tool: 'count-panels', new_version: completed } },
      { tool: ' CAD-EDIT-SAVE ', result: { tool: 'drawing.write' } },
    ]) {
      for (const variant of legacyVariants) await assertLegacyParity(envelope, variant, product)
    }
    for (const variant of [{ readFailure: true, quiet: true }, { unreadable: true, quiet: true }]) {
      await assertLegacyParity({ tool: 'drawing.write', result: {} }, variant, product)
    }
  }
  await exercise(source)
  const mutation = changedCompletion('    if (projectModeRef.current) {',
    '    if (projectModeRef.current) return false\n    if (projectModeRef.current) {')
  await expect(exercise(mutation)).rejects.toThrow()
})

it('R13G-08c a Solar completion in a project is refused before any legacy statement', async () => {
  const exercise = async (product) => {
    const envelopes = [
      { tool: 'solar-settings' }, { tool: ' SOLAR-SETTINGS ' }, { tool: '\tSoLaR-settings\n' },
      { result: { tool: 'solar-settings' } },
      { tool: 'drawing.write', result: { tool: ' SOLAR-SETTINGS ' } },
    ]
    for (const envelope of envelopes) {
      for (const variant of [{}, { mock: true }]) {
        const h = completionHarness(true, variant, product)
        expect(await h.complete(completed, envelope)).toBe(false)
        expect(h.work).toEqual([])
        expect(h.runtime.complete).not.toHaveBeenCalled()
      }
      const standalone = completionHarness(false, {}, product)
      expect(await standalone.complete(completed, envelope)).toBe(true)
      expect(standalone.work).toContainEqual(['fetch', false, completed.drawing_id, 'head'])
    }
  }
  await exercise(source)
  const noPredicate = changedCompletion('      if (solarIdentity) return false', '      if (false) return false')
  await expect(exercise(noPredicate)).rejects.toThrow()
  const onlyTopLevel = changedCompletion('[() => envelope?.tool, () => envelope?.result?.tool]', '[() => envelope?.tool]')
  await expect(exercise(onlyTopLevel)).rejects.toThrow()
})

const identityThrow = () => { throw new Error('identity exploded') }
const readableFlag = (variant) => (variant.unreadable ? { new_version_readable: false } : {})
const explodingTool = (key, base) => new Proxy(base, {
  get: (target, name, receiver) => (name === key ? identityThrow() : Reflect.get(target, name, receiver)),
})
const throwingIdentities = [
  ['top-level getter', (variant) => Object.defineProperty(
    { ok: true, result: { new_version: completed, ...readableFlag(variant) } },
    'tool', { enumerable: true, get: identityThrow })],
  ['nested getter', (variant) => ({ tool: 'drawing.write', result: Object.defineProperty(
    { ...readableFlag(variant) }, 'tool', { enumerable: true, get: identityThrow }) })],
  ['top-level proxy', (variant) => explodingTool('tool',
    { tool: 'drawing.write', result: { ...readableFlag(variant) } })],
  ['nested proxy', (variant) => ({ tool: 'drawing.write',
    result: explodingTool('tool', { ...readableFlag(variant) }) })],
]
const legacyIdentityBlock = [
  '    if (projectModeRef.current) {',
  "      const solarIdentity = [envelope?.tool, envelope?.result?.tool].some((value) => typeof value === 'string'",
  "        && value.trim() && value.trim().toLowerCase().startsWith('solar-'))",
  '      if (solarIdentity) return false',
  '    }',
].join('\n')
const guardedIdentityBlock = [
  '    if (projectModeRef.current) {',
  '      const solarIdentity = [() => envelope?.tool, () => envelope?.result?.tool].some((read) => {',
  '        try {',
  '          const value = read()',
  "          return typeof value === 'string' && Boolean(value.trim())",
  "            && value.trim().toLowerCase().startsWith('solar-')",
  '        } catch {',
  '          return false',
  '        }',
  '      })',
  '      if (solarIdentity) return false',
  '    }',
].join('\n')

it("R13G-08e an identity read that throws runs main's legacy body in a project and standalone", async () => {
  const exercise = async (product) => {
    for (const [, make] of throwingIdentities) {
      for (const variant of legacyVariants) await assertLegacyParity(make, variant, product)
    }
  }
  await exercise(source)
  expect(source.split(guardedIdentityBlock)).toHaveLength(2)
  const unguarded = changedCompletion(guardedIdentityBlock, legacyIdentityBlock)
  await expect(exercise(unguarded)).rejects.toThrow()
})

it('R13G-08f a readable Solar identity is refused even when the other identity read throws', async () => {
  const exercise = async (product) => {
    const envelopes = [
      { tool: 'solar-settings', result: Object.defineProperty({}, 'tool', { get: identityThrow }) },
      Object.defineProperty({ result: { tool: ' Solar-Settings ' } }, 'tool', { get: identityThrow }),
      explodingTool('tool', { tool: 'drawing.write', result: { tool: 'solar-settings' } }),
      { tool: 'solar-settings', result: explodingTool('tool', {}) },
    ]
    for (const envelope of envelopes) {
      for (const variant of [{}, { mock: true }]) {
        const h = completionHarness(true, variant, product)
        expect(await h.complete(completed, envelope)).toBe(false)
        expect(h.work).toEqual([])
        expect(h.runtime.complete).not.toHaveBeenCalled()
      }
    }
  }
  await exercise(source)
  const oneTry = changedCompletion(guardedIdentityBlock, [
    '    if (projectModeRef.current) {',
    '      let solarIdentity = false',
    '      try {',
    "        solarIdentity = [envelope?.tool, envelope?.result?.tool].some((value) => typeof value === 'string'",
    "          && value.trim() && value.trim().toLowerCase().startsWith('solar-'))",
    '      } catch {',
    '        solarIdentity = false',
    '      }',
    '      if (solarIdentity) return false',
    '    }',
  ].join('\n'))
  await expect(exercise(oneTry)).rejects.toThrow()
})

it('R13G-08g a tagged completion whose metadata cannot be read is refused without the runtime', async () => {
  const exercise = async (product) => {
    const outcome = { ok: true, tool: 'solar-settings', result: { canonical: true } }
    for (const project of [true, false]) {
      const h = completionHarness(project, {}, product)
      const options = Object.defineProperty({}, 'projectSolarCompletion', { enumerable: true, get: identityThrow })
      expect(await h.complete(null, outcome, options)).toBe(false)
      expect(h.runtime.complete).not.toHaveBeenCalled()
      expect(h.work).toEqual([])
    }
  }
  await exercise(source)
  const unguarded = changedCompletion([
    '      let metadata',
    '      try {',
    '        metadata = options.projectSolarCompletion',
    '      } catch {',
    '        return false',
    '      }',
    '      return projectSolarRuntime.complete(envelope, metadata)',
  ].join('\n'), '      return projectSolarRuntime.complete(envelope, options.projectSolarCompletion)')
  await expect(exercise(unguarded)).rejects.toThrow()
})

it("R13G-08h options whose own-property check throws keep main's legacy body in a project and standalone", async () => {
  const exercise = async (product) => {
    for (const envelope of [{ tool: 'drawing.write', result: {} }, { ok: true, result: { new_version: completed } }]) {
      for (const variant of legacyVariants) {
        const options = new Proxy(variant.quiet ? { announce: false } : {}, {
          getOwnPropertyDescriptor: () => { throw new Error('descriptor trap exploded') },
        })
        await assertLegacyParity(envelope, { ...variant, options }, product)
      }
    }
  }
  await exercise(source)
  const unguarded = changedCompletion([
    '    let canonical = false',
    '    try {',
    "      canonical = Object.prototype.hasOwnProperty.call(options ?? {}, 'projectSolarCompletion')",
    '    } catch {',
    '      canonical = false',
    '    }',
  ].join('\n'), "    const canonical = Object.prototype.hasOwnProperty.call(options ?? {}, 'projectSolarCompletion')")
  await expect(exercise(unguarded)).rejects.toThrow()
})

function extractEngineSaved(product) {
  const declaration = 'const onEngineSaved ='
  const next = 'const engineScope ='
  expect(product.split(declaration)).toHaveLength(2)
  expect(product.split(next)).toHaveLength(2)
  const start = product.indexOf(declaration)
  const end = product.indexOf(next, start)
  expect(end).toBeGreaterThan(start)
  return product.slice(start, end) + '\nreturn onEngineSaved'
}

it("R13G-08d an untagged completion with no readable identity runs main's legacy body in a project", async () => {
  const exercise = async (product) => {
    const unreadableIdentities = [undefined, null, '', ' \t\n ', 0, false, {}, ['solar-settings']]
    const envelopes = [undefined, null, {}, { result: {} }, { ok: true, result: { new_version: completed } }]
    for (const tool of unreadableIdentities) {
      for (const resultTool of unreadableIdentities) envelopes.push({ tool, result: { tool: resultTool } })
    }
    for (const envelope of envelopes) {
      for (const variant of legacyVariants) await assertLegacyParity(envelope, variant, product)
    }
    const engine = extractEngineSaved(product)
    for (const variant of legacyVariants) {
      const runs = []
      for (const project of [false, true]) {
        const h = completionHarness(project, variant, product)
        const forward = vi.fn((...args) => { h.pending = h.complete(...args); return h.pending })
        const onSaved = new Function('completedVersionRef', engine)({ current: forward })
        expect(onSaved({ new_version: completed })).toBeUndefined()
        expect(forward).toHaveBeenCalledExactlyOnceWith(completed, { result: {} })
        expect(forward.mock.calls[0][0]).toBe(completed)
        runs.push({ result: await h.pending, work: h.work })
        expect(h.runtime.complete).not.toHaveBeenCalled()
        expect(h.work[0]).toEqual(['scopeRead'])
        forward.mockClear()
        expect(onSaved({})).toBeUndefined()
        expect(forward).not.toHaveBeenCalled()
      }
      expect(runs[1]).toEqual(runs[0])
      expect(runs[1].result).toBe(!(variant.stale || variant.readFailure || variant.mockFailure))
    }
  }
  await exercise(source)
  const mutation = changedCompletion('    if (projectModeRef.current) {',
    "    if (projectModeRef.current) {\n      if (typeof envelope?.tool !== 'string' || !envelope.tool.trim()) return false")
  await expect(exercise(mutation)).rejects.toThrow()
})
