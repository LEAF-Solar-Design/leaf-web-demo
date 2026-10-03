// @vitest-environment node
import { readFileSync } from 'node:fs'
import esbuild from 'esbuild'
import { describe, expect, it, vi } from 'vitest'
import createDrawingUploadController from './controllers/upload/createDrawingUploadController.js'
import { identityFromUploadReceipt, seedDrawingIdentity } from './drawing/drawingIdentity.js'
import { PROFILE_REASONS, profileRibbonTabs } from './lib/ribbonClusters.js'

const source = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8').replace(/\r\n/g, '\n')
const compile = (text) => esbuild.transformSync(text, { loader: 'jsx' }).code
const adapterCode = compile(source.slice(source.indexOf('function promoteStandaloneUpload('), source.indexOf('function ConsoleDrawingObjects(')))
const contextCode = source.slice(source.indexOf('  const standaloneMounted ='), source.indexOf('  useEffect(() => {', source.indexOf('  const standaloneMounted =')))
const filesCode = source.slice(source.indexOf('      files: { onUpload:'), source.indexOf('\n', source.indexOf('      files: { onUpload:'))).trim().slice('files: '.length).replace(/,$/, '')
const controlSource = readFileSync(new URL('./components/DrawingUploadControl.jsx', import.meta.url), 'utf8')
const React = { createElement: (type, props, ...children) => ({ type, props: { ...props, children } }) }
const renderControl = new Function('React', 'useRef', compile(controlSource.replace(/^import .*$/m, '').replace('export default function', 'function')) + '\nreturn DrawingUploadControl')(
  React, (value) => ({ current: value }),
)
const nodes = (node) => !node || typeof node !== 'object' ? [] : [node, ...(node.props?.children || []).flatMap(nodes)]
const text = (node) => typeof node === 'string' ? node : (node?.props?.children || []).map(text).join('')
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const tick = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve() }
const file = { name: 'panel.dxf', size: 32 }
const receipt = (id) => ({ drawing_id: id, status: 'ready', tenant_kind: 'account' })

// Small hook seams execute the extracted production adapters and App guards.
// Upload state and generation semantics always come from the real controller.
function hooks() {
  const slots = []
  let cursor = 0, pending = [], dirty = false
  const same = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]))
  const useMemo = (fn, deps) => {
    const index = cursor++
    if (!slots[index] || !same(slots[index].deps, deps)) slots[index] = { deps, value: fn() }
    return slots[index].value
  }
  return {
    useMemo, useCallback: (fn, deps) => useMemo(() => fn, deps),
    useRef: (value) => { const index = cursor++; return (slots[index] ||= { current: value }) },
    useState: (value) => {
      const index = cursor++
      const slot = slots[index] ||= { value }
      slot.set ||= (next) => { slot.value = next; dirty = true }
      return [slot.value, slot.set]
    },
    useLayoutEffect: (fn, deps) => {
      const index = cursor++
      if (!slots[index] || !same(slots[index].deps, deps)) {
        const previous = slots[index]
        slots[index] = { deps, cleanup: previous?.cleanup }
        pending.push(() => { previous?.cleanup?.(); slots[index].cleanup = fn() })
      }
    },
    begin: () => { cursor = 0; dirty = false },
    commit: () => { const effects = pending; pending = []; effects.forEach((fn) => fn()) },
    dirty: () => dirty,
    dispose: () => { slots.forEach((slot) => slot.cleanup?.()); pending = [] },
  }
}

