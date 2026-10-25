import React, { createRef, useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { projectSolarDrawingView } from './solar/solarProjectDrawingModel.js'
import { useAppProjectSolarRuntime } from './solar/useAppProjectSolarRuntime.js'
import { projectSolarAppReason } from './solar/solarProjectAppModel.js'
import { buildDrawingObjectIndex } from './lib/drawingObjectIndex.js'
import { drawingExtents, drawingPropertyName } from './site/PropertiesDock.jsx'
import useDrawingVersionController from './controllers/useDrawingVersionController.js'
import EngineDocumentView from './cadedit/EngineDocumentView.jsx'
import * as engineContext from './cadedit/EngineSessionProvider.jsx'
import Viewer from './components/Viewer.jsx'

// Same mounted-Viewer seam as components/viewerSolarOverlay.test.jsx: retain the real
// imperative state, geometry and effects, replacing only browser graphics facilities.
vi.mock('three', async importOriginal => {
  const actual = await importOriginal()
  class WebGLRenderer {
    constructor() { this.domElement = document.createElement('canvas') }
    setPixelRatio() {} setSize() {} render() {} dispose() {}
  }
  return { ...actual, WebGLRenderer }
})
vi.mock('three/examples/jsm/controls/OrbitControls.js', async () => {
  const { Vector3 } = await vi.importActual('three')
  class OrbitControls {
    constructor(camera, dom) {
      this.object = camera; this.domElement = dom; this.target = new Vector3()
      this.enabled = true; this.mouseButtons = {}; this.touches = {}
    }
    addEventListener() {} removeEventListener() {} update() {} dispose() {}
  }
  return { OrbitControls }
})
afterEach(cleanup)
afterEach(() => vi.unstubAllGlobals())

const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
function find(predicate, root = tree) {
  const found = []
  function walk(node) {
    if (!node || typeof node.type !== 'string') return
    if (predicate(node)) found.push(node)
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value?.type) walk(value)
    }
  }
  walk(root)
  return found
}
function one(predicate, root = tree) {
  const nodes = find(predicate, root)
  expect(nodes, 'unique production extraction').toHaveLength(1)
  return nodes[0]
}
const text = node => source.slice(node.start, node.end)
const decl = name => one(node => node.type === 'VariableDeclarator' && node.id.name === name)
const element = name => one(node => node.type === 'JSXElement' && node.openingElement.name.name === name)
const prop = (node, name) => one(n => n.type === 'JSXAttribute' && n.name.name === name, node.openingElement).value.expression
function evaluate(node, env = {}) {
  if (node.type === 'ConditionalExpression') return evaluate(evaluate(node.test, env) ? node.consequent : node.alternate, env)
  if (node.type === 'LogicalExpression') {
    const left = evaluate(node.left, env)
    if (node.operator === '&&') return left && evaluate(node.right, env)
    if (node.operator === '||') return left || evaluate(node.right, env)
    if (node.operator === '??') return left ?? evaluate(node.right, env)
  }
  if (node.type === 'JSXExpressionContainer') return node.expression.type === 'JSXEmptyExpression' ? null : evaluate(node.expression, env)
  if (node.type === 'JSXText') return /[\r\n]/.test(node.value) ? node.value.trim() || null : node.value
  if (node.type === 'JSXFragment') return React.createElement(React.Fragment, null, ...node.children.map(n => evaluate(n, env)))
  if (node.type === 'JSXElement') {
    const name = node.openingElement.name.name
    const component = /^[a-z]/.test(name) ? name : env[name]
    expect(component, `component binding: ${name}`).toBeTruthy()
    const props = {}
    for (const attr of node.openingElement.attributes) {
      expect(attr.type).toBe('JSXAttribute')
      props[attr.name.name] = attr.value === null ? true : attr.value.type === 'StringLiteral' ? attr.value.value : evaluate(attr.value.expression, env)
    }
    return React.createElement(component, props, ...node.children.map(n => evaluate(n, env)))
  }
  return Function(...Object.keys(env), `return (${text(node)})`)(...Object.values(env))
}
const callback = (name, env) => evaluate(decl(name).init.arguments[0], env)
const U = n => `${String(n).repeat(8)}-${String(n).repeat(4)}-${String(n).repeat(4)}-${String(n).repeat(4)}-${String(n).repeat(12)}`
const B = { kind: 'project', organization_id: U(1), project_id: U(2), drawing_id: U(3), input_version_id: U(4) }
const keyOf = binding => 'project:' + ['organization_id', 'project_id', 'drawing_id', 'input_version_id'].map(k => binding[k]).join(':')
function intakeOf(name = 'canonical.dwg', handle = 'CA') {
  return { dwg: name, layers: ['Panels'], polylines: [{ handle, layer: 'Panels', pts: [[0, 0], [10, 20]] }], inserts: [], faces3d: [] }
}
function graphIntake() {
  return { ...intakeOf(), solar_design_graph_sha256: 'a'.repeat(64), solar_design_graph: {
    graph_schema_version: 1, project: { units: { compute_units: 'm', meters_per_unit: 2 } },
    frames: [], panels: [{ id: 'panel', centre: [2, 4] }],
    strings: [{ id: 'string', ordered_panel_refs: ['panel'], route: [[2, 4], [6, 8]] }], inverters: [], routes: [],
  } }
}
function contextOf(intake, binding = B) {
  const { kind, ...identity } = binding
  return { context: { ...identity, head_version_id: binding.input_version_id, is_head: true, intake_sha256: 'b'.repeat(64) }, intake }
}
function environment(intake = graphIntake(), scene, binding = B) {
  const legacy = intakeOf('legacy.dwg', 'EE')
  legacy.layers = ['Engine', 'Legacy']
  legacy.polylines = [{ handle: 'EE', layer: 'Engine', pts: [[90, 100], [190, 250]] },
    { handle: 'EF', layer: 'Legacy', pts: [[30, 40], [50, 60]] }]
  return { projectMode: true, projectSolar: { scope: { binding } },
    projectSolarRuntime: { contextView: contextOf(intake, binding), contextCurrent: () => true, visibleLayers: {}, setVisibleLayers: vi.fn() },
    viewerRef: { current: { getDrawingScene: () => scene ?? { intake, drawingKey: keyOf(binding), ready: true } } },
    activeIntake: legacy, shown: legacy, intake: legacy, standaloneVisibleLayers: {}, setStandaloneVisibleLayers: vi.fn(),
    consoleDrawingKey: 'console:legacy', drawingLoad: { drawingId: 'legacy' }, REQUESTED_DRAWING_ID: 'legacy',
    useMemo, projectSolarDrawingView, buildDrawingObjectIndex }
}
function projection(env) {
  const scope = { ...env }
  for (const name of ['projectScene', 'projectDrawing', 'drawingIntake', 'projectDrawingName', 'visibleLayers', 'setVisibleLayers'])
    scope[name] = evaluate(decl(name).init, scope)
  return scope
}
function viewerEnv(scope, overrides = {}) {
  // These are inputs to production JSX, not a rewritten Viewer or projection.
  return { Viewer, ...scope, solarStarterViewerRef: createRef(), SOLAR_STARTER_EMPTY_INTAKE: intakeOf(),
    solarOverlay: {}, solarStringRoutes: [{}], overlay: { highlight_handles: ['EE'], markers: [{}], polylines: [{}] },
    pendingEdit: { edit: true }, writeGhost: { ghost: true }, colorForLayer: () => '#888888',
    workspaceCardRef: { current: { dataset: { cockpitPicking: '1' } } }, canvasPickRef: { current: vi.fn() },
    canvasMarqueeRef: { current: vi.fn() }, setSelectedHandle: vi.fn(), ...overrides }
}

