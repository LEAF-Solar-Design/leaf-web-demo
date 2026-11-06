import React, { useLayoutEffect, useMemo, useRef } from 'react'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import useMaterialIntake from './workspace/useMaterialIntake.js'
import { createProjectDrawingEditor, projectDrawingEditorScopeKey } from './cadedit/projectDrawingEditor.js'
import { createProjectDrawingEditorTransport, importUploadedDrawingVersion } from './api.js'

vi.mock('./api.js', async (original) => ({ ...await original(), importUploadedDrawingVersion: vi.fn() }))
afterEach(cleanup)
beforeEach(() => vi.clearAllMocks())

const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
function productionFunction(name, environment = {}) {
  const declaration = tree.program.body.map((node) => node.declaration || node)
    .find((node) => node.type === 'FunctionDeclaration' && node.id.name === name)
  if (!declaration) throw new Error(`Production function missing: ${name}`)
  return new Function(...Object.keys(environment), `${source.slice(declaration.start, declaration.end)}; return ${name}`)(...Object.values(environment))
}
function productionCallback(name, environment) {
  let callback
  function walk(node) {
    if (!node || typeof node !== 'object') return
    if (node.type === 'VariableDeclarator' && node.id?.name === name) callback = node.init.arguments[0]
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value && typeof value === 'object') walk(value)
    }
  }
  walk(tree)
  return new Function(...Object.keys(environment), `return (${source.slice(callback.start, callback.end)})`)(...Object.values(environment))
}
const deriveScope = productionFunction('projectEditorScope')
const seat = productionFunction('seatProjectDrawingPublication')
const O = '10000000-0000-4000-8000-000000000001'
const P = '20000000-0000-4000-8000-000000000001'
const Q = '20000000-0000-4000-8000-000000000002'
const D = '30000000-0000-4000-8000-000000000001'
const V = '40000000-0000-4000-8000-000000000001'
const R = '50000000-0000-4000-8000-000000000001'
const cap = `lco1.${'a'.repeat(64)}`
const scope = (project = P) => ({ organization_id: O, project_id: project, drawing_id: null, input_version_id: null })
const version = { org_id: O, project_id: P, drawing_id: D, version_id: V, seq: 1 }

describe('BSP53', () => {
it('derives initial project custody without inheriting a standalone drawing', () => {
  const workspace = { project: { project_id: P, org_id: O }, drawing_versions: [] }
  expect(deriveScope({ enabled: true, orgId: O, projectId: P, workspace, versionId: null })).toEqual(scope())
  expect(deriveScope({ enabled: true, orgId: O, projectId: Q, workspace, versionId: null })).toBeNull()
  expect(deriveScope({ enabled: true, orgId: O, projectId: P, workspace, versionId: V })).toBeNull()
  expect(deriveScope({ enabled: false, orgId: O, projectId: P, workspace })).toBeNull()
})

it('seats only the returned same-project version after fresh workspace hydration', async () => {
  const controller = { getSnapshot: () => ({ openProjectId: P, orgId: O }),
    rehydrate: vi.fn(async () => ({ drawing_versions: [version] })), selectCanonicalVersion: vi.fn() }
  expect(await seat(controller, { drawing_version: version })).toBe(true)
  expect(controller.selectCanonicalVersion).toHaveBeenCalledExactlyOnceWith(V)
  controller.selectCanonicalVersion.mockClear()
  let finish
  controller.rehydrate.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const pending = seat(controller, { drawing_version: version })
  controller.getSnapshot = () => ({ openProjectId: Q, orgId: O })
  finish({ drawing_versions: [version] })
  expect(await pending).toBe(false)
  expect(controller.selectCanonicalVersion).not.toHaveBeenCalled()
})

it('a same-project version selection during readback cannot be overwritten by an older save', async () => {
  let current = { openProjectId: P, orgId: O, canonicalVersionId: null }
  let finish
  const controller = { getSnapshot: () => current, rehydrate: () => new Promise((resolve) => { finish = resolve }),
    selectCanonicalVersion: vi.fn() }
  const pending = seat(controller, { drawing_version: version })
  current = { ...current, canonicalVersionId: '40000000-0000-4000-8000-000000000002' }
  finish({ drawing_versions: [version] })
  expect(await pending).toBe(false)
  expect(controller.selectCanonicalVersion).not.toHaveBeenCalled()
})

it('the App project branch cannot obtain the legacy standalone save target', () => {
  let target
  function walk(node) {
    if (!node || typeof node !== 'object') return
    if (node.type === 'VariableDeclarator' && node.id?.name === 'engineSaveTarget') target = node.init
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value && typeof value === 'object') walk(value)
    }
  }
  walk(tree)
  const evaluate = new Function('mock', 'projectMode', 'intake', `return (${source.slice(target.start, target.end)})`)
  expect(evaluate(false, true, { drawing_id: 'demo' })).toBeNull()
  expect(evaluate(true, false, { drawing_id: 'demo' })).toBeNull()
})

