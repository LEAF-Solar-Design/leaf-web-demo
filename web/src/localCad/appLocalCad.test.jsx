import React, { StrictMode, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import useDrawingVersionController from '../controllers/useDrawingVersionController.js'
import LocalCadConnect from './LocalCadConnect.jsx'
import { createLocalCadBridge } from './localCadBridge.js'
import { surfaceContract } from '../site/productSurfaces.js'
import { createViewHistory } from '../lib/viewHistory.js'
import EngineSessionProvider, { useEngineSessionContext, REACH_STATE } from '../cadedit/EngineSessionProvider.jsx'

const engineStub = vi.hoisted(() => ({ session: null }))
vi.mock('../cadedit/engineSession.js', () => ({ default: () => engineStub.session, SESSION_ERROR: { CRASHED: 'crashed' } }))

vi.mock('./localCadBridge.js', () => ({ createLocalCadBridge: vi.fn() }))

// Like appProjectSolarScope.test.jsx, mount the production App statements
// with real React/controller state and explicit transport/browser adapters.
const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const ast = parse(source, { sourceType: 'module', plugins: ['jsx'] })
const all = []
function walk(node, parent) {
  if (!node || typeof node.type !== 'string') return
  all.push({ node, parent })
  Object.values(node).forEach(value => {
    if (Array.isArray(value)) value.forEach(child => walk(child, node))
    else if (value?.type) walk(value, node)
  })
}
walk(ast)
const text = node => source.slice(node.start, node.end)
const unique = predicate => {
  const matches = all.filter(({ node, parent }) => predicate(node, parent))
  expect(matches).toHaveLength(1)
  return matches[0]
}
const declaration = name => unique(node => node.type === 'VariableDeclarator'
  && (node.id.name === name || (node.id.type === 'ArrayPattern' && node.id.elements[0]?.name === name)))
const named = name => text(declaration(name).parent)
const assignment = target => text(unique(node => node.type === 'ExpressionStatement'
  && node.expression.type === 'AssignmentExpression' && text(node.expression.left) === target).node)
const effect = marker => text(unique(node => node.type === 'ExpressionStatement'
  && node.expression.type === 'CallExpression' && node.expression.callee.name === 'useEffect'
  && text(node.expression.arguments[0]).includes(marker)).node)
const statements = [
  ...['localCadBridgeRef', 'localCadSnapshotRef', 'localCadRestoreRef', 'localCadStartedRef',
    'localCadLoadGenerationRef', 'localCadEpochRef', 'localCadHistoryRestoredRef',
    'localCadStatus', 'stopLocalCad', 'connectLocalCad', 'localCadUpdateRef'].map(named),
  assignment('requestedDrawingIdRef.onDrawingLoad'),
  named('applySelectedDrawingIntake'),
  assignment('localCadRestoreRef.current'), assignment('localCadUpdateRef.current'), effect('createLocalCadBridge'),
  named('localCadEligible'), effect('reconnect is optional'),
].join('\n')
const useProductionBridge = Function('React', 'useRef', 'useState', 'useCallback', 'useEffect', 'createLocalCadBridge', 'createViewHistory', `
  return function useProductionBridge(env) {
    const { studioGround, drafting, projectMode, activeIntake, shown, drawingState, drawing,
      loadErr, drawingLoad, setDrawingLoad, selectedHandle, setSelectedHandle, pendingEdit, setPendingEdit,
      engineHistory, setEngineHistory, resultCandidate, setResultCandidate, offscreenResult, setOffscreenResult,
      viewHistoryRef, setViewHistorySize,
      standaloneVisibleLayers, overlayStale, seatIntake, setStandaloneVisibleLayers,
      setOverlayStale, setActiveIntake, setLoadErr, closeStartForChange, setLocalCadActive, requestedDrawingIdRef,
      SOLAR_STARTER_EMPTY_INTAKE, applyDrawingIntake } = env;
    ${statements}
    return { localCadEligible, localCadStatus, connectLocalCad, stopLocalCad, applySelectedDrawingIntake,
      onDrawingLoad: requestedDrawingIdRef.onDrawingLoad };
  }
`)(React, useRef, useState, useCallback, useEffect, createLocalCadBridge, createViewHistory)

const hold = unique(node => node.type === 'FunctionDeclaration' && node.id.name === 'LocalCadEngineHold').node
const holdBody = source.slice(hold.body.start + 1, hold.body.body.find(node => node.type === 'ReturnStatement').start)
const ProductionEngineHold = Function('useEngineSessionContext', 'useLayoutEffect', 'REACH_STATE', 'LOCAL_CAD_EDIT_REASON',
  `return function LocalCadEngineHold({ active }) { ${holdBody} return null }`)(
  useEngineSessionContext, useLayoutEffect, REACH_STATE, 'AutoCAD is connected. Disconnect to edit this drawing.')
const provider = unique(node => node.type === 'JSXOpeningElement' && node.name.name === 'EngineSessionProvider').node
const targetExpression = provider.attributes.find(attr => attr.name?.name === 'saveTarget').value.expression
const productionSaveTarget = Function('localCadActive', 'engineSaveTarget', `return (${text(targetExpression)})`)
const saveObject = declaration('engineSaveTarget').node.init.consequent
const saveCallback = saveObject.properties.find(property => property.key.name === 'save').value
const productionSave = Function('env', `
  const { localCadSnapshotRef, getDrawingVersions, checkout, pendingSavesRef, takeCheckout,
    releaseCheckout, saveDrawingVersionPlan, saveEditedDrawingVersion } = env;
  const REQUESTED_DRAWING_ID = 'prior';
  const LOCAL_CAD_EDIT_REASON = 'AutoCAD is connected. Disconnect to edit this drawing.';
  return (${text(saveCallback)});
`)
const loadSeat = unique(node => node.type === 'VariableDeclarator' && node.id.name === 'seat'
  && text(node.init).includes('loadGeneration')).node.init
const productionLoadSeat = Function('requestedDrawingIdRef', 'loadGeneration', 'seatIntake', 'setDrawingLoad', `
  const current = () => true, loadDrawingId = 'prior', mock = false, isEditFixture = false;
  return (${text(loadSeat)})
`)
let engineContext
function EngineActions() {
  engineContext = useEngineSessionContext()
  const { session, canSave, reach } = engineContext
  const reason = reach.state === REACH_STATE.HELD ? reach.sentence : ''
  return <>
    <button disabled={!!reason} title={reason} onClick={() => session.actions.create()}>Engine edit</button>
    <button disabled={!!reason} title={reason} onClick={() => session.actions.undo()}>Engine undo</button>
    <button disabled={!!reason} title={reason} onClick={() => session.actions.redo()}>Engine redo</button>
    <button disabled={!canSave} onClick={() => session.actions.save()}>Engine save</button>
  </>
}

const prior = { dwg: 'prior.dwg', polylines: [], layers: ['Roof'] }
const priorState = { drawing_id: 'prior', version: 3, head: 3, latest: 5 }
const bridgeIntake = { source: 'bridge', dwg: 'live.dwg', polylines: [], layers: ['Live'] }
let transports, observed, apply
function Harness({ initialIntake = null, initialActiveIntake = null, initialError = null, projectMode = false, drafting = surfaceContract('solar').chrome.cockpit, studioGround = true }) {
  const owner = useRef(null)
  const drawing = useDrawingVersionController({ initialIntake,
    initialDrawingState: initialIntake ? priorState : null,
    loadVersions: async () => ({ versions: [{ version: 1 }, { version: 3 }] }),
    loadVersion: async () => ({ intake: { ...prior, dwg: 'historical.dwg' } }),
    onApplyIntake: next => owner.current?.applySelectedDrawingIntake(next) })
  const [activeIntake, setActiveIntake] = useState(initialActiveIntake)
  const [localCadActive, setLocalCadActive] = useState(false)
  const [loadErr, setLoadErr] = useState(initialError)
  const [drawingLoad, setDrawingLoad] = useState({ state: initialError ? 'failed' : initialIntake ? 'seated' : 'idle' })
  const [selectedHandle, setSelectedHandle] = useState(null)
  const [pendingEdit, setPendingEdit] = useState(null)
  const [engineHistory, setEngineHistory] = useState(null)
  const [resultCandidate, setResultCandidate] = useState(null)
  const [offscreenResult, setOffscreenResult] = useState(null)
  const viewHistoryRef = useRef(createViewHistory())
  const requestedDrawingIdRef = useRef(null)
  const bridge = useProductionBridge({ studioGround, drafting, projectMode, activeIntake, setActiveIntake,
    drawing, loadErr, drawingLoad, setDrawingLoad, selectedHandle, setSelectedHandle, pendingEdit, setPendingEdit,
    engineHistory, setEngineHistory, resultCandidate, setResultCandidate, offscreenResult, setOffscreenResult,
    viewHistoryRef, setViewHistorySize: () => {},
    shown: drawing.shown, drawingState: drawing.drawingState, standaloneVisibleLayers: drawing.visibleLayers,
    overlayStale: drawing.overlayStale, seatIntake: drawing.actions.seatIntake,
    setStandaloneVisibleLayers: drawing.actions.setVisibleLayers, setOverlayStale: drawing.actions.setOverlayStale,
    setLoadErr, closeStartForChange: () => {}, setLocalCadActive, requestedDrawingIdRef,
    SOLAR_STARTER_EMPTY_INTAKE: { polylines: [], layers: [] }, applyDrawingIntake: apply })
  owner.current = bridge
  observed = { drawing, bridge, localCadActive, loadErr, drawingLoad, requestedDrawingIdRef,
    activeIntake, setActiveIntake, viewHistoryRef }
  return <EngineSessionProvider saveTarget={productionSaveTarget(localCadActive, { save: apply })}>
    <ProductionEngineHold active={localCadActive} /><EngineActions /><div className="viewer-toolbar">
    {bridge.localCadEligible && <LocalCadConnect {...bridge.localCadStatus}
      onConnect={bridge.connectLocalCad} onDisconnect={() => bridge.stopLocalCad({ forget: true })} />}
    <output>{drawing.shown?.dwg || 'empty'}</output>
    {loadErr && !localCadActive && <div role="alert">{loadErr}<button>Retry</button></div>}
    {drawing.historyOpen && <aside>{drawing.history?.versions.map(row => <span key={row.version}>Version {row.version}</span>)}</aside>}
  </div></EngineSessionProvider>
}
beforeEach(() => {
  transports = []; apply = vi.fn(); localStorage.clear()
  engineStub.session = { engineParsed: true, selectedIds: [], entities: [], documentId: 'browser.dxf',
    status: 'ready', actions: { create: vi.fn(), undo: vi.fn(), redo: vi.fn(), save: vi.fn() } }
  createLocalCadBridge.mockImplementation(callbacks => {
    let running = false
    const bridge = {
      start: vi.fn(() => { running = true; callbacks.onStatus({ status: 'connecting' }) }),
      stop: vi.fn(() => { running = false; callbacks.onStatus({ status: 'stopped' }) }),
      snapshot: () => ({ running, status: running ? 'connecting' : 'stopped' }),
      update: (intake, epoch = 'boot-a') => callbacks.onUpdate({ intake, epoch }),
      loss: () => callbacks.onLoss(),
      status: status => callbacks.onStatus(status),
    }
    transports.push(bridge)
    return bridge
  })
})
afterEach(() => { cleanup(); localStorage.clear(); vi.clearAllMocks() })

it('places the signed-out Solar CAD control after + inside the actual toolbar and before actions', () => {
  const control = unique(node => node.type === 'JSXElement' && node.openingElement.name.name === 'LocalCadConnect')
  const gate = unique(node => node.type === 'JSXExpressionContainer' && node.expression.type === 'LogicalExpression'
    && node.expression.right === control.node)
  const toolbar = gate.parent
  expect(toolbar.openingElement.attributes.some(attr => attr.name?.name === 'className' && attr.value.value === 'viewer-toolbar')).toBe(true)
  const toolbarText = text(toolbar)
  expect(toolbarText.indexOf('doc-tab-add')).toBeLessThan(toolbarText.indexOf('<LocalCadConnect'))
  expect(toolbarText.indexOf('<LocalCadConnect')).toBeLessThan(toolbarText.indexOf('className="viewer-actions"'))
  expect(text(gate.node.expression.left)).toBe('localCadEligible')
  expect(text(declaration('localCadEligible').node.init)).toBe('!!studioGround && drafting && !projectMode')
  expect(text(declaration('projectMode').node.init)).toBe('!mock && !!openProjectId')
  render(<Harness />)
  expect(screen.getByRole('button', { name: 'Connect AutoCAD' }).closest('.viewer-toolbar')).toBeTruthy()
  expect(transports.every(bridge => bridge.start.mock.calls.length === 0)).toBe(true)
})

it.each([true, false])('Connect leaves the canvas alone; Disconnect restores prior drawing=%s', async hasPrior => {
  render(<Harness initialIntake={hasPrior ? prior : null} />)
  const transport = transports.at(-1)
  fireEvent.click(screen.getByRole('button', { name: 'Connect AutoCAD' }))
  expect(apply).not.toHaveBeenCalled()
  expect(observed.drawing.shown).toBe(hasPrior ? prior : null)
  act(() => transport.update(bridgeIntake))
  expect(observed.drawing.shown).toBe(bridgeIntake)
  expect(observed.localCadActive).toBe(true)
  expect(apply.mock.calls.at(-1)[0]).toBe(bridgeIntake)
  act(() => transport.update({ ...bridgeIntake, dwg: 'next.dwg' }))
  fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
  expect(observed.drawing.shown).toBe(hasPrior ? prior : null)
  expect(observed.drawing.drawingState).toEqual(hasPrior ? priorState : null)
  expect(observed.drawing.visibleLayers).toEqual(hasPrior ? { Roof: true } : {})
  expect(observed.localCadActive).toBe(false)
  expect(apply.mock.calls.at(-1)[0]).toBe(hasPrior ? prior : null)
  expect(localStorage.getItem('leaf.localCad.connect')).toBeNull()
})

it('restores on transport stop but a superseding drawing load discards the snapshot', () => {
  render(<Harness initialIntake={prior} />)
  fireEvent.click(screen.getByRole('button', { name: 'Connect AutoCAD' }))
  act(() => transports.at(-1).update(bridgeIntake))
  act(() => transports.at(-1).stop())
  expect(observed.drawing.shown).toBe(prior)
  fireEvent.click(screen.getByRole('button', { name: 'Connect AutoCAD' }))
  act(() => transports.at(-1).update(bridgeIntake))
  const next = { ...prior, dwg: 'upload.dxf' }
  act(() => {
    observed.bridge.onDrawingLoad()
    observed.drawing.actions.seatIntake(next, { apply: true })
  })
  expect(observed.drawing.shown).toBe(next)
  act(() => transports.at(-1).stop())
  expect(observed.drawing.shown).toBe(next)
})

it('a cloud version wins without losing its newly seated version state', () => {
  render(<Harness initialIntake={prior} />)
  fireEvent.click(screen.getByRole('button', { name: 'Connect AutoCAD' }))
  act(() => transports.at(-1).update(bridgeIntake))
  const next = { ...prior, dwg: 'cloud.dwg' }
  const version = { drawing_id: 'cloud', version: 8, head: 8, latest: 9 }
  act(() => observed.drawing.actions.seatVersion({ ...version, intake: next }, { drawingId: 'cloud' }))
  expect(observed.drawing.shown).toBe(next)
  expect(observed.drawing.drawingState).toEqual(version)
  expect(observed.localCadActive).toBe(false)
  act(() => transports.at(-1).stop())
  expect(observed.drawing.shown).toBe(next)
})

it('Disconnect restores the historical preview, history panel, failures and layer visibility', async () => {
  render(<Harness initialIntake={prior} />)
  await act(async () => {
    await observed.drawing.actions.toggleHistory()
    await observed.drawing.actions.previewVersion(1)
  })
  act(() => {
    observed.drawing.actions.setVisibleLayers({ Roof: false })
    observed.drawing.actions.markRefreshFailure({ drawing_id: 'prior', message: 'refresh failed' })
  })
  const before = observed.drawing.captureSnapshot()
  fireEvent.click(screen.getByRole('button', { name: 'Connect AutoCAD' }))
  act(() => transports.at(-1).update(bridgeIntake))
  expect(screen.queryByText('Version 1')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
  expect(screen.getByText('Version 1')).toBeTruthy()
  expect(observed.drawing.shown.dwg).toBe('historical.dwg')
  for (const key of Object.keys(before)) expect(observed.drawing[key]).toBe(before[key])
})

it('Disconnect after a failed load restores its error and Retry UI', () => {
  render(<Harness initialError="Network unavailable" />)
  fireEvent.click(screen.getByRole('button', { name: 'Connect AutoCAD' }))
  act(() => transports.at(-1).update(bridgeIntake))
  expect(screen.queryByRole('alert')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
  expect(screen.getByRole('alert').textContent).toContain('Network unavailable')
  expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
  expect(observed.drawingLoad.state).toBe('failed')
})

it('restores an engine view over its original base, so closing the engine reveals the original drawing', () => {
  const engineIntake = { source: 'engine', dwg: 'engine.dxf', documentId: 'engine.dxf', layers: ['Engine'] }
  render(<Harness initialIntake={prior} initialActiveIntake={engineIntake} />)
  const navigation = observed.viewHistoryRef.current
  fireEvent.click(screen.getByRole('button', { name: 'Connect AutoCAD' }))
  act(() => transports.at(-1).update(bridgeIntake))
  expect(observed.activeIntake).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
  expect(observed.activeIntake).toBe(engineIntake)
  expect(observed.viewHistoryRef.current).toBe(navigation)
  act(() => observed.setActiveIntake(null))
  expect(observed.drawing.intake).toBe(prior)
  expect(observed.drawing.shown).toBe(prior)
})

it('bridge loss restores the prior drawing while polling continues; recovery takes a fresh snapshot', () => {
  render(<Harness initialIntake={prior} />)
  fireEvent.click(screen.getByRole('button', { name: 'Connect AutoCAD' }))
  const transport = transports.at(-1)
  act(() => transport.update({ ...bridgeIntake, truncated: true }))
  expect(observed.bridge.localCadStatus.truncated).toBe(true)
  act(() => { transport.loss(); transport.status({ status: 'waiting' }) })
  expect(observed.drawing.shown).toBe(prior)
  expect(observed.localCadActive).toBe(false)
  expect(observed.bridge.localCadStatus.truncated).toBe(false)
  expect(transport.snapshot().running).toBe(true)
  expect(localStorage.getItem('leaf.localCad.connect')).toBe('1')
  act(() => observed.drawing.actions.setVisibleLayers({ Roof: false }))
  act(() => transport.update(bridgeIntake))
  expect(observed.drawing.shown).toBe(bridgeIntake)
  fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
  expect(observed.drawing.visibleLayers).toEqual({ Roof: false })
})

it('keeps a hidden layer hidden across two revisions and resets visibility for an epoch replacement', () => {
  render(<Harness initialIntake={prior} />)
  fireEvent.click(screen.getByRole('button', { name: 'Connect AutoCAD' }))
  const transport = transports.at(-1)
  act(() => transport.update(bridgeIntake))
  act(() => observed.drawing.actions.setVisibleLayers({ Live: false }))
  act(() => transport.update({ ...bridgeIntake, layers: ['Live', 'New'] }))
  act(() => transport.update({ ...bridgeIntake, layers: ['Live', 'New', 'Newest'] }))
  expect(observed.drawing.visibleLayers).toEqual({ Live: false, New: true, Newest: true })
  act(() => transport.update(bridgeIntake, 'boot-b'))
  expect(observed.drawing.visibleLayers).toEqual({ Live: true })
})

it('disables engine save, edit, undo and redo while AutoCAD is seated and restores them on Disconnect', () => {
  render(<Harness initialIntake={prior} />)
  for (const name of ['Engine edit', 'Engine undo', 'Engine redo', 'Engine save']) {
    expect(screen.getByRole('button', { name }).disabled).toBe(false)
  }
  fireEvent.click(screen.getByRole('button', { name: 'Connect AutoCAD' }))
  act(() => transports.at(-1).update(bridgeIntake))
  for (const name of ['Engine edit', 'Engine undo', 'Engine redo', 'Engine save']) {
    const button = screen.getByRole('button', { name })
    expect(button.disabled).toBe(true)
    fireEvent.click(button)
  }
  expect(screen.getByRole('button', { name: 'Engine edit' }).title)
    .toBe('AutoCAD is connected. Disconnect to edit this drawing.')
  // The real provider's retained callbacks must refuse edits too.
  act(() => {
    engineContext.session.actions.create()
    engineContext.session.actions.undo()
    engineContext.session.actions.redo()
  })
  for (const action of Object.values(engineStub.session.actions)) expect(action).not.toHaveBeenCalled()
  expect(engineContext.canSave).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
  for (const name of ['Engine edit', 'Engine undo', 'Engine redo', 'Engine save']) {
    const button = screen.getByRole('button', { name })
    expect(button.disabled).toBe(false)
    fireEvent.click(button)
  }
  for (const action of Object.values(engineStub.session.actions)) expect(action).toHaveBeenCalledTimes(1)
})

it.each(['retained callback', 'head read', 'checkout'])('refuses an old save after Connect during %s', async stage => {
  let finish
  const pending = new Promise(resolve => { finish = resolve })
  const env = {
    localCadSnapshotRef: { current: stage === 'retained callback' ? {} : null },
    getDrawingVersions: vi.fn(() => stage === 'head read' ? pending : Promise.resolve({ head: 3 })),
    checkout: { actions: { getCapability: () => null } },
    pendingSavesRef: { current: new Map() },
    takeCheckout: vi.fn(() => pending), releaseCheckout: vi.fn().mockResolvedValue(null),
    saveDrawingVersionPlan: vi.fn(), saveEditedDrawingVersion: vi.fn(),
  }
  const saving = productionSave(env)(new Uint8Array([1]), stage === 'head read' ? null : 3, 'digest')
  const rejected = expect(saving).rejects.toThrow('AutoCAD is connected. Disconnect to edit this drawing.')
  env.localCadSnapshotRef.current = {}
  finish(stage === 'checkout' ? { acquired: true, checkout_capability: 'lease' } : { head: 3 })
  await rejected
  expect(env.saveDrawingVersionPlan).not.toHaveBeenCalled()
  expect(env.saveEditedDrawingVersion).not.toHaveBeenCalled()
  if (stage === 'checkout') expect(env.releaseCheckout).toHaveBeenCalledWith('prior', 'lease')
  else expect(env.takeCheckout).not.toHaveBeenCalled()
})

it.each([false, true])('an initial load completing after Connect (remembered=%s) keeps polling and the remembered choice', async remembered => {
  if (remembered) localStorage.setItem('leaf.localCad.connect', '1')
  render(<Harness />)
  if (remembered) await act(async () => {})
  else fireEvent.click(screen.getByRole('button', { name: 'Connect AutoCAD' }))
  const transport = transports.at(-1)
  const initialGeneration = observed.requestedDrawingIdRef.loadGeneration ?? 0
  act(() => transport.update(bridgeIntake))
  const lateSeat = productionLoadSeat(observed.requestedDrawingIdRef, initialGeneration,
    observed.drawing.actions.seatIntake, vi.fn())
  act(() => { lateSeat(prior) })
  expect(transport.snapshot().running).toBe(true)
  expect(observed.drawing.shown).toBe(bridgeIntake)
  expect(localStorage.getItem('leaf.localCad.connect')).toBe('1')
  act(() => { expect(observed.bridge.onDrawingLoad(initialGeneration + 1)).toBe(true) })
  expect(transport.snapshot().running).toBe(false)
  expect(localStorage.getItem('leaf.localCad.connect')).toBe('1')
})

it('honors a remembered choice only on eligible surfaces and starts once under StrictMode', async () => {
  localStorage.setItem('leaf.localCad.connect', '1')
  const view = render(<StrictMode><Harness /></StrictMode>)
  await act(async () => {})
  expect(transports.reduce((n, bridge) => n + bridge.start.mock.calls.length, 0)).toBe(1)
  view.rerender(<StrictMode><Harness /></StrictMode>)
  await act(async () => {})
  expect(transports.reduce((n, bridge) => n + bridge.start.mock.calls.length, 0)).toBe(1)
  view.unmount()
  for (const props of [{ projectMode: true }, { drafting: false }, { studioGround: false }]) {
    const count = transports.length
    const ineligible = render(<Harness {...props} />)
    await act(async () => {})
    expect(screen.queryByTestId('local-cad-connect')).toBeNull()
    expect(transports.slice(count).every(bridge => bridge.start.mock.calls.length === 0)).toBe(true)
    ineligible.unmount()
  }
})

it('reconnects after leaving a drafting surface while preserving the Connect choice', async () => {
  const view = render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Connect AutoCAD' }))
  const transport = transports.at(-1)
  view.rerender(<Harness drafting={false} />)
  expect(transport.snapshot().running).toBe(false)
  expect(localStorage.getItem('leaf.localCad.connect')).toBe('1')
  view.rerender(<Harness drafting />)
  await act(async () => {})
  expect(transports.at(-1).snapshot().running).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
  expect(localStorage.getItem('leaf.localCad.connect')).toBeNull()
})

it('never starts on an eligible surface without a click or remembered choice', async () => {
  render(<StrictMode><Harness /></StrictMode>)
  await act(async () => {})
  expect(transports.every(bridge => bridge.start.mock.calls.length === 0)).toBe(true)
})

it('retires a queued remembered reconnect on surface change and unmount', async () => {
  localStorage.setItem('leaf.localCad.connect', '1')
  const view = render(<Harness />)
  view.rerender(<Harness drafting={false} />)
  await act(async () => {})
  expect(transports.every(bridge => bridge.start.mock.calls.length === 0)).toBe(true)
  view.rerender(<Harness drafting />)
  view.unmount()
  await act(async () => {})
  expect(transports.every(bridge => bridge.start.mock.calls.length === 0)).toBe(true)
  expect(localStorage.getItem('leaf.localCad.connect')).toBe('1')
})