it('R13G2A-01 E1 receives the actual Viewer scene readiness', () => {
  const intake = graphIntake(), good = { intake, drawingKey: keyOf(B), ready: true }
  const view = projection(environment(intake, good)).projectDrawing
  expect(view.reason).toBe('READY')
  expect(view.overlay.polylines).toEqual([{ pts: [[1, 2], [3, 4]], color: '#22c55e' }])
  for (const scene of [{ ...good, intake: { ...intake } }, { ...good, drawingKey: 'wrong' },
    { ...good, ready: false }, { ...good, ready: 1 }, { ...good, ready: false, current: true }]) {
    const result = projection(environment(intake, scene)).projectDrawing
    expect(result.reason).toBe('SCENE_PENDING'); expect(result.overlay).toBeNull(); expect(result.intake).toBe(intake)
  }
  for (const [base, reason] of [[intakeOf(), 'GRAPHLESS'], [{ ...intake, solar_design_graph: null }, 'INVALID_GRAPH']]) {
    const result = projection(environment(base)).projectDrawing
    expect(result.reason).toBe(reason); expect(result.intake).toBe(base); expect(result.overlay).toBeNull()
  }
})

it('R13G2A-02 project Viewer props exclude standalone overlays and ghosts', () => {
  const scope = projection(environment()), env = viewerEnv(scope), viewer = element('Viewer')
  for (const [name, value] of [['intake', scope.projectDrawing.intake], ['drawingKey', scope.projectDrawing.drawingKey],
    ['solarOverlay', scope.projectDrawing.overlay], ['pendingEdit', null], ['stringRoutes', undefined],
    ['highlightHandles', undefined], ['markers', undefined], ['overlayPolylines', undefined]])
    expect(evaluate(prop(viewer, name), env)).toBe(value)
  evaluate(prop(viewer, 'onSelectEntity'), env)('CA', { additive: true })
  expect(env.canvasPickRef.current).not.toHaveBeenCalled()
  expect(env.setSelectedHandle).toHaveBeenCalledWith('CA')
  expect(evaluate(prop(viewer, 'marqueeGate'), env)()).toBe(false)
  evaluate(prop(viewer, 'onMarqueeSelect'), env)(['CA'], {})
  expect(env.canvasMarqueeRef.current).not.toHaveBeenCalled()
})