it('project History and recovery use project versions without reading or mutating demo', async () => {
  const setProjectPane = vi.fn(), setBrowserPanel = vi.fn(), legacyHistory = vi.fn(), legacyUndo = vi.fn()
  productionCallback('onToggleHistoryTracked', { projectModeRef: { current: true }, setProjectPane,
    setBrowserPanel, onToggleHistory: legacyHistory })()
  expect(setProjectPane).toHaveBeenCalledWith('versions')
  expect(setBrowserPanel).toHaveBeenCalledWith('project')
  expect(legacyHistory).not.toHaveBeenCalled()
  const previous = { ...version, version_id: '40000000-0000-4000-8000-000000000002', seq: 1 }
  const current = { ...version, seq: 2 }
  const restored = { ...version, version_id: R, seq: 3 }
  const state = { openProjectId: P, orgId: O, canonicalVersionId: V,
    workspace: { drawing_versions: [previous, current, { ...previous, drawing_id: Q, seq: 0 }] } }
  const workspaceController = { getSnapshot: () => state, rehydrate: async () => ({ drawing_versions: [restored] }),
    selectCanonicalVersion: vi.fn() }
  const projectEditorTarget = { restoreVersion: vi.fn(async () => ({ drawing_version: restored })) }
  const recover = productionCallback('undoCurrentVersion', { projectModeRef: { current: true }, workspaceController,
    projectEditorTarget, seatProjectDrawingPublication: seat, showToast: vi.fn(), humanizeError: String,
    undoDrawingVersion: legacyUndo })
  await recover()
  expect(projectEditorTarget.restoreVersion).toHaveBeenCalledExactlyOnceWith(previous.version_id)
  expect(workspaceController.selectCanonicalVersion).toHaveBeenCalledWith(R)
  expect(legacyUndo).not.toHaveBeenCalled()
})

function bridgeHarness() {
  const ready = new Set()
  let completeUpload
  const upload = { getSnapshot: () => ({ busy: false, phase: 'idle' }),
    subscribeReady: (listener) => { ready.add(listener); return () => ready.delete(listener) },
    actions: { setEngine: vi.fn(), cancel: vi.fn(), upload: vi.fn(async () => {
      await new Promise((resolve) => { completeUpload = resolve })
      const result = { receipt: { drawing_id: 'u-1234567890' }, status: { extracted_version: 1 } }
      await Promise.all([...ready].map((listener) => listener(result)))
      return result
    }) } }
  const Bridge = productionFunction('ProjectDrawingEditorBridge', {
    useRef, useMemo, useLayoutEffect, useDrawingUploadController: () => upload,
    useMaterialIntake, createProjectDrawingEditor, projectDrawingEditorScopeKey,
    createProjectDrawingEditorTransport,
  })
  const transport = { read: vi.fn(), save: vi.fn(), restore: vi.fn(), acquireCheckout: vi.fn(), releaseCheckout: vi.fn() }
  let target
  const onTarget = (next) => { target = next }
  const view = render(<Bridge scope={scope()} onTarget={onTarget} transport={transport} />)
  return { view, Bridge, transport, onTarget, upload, target: () => target, finish: () => completeUpload() }
}

it('project Open DXF uses the actual upload/adoption hook before returning canonical custody', async () => {
  vi.mocked(importUploadedDrawingVersion).mockResolvedValue({ drawingVersion: version, replayed: false })
  const h = bridgeHarness()
  let pending
  act(() => { pending = h.target().openFile(new File(['DXF'], 'roof.dxf')) })
  await act(async () => { h.finish(); await pending })
  expect(await pending).toMatchObject({ drawing_version: version, projectOperation: 'import' })
  expect(h.upload.actions.setEngine).toHaveBeenCalledWith('local')
  expect(importUploadedDrawingVersion).toHaveBeenCalledExactlyOnceWith(P,
    { drawingId: 'u-1234567890', version: 1, name: 'roof.dxf' }, { idempotencyKey: `b3:${P}:u-1234567890:1` })
  expect(h.transport.save).not.toHaveBeenCalled()
})

it('a project switch retires an in-flight import before it can adopt or reseat material', async () => {
  const h = bridgeHarness()
  let pending
  act(() => { pending = h.target().openFile(new File(['DXF'], 'roof.dxf')) })
  const rejected = expect(pending).rejects.toThrow()
  h.view.rerender(<h.Bridge scope={scope(Q)} onTarget={h.onTarget} transport={h.transport} />)
  await act(async () => { h.finish(); await rejected })
  expect(importUploadedDrawingVersion).not.toHaveBeenCalled()
  expect(h.target().scopeKey).toBe(projectDrawingEditorScopeKey(scope(Q)))
})

it('transports bytes to exact canonical scope and never the standalone versions endpoint', async () => {
  const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ drawing_version: version }), { status: 200 }))
  const api = createProjectDrawingEditorTransport({ fetchImpl })
  const bytes = new Uint8Array([65, 66])
  await api.save({ ...scope(), drawing_id: D, input_version_id: V }, bytes, { requestId: R, capability: cap })
  const [url, request] = fetchImpl.mock.calls[0]
  expect(url).toContain(`/api/projects/${P}/drawings/${D}/versions/${V}/editor`)
  expect(url).not.toContain('/api/drawings/demo')
  expect(request.body).toBe(bytes)
  expect(request.headers).toMatchObject({ 'X-Org-Id': O, 'X-Project-Id': P, 'X-Checkout-Capability': cap, 'Idempotency-Key': R })
  expect(() => api.save(scope(), bytes, { requestId: R, capability: cap })).toThrow()
  expect(fetchImpl).toHaveBeenCalledTimes(1)
})

it('bounds source reads and preserves unknown publication outcome', async () => {
  const api = createProjectDrawingEditorTransport({ fetchImpl: vi.fn(async () => {
    throw new Error('offline')
  }) })
  await expect(api.save({ ...scope(), drawing_id: D, input_version_id: V }, new Uint8Array([1]),
    { requestId: R, capability: cap })).rejects.toMatchObject({ outcomeUnknown: true })
  const huge = createProjectDrawingEditorTransport({ fetchImpl: vi.fn(async () => new Response('x', {
    headers: { 'X-Leaf-Source-Sha256': 'a'.repeat(64), 'content-length': `${17 * 1024 * 1024}` },
  })) })
  await expect(huge.read({ ...scope(), drawing_id: D, input_version_id: V })).rejects.toMatchObject({ reason: 'project_editor_response_invalid' })
})
})