function harness(serviceOverrides = {}, initial = {}) {
  const appHooks = hooks()
  let adapterHooks = hooks(), onControllerReady, controller = null, actions = null, element = null, app = null
  let input = { studioGround: {}, surfaceSlots: { ground: 'board' }, mock: false, openProjectId: null,
    signedIn: false, projectPane: null, REQUESTED_DRAWING_ID: 'demo', DRAWING_SOURCE: 'rooftop_demo', ...initial }
  const setFromUpload = vi.fn((value) => {
    const identity = identityFromUploadReceipt(value)
    input = { ...input, REQUESTED_DRAWING_ID: identity.drawingId, DRAWING_SOURCE: identity.source }
  })
  const services = { policy: vi.fn(async () => ({ enabled: true })), upload: vi.fn(async () => receipt('new')),
    status: vi.fn(async () => ({ status: 'ready' })), intake: vi.fn(async () => ({ documentId: 'new-v1.dxf' })),
    wait: vi.fn(async () => {}), ...serviceOverrides }
  const makeAdapter = () => new Function('React', 'useRef', 'useLayoutEffect', 'useDrawingUploadController', 'DrawingUploadControl', 'PROFILE_REASONS',
    adapterCode + '\nreturn { StandaloneMaterialUpload, promoteStandaloneUpload }')(
    React, adapterHooks.useRef, adapterHooks.useLayoutEffect, (options) => {
      onControllerReady = options.onReady
      if (!controller) {
        controller = createDrawingUploadController({ services, onReady: (result) => onControllerReady(result) })
        actions = { upload: controller.upload, cancel: vi.fn(controller.cancel), setEngine: controller.setEngine }
        controller.start()
        controller.loadPolicy()
      }
      return { ...controller.getSnapshot(), getSnapshot: controller.getSnapshot, actions }
    }, 'DrawingUploadControl', PROFILE_REASONS,
  )
  let adapter = makeAdapter()
  const project = new Function('studioGround', 'surfaceSlots', 'mock', 'openProjectId', 'REQUESTED_DRAWING_ID', 'DRAWING_SOURCE', 'projectPane', 'setFromUpload',
    'useMemo', 'useRef', 'useState', 'useCallback', 'useLayoutEffect', 'promoteStandaloneUpload',
    contextCode + '\nreturn { standaloneMounted, standaloneToken, standaloneSelection, standalonePolicyReady, getStandaloneContext, publishStandalonePolicy, onStandaloneReady }')
  const files = new Function('mock', 'openProjectId', 'signedIn', 'standalonePolicyReady', 'setProjectPane', 'return ' + filesCode)
  const render = (patch = {}, { adapterRender = true, commit = true } = {}) => {
    input = { ...input, ...patch }
    for (let pass = 0; pass < 3; pass += 1) {
      appHooks.begin()
      app = project(input.studioGround, input.surfaceSlots, input.mock, input.openProjectId, input.REQUESTED_DRAWING_ID, input.DRAWING_SOURCE, input.projectPane,
        setFromUpload, appHooks.useMemo, appHooks.useRef, appHooks.useState, appHooks.useCallback, appHooks.useLayoutEffect, adapter.promoteStandaloneUpload)
      if (adapterRender) {
        if (app.standaloneMounted) {
          adapterHooks.begin()
          element = adapter.StandaloneMaterialUpload({ token: app.standaloneToken, pane: input.projectPane, selection: app.standaloneSelection,
            getContext: app.getStandaloneContext, onPolicy: app.publishStandalonePolicy, onReady: app.onStandaloneReady,
            onBack: () => render({ projectPane: null }) })
          if (commit) adapterHooks.commit()
        } else if (controller) {
          adapterHooks.dispose(); controller.dispose(); controller = null; actions = null
          adapterHooks = hooks(); adapter = makeAdapter(); element = null
        }
      }
      if (commit) appHooks.commit()
      if (!appHooks.dirty()) break
    }
    return element
  }
  const ribbon = () => {
    const binding = files(input.mock, input.openProjectId, input.signedIn, app.standalonePolicyReady, (pane) => render({ projectPane: pane }))
    return profileRibbonTabs('project', { files: binding })[0].clusters.flatMap((cluster) => cluster.tools).find((tool) => tool.id === 'files:upload')
  }
  const control = () => nodes(element).find((node) => node.type === 'DrawingUploadControl')?.props
  render()
  return { services, setFromUpload, render, ribbon, control, get app() { return app }, get element() { return element },
    get controller() { return controller }, get actions() { return actions }, get input() { return input },
    readyCallback: () => onControllerReady,
    open: () => ribbon().onClick(),
    dispose: () => { adapterHooks.dispose(); controller?.dispose(); appHooks.dispose() },
  }
}