it('R13G2A-03 engine presentation cannot overwrite the project drawing', async () => {
  const session = { engineParsed: true, errorKind: null, documentId: 'parsed.dxf',
    entities: [{ id: 'e1', type: 'LINE', layer: 'Engine', vertices: [[0, 0, 0], [100, 50, 0]] }],
    undoDepth: 0, redoDepth: 0, selectedId: '', selectedIds: [], actions: { select: vi.fn() } }
  vi.spyOn(engineContext, 'useEngineSessionContext').mockImplementation(() => ({ session, highlightedIds: new Set() }))
  const viewerRef = { current: { applyVersion: vi.fn(), setHighlight: vi.fn() } }
  function gate(name) {
    const target = element(name)
    return one(node => node.type === 'LogicalExpression' && node.right === target).left
  }
  const env = { ENV_CAD_EDIT: true, studioGround: true, drafting: true, activeSurface: 'solar', groundShowsDrawing: () => true,
    engineViewArmed: true }
  const mounted = mode => evaluate(gate('EngineDocumentView'), { ...env, projectMode: mode })
    ? <EngineDocumentView viewerRef={viewerRef} /> : null
  const view = render(mounted(false))
  expect(viewerRef.current.applyVersion.mock.calls.at(-1)[0].documentId).toBe('parsed.dxf')
  view.rerender(mounted(true))
  viewerRef.current.applyVersion.mockClear() // Unmount cleanup may release the old override.
  session.entities = [...session.entities, { ...session.entities[0], id: 'e2' }]
  session.undoDepth = 1
  view.rerender(mounted(true)); await act(async () => { await Promise.resolve() })
  expect(viewerRef.current.applyVersion).not.toHaveBeenCalled()
  for (const mode of [true, false]) {
    for (const name of ['EngineDocumentView', 'CanvasPointPicker', 'EngineDockProperties'])
      expect(Boolean(evaluate(gate(name), { ...env, projectMode: mode }))).toBe(!mode)
    expect(evaluate(prop(element('EngineObjectBridge'), 'active'), { ...env, projectMode: mode })).toBe(!mode)
    const rows = decl('propertyRowsEl').init
    expect(Boolean(evaluate(rows.test, { ...env, projectMode: mode }))).toBe(!mode)
  }
  view.rerender(mounted(false))
  expect(viewerRef.current.applyVersion.mock.calls.at(-1)[0].polylines).toHaveLength(2)
})

it('R13G2A-04 drawing facts and object identity follow the canonical intake', () => {
  const input = environment(intakeOf()), base = projection(input)
  const env = { ...base, useMemo: fn => fn(), studioGround: true, drafting: true, drawingExtents, drawingPropertyName,
    engineHistory: { undoDepth: 90 }, projectName: 'legacy', drawingName: 'legacy', mock: true,
    layerCounts: {}, toggleLayer: vi.fn(), colorForLayer: vi.fn(), onOpenStart: vi.fn() }
  const facts = evaluate(decl('paneDrawingFacts').init.arguments[0], env)()
  expect(facts).toMatchObject({ name: 'canonical.dwg', entities: 1, layers: 1, layersShown: 1, source: 'project drawing' })
  expect(facts.undoDepth).toBeUndefined()
  expect(facts.extents).toEqual(drawingExtents(base.projectDrawing.intake.polylines))
  const index = evaluate(decl('consoleObjectIndex').init.arguments[0], env)()
  expect(index.drawingKey).toBe(keyOf(B))
  expect(index.records.some(record => record.handles.includes('CA'))).toBe(true)
  expect(index.records.some(record => record.handles.includes('EE'))).toBe(false)
  const title = one(node => node.type === 'JSXElement' && node.openingElement.attributes.some(a => a.name?.name === 'className' && a.value?.value === 'viewer-title'))
  const titleBranch = one(node => node.type === 'ConditionalExpression' && node.test.name === 'projectMode', title)
  const rendered = render(evaluate(titleBranch, env))
  expect(rendered.container.textContent).toBe(`canonical.dwg ${B.input_version_id}`)
  expect(evaluate(prop(element('StatusTabs'), 'name'), env)).toBe('canonical.dwg')
  const layersCall = one(node => node.type === 'CallExpression' && node.callee.name === 'layersCluster')
  const layerProp = one(node => node.type === 'ObjectProperty' && node.key.name === 'layers', layersCall.arguments[0])
  expect(evaluate(layerProp.value, env)).toBe(base.projectDrawing.intake.layers)
  const hasDrawing = one(node => node.type === 'ObjectProperty' && node.key.name === 'hasDrawing' && node.value.type === 'ConditionalExpression')
  expect(evaluate(hasDrawing.value, env)).toBe(true)
  const refused = projection({ ...input, projectSolarRuntime: { ...input.projectSolarRuntime, contextView: null } })
  const bad = { ...env, ...refused }
  expect(refused.drawingIntake).toBeNull()
  expect(evaluate(decl('paneDrawingFacts').init.arguments[0], bad)()).toBeNull()
  expect(evaluate(decl('consoleObjectIndex').init.arguments[0], bad)()).toBeNull()
  expect(evaluate(hasDrawing.value, bad)).toBe(false)
  expect(evaluate(layerProp.value, bad)).toBeUndefined()
})

function store(snapshot) {
  const listeners = new Set()
  return { getSnapshot: () => snapshot, subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn) },
    change(patch) { snapshot = { ...snapshot, ...patch }; for (const fn of listeners) fn() } }
}
const drain = async () => { for (let i = 0; i < 35; i++) await Promise.resolve() }
async function layerHarness(projectMode) {
  const ws = store({ orgId: B.organization_id, openProjectId: B.project_id, canonicalVersionId: B.input_version_id })
  const session = store({ status: 'active', generation: 1 })
  const client = store({ binding: B, authorityKey: 'session-1' })
  client.submitRun = vi.fn(); client.readJob = vi.fn()
  client.readContext = vi.fn(async () => ({ ok: true, data: contextOf(intakeOf(), client.getSnapshot().binding) }))
  let capture = { live: true }
  function owner(binding) { const token = capture; return { ready: true, scope: { binding, authorityKey: 'session-1' }, projectClient: client, isCurrent: () => token.live } }
  let inputs = { projectMode, projectSolar: owner(B), workspaceController: ws, sessionController: session,
    completedVersionRef: { current: vi.fn() }, hostEnabled: true, externalBusy: false }
  const hook = renderHook(props => {
    const runtime = useAppProjectSolarRuntime(props)
    const [standalone, setStandalone] = useState({ Panels: true })
    const env = { projectMode: props.projectMode, projectSolarRuntime: runtime, standaloneVisibleLayers: standalone,
      setStandaloneVisibleLayers: setStandalone, useCallback }
    env.setVisibleLayers = evaluate(decl('setVisibleLayers').init, env)
    const toggle = evaluate(decl('toggleLayer').init, env)
    return { runtime, standalone, toggle }
  }, { initialProps: inputs })
  await act(drain)
  async function update(patch) { inputs = { ...inputs, ...patch }; await act(async () => { hook.rerender(inputs); await drain() }) }
  async function rotate() {
    capture.live = false; capture = { live: true }
    const binding = { ...B, input_version_id: U(6) }
    await act(async () => {
      ws.change({ canonicalVersionId: binding.input_version_id }); client.change({ binding })
      inputs = { ...inputs, projectSolar: owner(binding) }; hook.rerender(inputs); await drain()
    })
  }
  return { ...hook, update, rotate }
}