describe('console standalone upload', () => {
  it.each([false, true])('CSU-V1 policy permits standalone upload with signedIn=%s', async (signedIn) => {
    const policy = deferred()
    const h = harness({ policy: vi.fn(() => policy.promise) }, { signedIn })
    expect(h.ribbon().disabled).toBe(true)
    expect(h.element).toBeNull()
    policy.resolve({ enabled: true }); await tick(); h.render()
    expect(h.ribbon().disabled).toBe(false)
    h.open()
    expect(nodes(h.element).filter((node) => node.type === 'DrawingUploadControl')).toHaveLength(1)
    const rendered = renderControl(h.control())
    expect(nodes(rendered).find((node) => node.type === 'input').props['aria-label']).toBe('Drawing file')
    expect(nodes(rendered).find((node) => node.type === 'button' && text(node) === 'Upload DWG or DXF').props.disabled).toBe(false)
    expect(h.services.upload).not.toHaveBeenCalled()
    expect(source).toContain("pane={standaloneMounted && projectPane === 'material' ? null : projectPane}")
    h.dispose()
  })

  it.each([undefined, null, {}, { enabled: false }, { enabled: 'true' }, { enabled: 1 }, { enabled: null }, 'reject'])('CSU-V2 unavailable policy %j fails closed and refuses file selection', async (policyValue) => {
    const policy = deferred()
    const h = harness({ policy: vi.fn(() => policy.promise) }, { projectPane: 'material' })
    const assertRefused = () => {
      h.render()
      expect(h.ribbon()).toMatchObject({ disabled: true, reason: 'Drawing upload is unavailable in this session' })
      expect(text(h.element)).toContain(PROFILE_REASONS.uploadDrawing)
      const rendered = renderControl(h.control())
      expect(nodes(rendered).find((node) => node.type === 'input').props.disabled).toBe(true)
      expect(nodes(rendered).find((node) => node.type === 'button' && text(node) === 'Upload DWG or DXF').props.disabled).toBe(true)
      h.control().onUpload(file)
      expect(h.services.upload).not.toHaveBeenCalled()
    }
    assertRefused()
    if (policyValue === 'reject') policy.reject(new Error('Policy unavailable.'))
    else policy.resolve(policyValue)
    await tick(); assertRefused()
    h.dispose()
    const refresh = deferred()
    const enabled = harness({ policy: vi.fn().mockResolvedValueOnce({ enabled: true }).mockImplementation(() => refresh.promise) }, { projectPane: 'material' })
    await tick(); enabled.render(); expect(enabled.ribbon().disabled).toBe(false)
    const refreshing = enabled.controller.loadPolicy(); enabled.render()
    expect(enabled.ribbon().disabled).toBe(true)
    expect(enabled.control().disabled).toBe(true)
    enabled.control().onUpload(file); expect(enabled.services.upload).not.toHaveBeenCalled()
    refresh.resolve({ enabled: true }); await refreshing; enabled.dispose()
  })

  it('CSU-V3 waits for intake and promotes the receipt once before Drawing ready', async () => {
    const intake = deferred()
    const h = harness({ upload: vi.fn(async () => ({ ...receipt('uploaded'), status: 'extracting' })), intake: vi.fn(() => intake.promise) })
    await tick(); h.render(); h.open()
    const run = h.control().onUpload(file); await tick(); h.render()
    expect(h.services.status).toHaveBeenCalledOnce()
    expect(h.controller.getSnapshot().phase).toBe('loading')
    expect(h.setFromUpload).not.toHaveBeenCalled()
    intake.resolve({ documentId: 'uploaded-v1.dxf' }); await run; h.render()
    expect(h.setFromUpload).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ drawing_id: 'uploaded', status: 'extracting' }))
    expect(h.input).toMatchObject({ REQUESTED_DRAWING_ID: 'uploaded', DRAWING_SOURCE: 'uploaded', projectPane: 'material' })
    expect(text(renderControl(h.control()))).toContain('Drawing ready')
    h.readyCallback()({ receipt: receipt('duplicate') })
    expect(h.setFromUpload).toHaveBeenCalledOnce()
    h.dispose()
  })

  it.each(['upload', 'status', 'intake'])('CSU-V4 %s refusal exposes error and preserves prior selection', async (boundary) => {
    const overrides = { upload: vi.fn(async () => ({ ...receipt('failed'), status: 'extracting' })) }
    overrides[boundary] = vi.fn(async () => { throw new Error('Drawing upload refused.') })
    const h = harness(overrides)
    await tick(); h.render(); h.open(); await h.control().onUpload(file); h.render()
    expect(h.control()).toMatchObject({ phase: 'failed', error: 'Drawing upload refused.' })
    expect(text(renderControl(h.control()))).toContain('Drawing upload refused.')
    expect(text(renderControl(h.control()))).not.toContain('Drawing ready')
    expect(h.setFromUpload).not.toHaveBeenCalled()
    expect(h.input.REQUESTED_DRAWING_ID).toBe('demo')
    h.dispose()
  })

  it.each([undefined, {}, { drawing_id: '' }, { drawing_id: 42 }, { drawing_id: null }])('CSU-V5 malformed readiness receipt %j preserves selection', async (value) => {
    const upload = deferred()
    const h = harness({ upload: vi.fn(() => upload.promise) })
    await tick(); h.render(); h.open()
    const run = h.control().onUpload(file)
    h.readyCallback()({ receipt: value, status: { status: 'ready' }, view: { drawing_id: 'wrong' } })
    expect(h.setFromUpload).not.toHaveBeenCalled()
    expect(h.input.REQUESTED_DRAWING_ID).toBe('demo')
    h.actions.cancel(); upload.resolve(receipt('late')); await run; h.dispose()
  })

  it.each(['upload', 'status', 'intake'])('CSU-V6 cancel A at %s then B alone promotes; busy refuses a second file', async (boundary) => {
    const a = deferred()
    const overrides = { upload: vi.fn().mockResolvedValueOnce({ ...receipt('A'), status: boundary === 'status' ? 'extracting' : 'ready' }).mockResolvedValue(receipt('B')) }
    if (boundary === 'upload') overrides.upload = vi.fn().mockImplementationOnce(() => a.promise).mockResolvedValue(receipt('B'))
    else overrides[boundary] = vi.fn().mockImplementationOnce(() => a.promise).mockResolvedValue(boundary === 'status' ? { status: 'ready' } : {})
    const h = harness(overrides)
    await tick(); h.render(); h.open()
    const runA = h.control().onUpload(file); await tick(); h.render()
    expect(h.control().disabled).toBe(true)
    h.control().onUpload({ ...file, name: 'second.dxf' })
    expect(h.services.upload).toHaveBeenCalledOnce()
    h.control().onCancel(); h.render()
    await h.control().onUpload(file); h.render()
    expect(h.setFromUpload).toHaveBeenCalledExactlyOnceWith(receipt('B'))
    a.resolve(boundary === 'upload' ? receipt('A') : boundary === 'status' ? { status: 'ready' } : {})
    await runA; h.render()
    expect(h.setFromUpload).toHaveBeenCalledOnce()
    expect(h.controller.getSnapshot()).toMatchObject({ phase: 'ready', receipt: receipt('B') })
    expect(h.input.REQUESTED_DRAWING_ID).toBe('B')
    h.dispose()
  })

  it.each(['back', 'tools', 'before-cleanup'])('CSU-V7 %s closes the attempt and reopening never revives A', async (close) => {
    const intake = deferred()
    const h = harness({ intake: vi.fn(() => intake.promise) })
    await tick(); h.render(); h.open()
    const run = h.control().onUpload(file); await tick()
    if (close === 'back') nodes(h.element).find((node) => node.type === 'button' && text(node) === 'Back to board').props.onClick()
    else if (close === 'before-cleanup') {
      h.render({ projectPane: 'tools' }, { adapterRender: false, commit: false })
      h.readyCallback()({ receipt: receipt('A'), status: { status: 'ready' }, view: {} })
      expect(h.setFromUpload).not.toHaveBeenCalled()
      h.render()
    } else h.render({ projectPane: 'tools' })
    expect(h.element).toBeNull()
    expect(h.actions.cancel).toHaveBeenCalledOnce()
    h.render({ projectPane: 'material' })
    intake.resolve({}); await run; h.render()
    expect(h.setFromUpload).not.toHaveBeenCalled()
    expect(h.controller.getSnapshot().phase).toBe('idle')
    h.dispose()
  })

  it.each(['project', 'selection', 'surface', 'mock', 'replacement', 'unmount'])('CSU-V8 current %s guard rejects A before adapter cleanup', async (change) => {
    const intake = deferred()
    const h = harness({ intake: vi.fn(() => intake.promise) })
    await tick(); h.render(); h.open()
    const run = h.control().onUpload(file); await tick()
    const callback = h.readyCallback()
    const attempt = { token: h.app.standaloneToken, selection: h.app.standaloneSelection }
    const patch = change === 'project' ? { openProjectId: 'P' } : change === 'selection' ? { REQUESTED_DRAWING_ID: 'C', DRAWING_SOURCE: 'C' }
      : change === 'mock' ? { mock: true } : { surfaceSlots: { ground: 'drawing' } }
    if (change === 'unmount') h.dispose()
    else {
      h.render(patch, { adapterRender: false, commit: false })
      if (change === 'replacement') h.render({ surfaceSlots: { ground: 'board' } }, { adapterRender: false, commit: false })
    }
    const completion = { receipt: receipt('A'), status: { status: 'ready' }, view: {} }
    h.app.onStandaloneReady(completion, attempt)
    callback(completion)
    expect(h.setFromUpload).not.toHaveBeenCalled()
    if (change === 'project') { expect(h.input.openProjectId).toBe('P'); expect(h.ribbon().disabled).toBe(true) }
    if (change === 'selection') expect(h.input.REQUESTED_DRAWING_ID).toBe('C')
    intake.resolve({}); await run
    expect(h.setFromUpload).not.toHaveBeenCalled()
    h.dispose()
  })

  it('CSU-V9 mock material keeps its existing refusal and mounts no upload controller', () => {
    const h = harness({}, { mock: true, projectPane: 'material' })
    expect(h.controller).toBeNull()
    expect(h.services.policy).not.toHaveBeenCalled()
    expect(h.ribbon().disabled).toBe(true)
    expect(h.setFromUpload).not.toHaveBeenCalled()
    expect(source).toContain('? <ProjectMaterialIntake project={null} mock={mock} artifacts={[]} />')
    const material = readFileSync(new URL('./workspace/ProjectMaterialIntake.jsx', import.meta.url), 'utf8')
    expect(material).toContain('Uploads are unavailable in this demo.')
    expect(material).toContain('disabled={!project || mock === true}')
    h.dispose()
  })

  it.each([null, 'remembered-operator'])('CSU-V10 fresh console ignores remembered operator drawing %s', (liveId) => {
    const identity = seedDrawingIdentity({ mode: 'console', liveId })
    expect(identity).toMatchObject({ source: 'rooftop_demo', drawingId: 'demo' })
    const h = harness({}, { surfaceSlots: { ground: 'drawing' }, REQUESTED_DRAWING_ID: identity.drawingId, DRAWING_SOURCE: identity.source })
    expect(h.controller).toBeNull()
    expect(h.services.upload).not.toHaveBeenCalled()
    expect(h.setFromUpload).not.toHaveBeenCalled()
    h.dispose()
  })

  it('CSU-V11 project gate and begin-before-upload attachment path remain independent', () => {
    const h = harness({}, { openProjectId: 'P', signedIn: true })
    expect(h.ribbon().disabled).toBe(false)
    h.open(); expect(h.controller).toBeNull()
    const events = [], onAttached = vi.fn()
    const upload = { actions: { upload: (value) => events.push(['upload', value]) } }
    const intake = { begin: (value) => { events.push(['begin', value]); return true }, retry: vi.fn() }
    const code = compile(source.slice(source.indexOf('function LiveProjectMaterialIntake('), source.indexOf('function promoteStandaloneUpload(')))
    const live = new Function('React', 'ProjectMaterialIntake', 'useDrawingUploadController', 'useMaterialIntake', code + '\nreturn LiveProjectMaterialIntake')(
      React, 'ProjectMaterialIntake', () => upload, (options) => { expect(options).toEqual({ upload, onAttached }); return intake },
    )({ project: { project_id: 'P', name: 'Project P' }, artifacts: [], onAttached })
    live.props.onStartUpload(file)
    expect(events).toEqual([['begin', { projectId: 'P', projectName: 'Project P', fileName: file.name }], ['upload', file]])
    h.render({ signedIn: false }); expect(h.ribbon().disabled).toBe(true)
    expect(h.setFromUpload).not.toHaveBeenCalled()
    expect(source).toContain('key={openProjectId}')
    expect(source).toContain('onAttached={rehydrate}')
    h.dispose()
  })

  it('CSU-V12 policy loads with pane closed and stale adapters cannot enable re-entry', async () => {
    const oldPolicy = deferred(), newPolicy = deferred()
    const policy = vi.fn().mockImplementationOnce(() => oldPolicy.promise).mockImplementationOnce(() => newPolicy.promise)
    const h = harness({ policy })
    expect(policy).toHaveBeenCalledOnce()
    expect(h.element).toBeNull()
    const oldToken = h.app.standaloneToken, publish = h.app.publishStandalonePolicy
    h.render({ surfaceSlots: { ground: 'drawing' } })
    expect(h.controller).toBeNull()
    h.render({ surfaceSlots: { ground: 'board' } })
    expect(policy).toHaveBeenCalledTimes(2)
    expect(h.app.standaloneToken).not.toBe(oldToken)
    publish(oldToken, true); oldPolicy.resolve({ enabled: true }); await tick(); h.render()
    expect(h.ribbon().disabled).toBe(true)
    newPolicy.resolve({ enabled: true }); await tick(); h.render()
    expect(h.ribbon().disabled).toBe(false)
    expect(h.services.upload).not.toHaveBeenCalled()
    for (const surface of ['cad', 'solar']) {
      const drawing = harness({}, { surfaceSlots: { ground: 'drawing' }, activeSurface: surface })
      expect(drawing.services.policy).not.toHaveBeenCalled(); drawing.dispose()
    }
    h.dispose()
  })
})