it('R13G2A-05 the layer callback follows the current drawing owner', async () => {
  const h = await layerHarness(false), oldStandalone = h.result.current.toggle
  await h.update({ projectMode: true })
  expect(h.result.current.runtime.contextCurrent()).toBe(true)
  act(() => h.result.current.toggle('Panels'))
  expect(h.result.current.runtime.visibleLayers).toEqual({ Panels: false })
  expect(h.result.current.standalone).toEqual({ Panels: true })
  expect(h.result.current.toggle).not.toBe(oldStandalone)
  const retainedProject = h.result.current.toggle
  await h.rotate()
  expect(h.result.current.runtime.visibleLayers).toEqual({})
  act(() => retainedProject('Panels'))
  expect(h.result.current.runtime.visibleLayers).toEqual({})
  act(() => h.result.current.toggle('Panels'))
  expect(h.result.current.runtime.visibleLayers).toEqual({ Panels: false })
})

it('R13G2A-06 the first project layer toggle hides an initially visible layer', async () => {
  const h = await layerHarness(true)
  expect(h.result.current.runtime.visibleLayers).toEqual({})
  act(() => h.result.current.toggle('Panels'))
  expect(h.result.current.runtime.visibleLayers).toEqual({ Panels: false })
  act(() => h.result.current.toggle('Panels'))
  expect(h.result.current.runtime.visibleLayers).toEqual({ Panels: true })
  await h.update({ projectMode: false })
  act(() => h.result.current.toggle('Missing'))
  expect(h.result.current.standalone.Missing).toBe(true)
  act(() => h.result.current.toggle('Missing'))
  expect(h.result.current.standalone.Missing).toBe(false)
})

it('R13G2A-07 presentation identity retires a retained Viewer override', () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  const intake = intakeOf(), ref = createRef(), base = projection(environment(intake))
  const env = viewerEnv(base, { viewerRef: ref, intake, projectMode: false })
  // Evaluate the entire production Viewer element. Free identifiers unrelated to identity are
  // neutral host inputs; the real component still owns its imperative override and effects.
  const identifiers = find(n => n.type === 'Identifier' || n.type === 'JSXIdentifier', element('Viewer'))
  for (const node of identifiers) if (!(node.name in env)) env[node.name] = undefined
  Object.assign(env, { Viewer, visibleLayers: {}, colorForLayer: () => '#888888', overlay: null,
    solarOverlay: null, solarStringRoutes: null, pendingEdit: null, writeGhost: null, studioGround: false,
    surfaceColorForLayer: () => '#888888', setSelectedHandle: vi.fn(), onViewerIntakeOverride: vi.fn(), onDrawingSceneReady: vi.fn() })
  const draw = () => evaluate(element('Viewer'), env)
  const view = render(draw())
  const override = intakeOf('override.dwg', 'EE')
  act(() => ref.current.applyVersion(override))
  expect(ref.current.getDrawingScene().intake).toBe(override)
  env.projectMode = true
  view.rerender(draw())
  expect(ref.current.getDrawingScene().intake).toBe(intake)
  act(() => ref.current.applyVersion(override))
  env.projectDrawing = projection(environment(intake, undefined, { ...B, input_version_id: U(6) })).projectDrawing
  view.rerender(draw())
  expect(ref.current.getDrawingScene().intake).toBe(intake)
  expect(ref.current.getDrawingScene().drawingKey).toBe(env.projectDrawing.drawingKey)
})

it('R13G2A-08 legacy completion can settle without repainting the canonical Viewer', async () => {
  const projectModeRef = { current: false }, viewerRef = { current: { applyVersion: vi.fn() } }
  const apply = callback('applyDrawingIntake', { projectModeRef, viewerRef })
  const controller = renderHook(() => useDrawingVersionController({ onApplyIntake: apply }))
  const legacy = intakeOf('completed.dwg'), getDrawingIntake = vi.fn(async () => ({ intake: legacy, version: 8, head: 8, latest: 8 }))
  const env = { projectModeRef, isScopeCurrent: () => true, mock: false, getDrawingIntake,
    onUndo: vi.fn(), showToast: vi.fn(), projectSolarRuntime: { complete: vi.fn() },
    versionUndoAction: () => ({ label: 'Undo', undo: true, onClick: env.onUndo }),
    markRefreshFailure: vi.fn(), recordCommittedUnreadableHead: vi.fn() }
  env.seatVersion = callback('seatVersion', { ...env, seatDrawingVersion: (...args) => controller.result.current.actions.seatVersion(...args) })
  const complete = callback('seatCompletedVersion', env)
  for (const mode of [true, false, true]) {
    projectModeRef.current = mode
    for (const envelope of [{ tool: 'cad-delete', result: {} }, { result: {} }]) {
      viewerRef.current.applyVersion.mockClear()
      let result
      await act(async () => { result = await complete({ drawing_id: 'legacy', version: 8 }, envelope) })
      expect(result).toBe(true)
      expect(controller.result.current.shown).toBe(legacy)
      if (mode) expect(viewerRef.current.applyVersion).not.toHaveBeenCalled()
      else expect(viewerRef.current.applyVersion).toHaveBeenCalledExactlyOnceWith(legacy)
    }
  }
  expect(getDrawingIntake).toHaveBeenCalledTimes(6)
})

it('R13G2A-09 canonical identity changes clear selection and navigation history', () => {
  const reset = vi.fn(), history = vi.fn()
  const effect = one(node => node.type === 'CallExpression' && node.callee.name === 'useLayoutEffect'
    && find(n => n.type === 'Identifier' && n.name === 'previousProjectDrawing', node).length > 0)
  const hook = renderHook(({ mode, contextView }) => {
    const input = environment()
    const env = projection({ ...input, projectMode: mode, projectSolarRuntime: { ...input.projectSolarRuntime, contextView },
      projectSolar: { scope: { binding: contextView ? { ...B, input_version_id: contextView.context.input_version_id } : B } } })
    env.previousProjectDrawing = evaluate(decl('previousProjectDrawing').init, { useRef })
    evaluate(effect, { ...env, useLayoutEffect, setSelectedHandle: reset, clearViewHistory: history })
  }, { initialProps: { mode: false, contextView: null } })
  expect(reset).not.toHaveBeenCalled(); expect(history).not.toHaveBeenCalled()
  const first = contextOf(intakeOf())
  hook.rerender({ mode: true, contextView: first })
  expect(history).toHaveBeenCalledTimes(1)
  hook.rerender({ mode: true, contextView: first })
  expect(history).toHaveBeenCalledTimes(1)
  const next = contextOf(intakeOf(), { ...B, input_version_id: U(6) })
  hook.rerender({ mode: true, contextView: next })
  expect(history).toHaveBeenCalledTimes(2)
  hook.rerender({ mode: true, contextView: null })
  expect(history).toHaveBeenCalledTimes(3)
  hook.rerender({ mode: false, contextView: null })
  expect(history).toHaveBeenCalledTimes(4)
  expect(reset.mock.calls).toEqual([[null], [null], [null], [null]])
})

it('R13G2A-14 a retained engine document returns after a project closes', () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  const session = { engineParsed: true, errorKind: null, documentId: 'engine.dxf',
    entities: [{ id: 'e1', type: 'LINE', layer: 'Engine', vertices: [[0, 0, 0], [100, 50, 0]] }],
    undoDepth: 0, redoDepth: 0, selectedId: '', selectedIds: [], actions: { select: vi.fn() } }
  vi.spyOn(engineContext, 'useEngineSessionContext').mockImplementation(() => ({ session, highlightedIds: new Set() }))
  const hook = one(node => node.type === 'FunctionDeclaration' && node.id?.name === 'useEngineViewArmed')
  const useEngineViewArmed = Function('useState', 'useLayoutEffect', `return (${text(hook)})`)(useState, useLayoutEffect)
  const armedAt = one(node => node.type === 'VariableDeclarator' && node.id.name === 'engineViewArmed').init
  expect(text(armedAt)).toBe('useEngineViewArmed(projectMode)')
  const engineGate = one(node => node.type === 'LogicalExpression' && node.right === element('EngineDocumentView')).left
  const viewerKey = prop(element('Viewer'), 'key')
  const legacy = intakeOf('legacy.dwg', 'LG')
  const canonical = intakeOf('canonical.dwg', 'CA')
  const shown = []
  const nameOf = intake => intake?.source === 'engine' ? intake.documentId : intake?.dwg
  const visibleLayers = {}, colorForLayer = () => '#888888'
  const onIntakeOverride = (override, base) => shown.push(nameOf(override ?? base))
  function Harness({ projectMode }) {
    // EngineDocumentView precedes the Viewer, as in App: the card renders before the ground portal.
    const viewerRef = useRef(null)
    const engineViewArmed = useEngineViewArmed(projectMode)
    const gateEnv = { ENV_CAD_EDIT: true, studioGround: true, projectMode, engineViewArmed }
    const key = evaluate(viewerKey, { projectMode, projectDrawing: { drawingKey: 'project:canonical' } })
    return <>
      {evaluate(engineGate, gateEnv) ? <EngineDocumentView viewerRef={viewerRef} consoleIntake={legacy} /> : null}
      <Viewer key={key} ref={viewerRef} intake={projectMode ? canonical : legacy}
        drawingKey={projectMode ? 'project:canonical' : 'console:legacy'}
        visibleLayers={visibleLayers} colorForLayer={colorForLayer} onIntakeOverride={onIntakeOverride} />
    </>
  }
  // render and rerender run inside act, which flushes layout and passive effects before returning.
  const view = render(<Harness projectMode={false} />)
  expect(shown.at(-1)).toBe('engine.dxf')
  view.rerender(<Harness projectMode />)
  expect(shown.at(-1)).toBe('canonical.dwg')
  shown.length = 0
  view.rerender(<Harness projectMode={false} />)
  // The standalone Viewer first seats the console drawing, then the retained engine document returns.
  expect(shown).toContain('legacy.dwg')
  expect(shown.at(-1)).toBe('engine.dxf')
  expect(shown.lastIndexOf('legacy.dwg')).toBeLessThan(shown.lastIndexOf('engine.dxf'))
}, 30_000)

it('R13G2A-13 project drawing status uses the retained publication retry', async () => {
  const wrap = one(node => node.type === 'JSXElement' && node.openingElement.attributes.some(a => a.name?.name === 'className' && a.value?.value === 'viewer-wrap'))
  const status = one(node => node.type === 'ConditionalExpression' && node.test.name === 'projectMode'
    && node.consequent.type === 'JSXFragment', wrap)
  const h = await layerHarness(true)
  const runtimeRetry = h.result.current.runtime.retryReadback
  const cases = [
    ['refused', 'sign_in_required', null, null, false, 'Sign in to use Solar in this project.'],
    ['refused', null, null, null, false, 'Project Solar is preparing this drawing version.'],
    ['ready', null, null, null, false, ''],
    ['refused', null, null, U(6), true, 'Project Solar is preparing this drawing version.'],
    ['refused', null, projectSolarAppReason('output_unreadable'), U(6), false,
      'The run published a drawing version, but that version could not be read. Refresh before continuing.'],
  ]
  for (const [mode, reason, notice, publishedVersionId, busy, sentence] of cases) {
    const runtime = { ...h.result.current.runtime, notice, publishedVersionId, busy, retryReadback: runtimeRetry }
    const env = { projectMode: true, projectDrawing: { mode }, projectSolar: { scope: { reason } }, projectSolarRuntime: runtime, projectSolarAppReason }
    const el = evaluate(status, env), view = render(el)
    expect(view.container.textContent).toBe(sentence + (publishedVersionId ? publishedVersionId + 'Retry' : ''))
    const button = view.queryByRole('button', { name: 'Retry' })
    if (!publishedVersionId) expect(button).toBeNull()
    else {
      expect(button.disabled).toBe(busy)
      const buttonNode = one(node => node.type === 'JSXElement' && node.openingElement.name.name === 'button', status.consequent)
      expect(evaluate(prop(buttonNode, 'onClick'), env)).toBe(runtimeRetry)
      if (!busy) await act(async () => { fireEvent.click(button); await drain() })
    }
    view.unmount()
  }
})
