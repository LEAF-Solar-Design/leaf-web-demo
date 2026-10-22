import React, { StrictMode, useCallback, useEffect, useRef, useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import useDrawingVersionController from '../controllers/useDrawingVersionController.js'
import LocalCadConnect from './LocalCadConnect.jsx'
import { createLocalCadBridge } from './localCadBridge.js'
import { surfaceContract } from '../site/productSurfaces.js'

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
    'localCadStatus', 'stopLocalCad', 'connectLocalCad', 'localCadUpdateRef'].map(named),
  assignment('requestedDrawingIdRef.onDrawingLoad'),
  named('applySelectedDrawingIntake'),
  assignment('localCadRestoreRef.current'), assignment('localCadUpdateRef.current'), effect('createLocalCadBridge'),
  named('localCadEligible'), effect('reconnect is optional'),
].join('\n')
const useProductionBridge = Function('React', 'useRef', 'useState', 'useCallback', 'useEffect', 'createLocalCadBridge', `
  return function useProductionBridge(env) {
    const { studioGround, drafting, projectMode, activeIntake, shown, drawingState,
      standaloneVisibleLayers, overlayStale, seatIntake, setStandaloneVisibleLayers,
      setOverlayStale, setActiveIntake, setLoadErr, closeStartForChange, setLocalCadActive, requestedDrawingIdRef,
      SOLAR_STARTER_EMPTY_INTAKE, applyDrawingIntake } = env;
    ${statements}
    return { localCadEligible, localCadStatus, connectLocalCad, stopLocalCad, applySelectedDrawingIntake,
      onDrawingLoad: requestedDrawingIdRef.onDrawingLoad };
  }
`)(React, useRef, useState, useCallback, useEffect, createLocalCadBridge)

const prior = { dwg: 'prior.dwg', polylines: [], layers: ['Roof'] }
const priorState = { drawing_id: 'prior', version: 3, head: 3, latest: 5 }
const bridgeIntake = { source: 'bridge', dwg: 'live.dwg', polylines: [], layers: ['Live'] }
let transports, observed, apply
function Harness({ initialIntake = null, projectMode = false, drafting = surfaceContract('solar').chrome.cockpit, studioGround = true }) {
  const owner = useRef(null)
  const drawing = useDrawingVersionController({ initialIntake,
    initialDrawingState: initialIntake ? priorState : null,
    onApplyIntake: next => owner.current?.applySelectedDrawingIntake(next) })
  const [activeIntake, setActiveIntake] = useState(null)
  const [localCadActive, setLocalCadActive] = useState(false)
  const requestedDrawingIdRef = useRef(null)
  const bridge = useProductionBridge({ studioGround, drafting, projectMode, activeIntake, setActiveIntake,
    shown: drawing.shown, drawingState: drawing.drawingState, standaloneVisibleLayers: drawing.visibleLayers,
    overlayStale: drawing.overlayStale, seatIntake: drawing.actions.seatIntake,
    setStandaloneVisibleLayers: drawing.actions.setVisibleLayers, setOverlayStale: drawing.actions.setOverlayStale,
    setLoadErr: () => {}, closeStartForChange: () => {}, setLocalCadActive, requestedDrawingIdRef,
    SOLAR_STARTER_EMPTY_INTAKE: { polylines: [], layers: [] }, applyDrawingIntake: apply })
  owner.current = bridge
  observed = { drawing, bridge, localCadActive }
  return <div className="viewer-toolbar">
    {bridge.localCadEligible && <LocalCadConnect {...bridge.localCadStatus}
      onConnect={bridge.connectLocalCad} onDisconnect={bridge.stopLocalCad} />}
    <output>{drawing.shown?.dwg || 'empty'}</output>
  </div>
}
beforeEach(() => {
  transports = []; apply = vi.fn(); localStorage.clear()
  createLocalCadBridge.mockImplementation(callbacks => {
    let running = false
    const bridge = {
      start: vi.fn(() => { running = true; callbacks.onStatus({ status: 'connecting' }) }),
      stop: vi.fn(() => { running = false; callbacks.onStatus({ status: 'stopped' }) }),
      snapshot: () => ({ running, status: running ? 'connecting' : 'stopped' }),
      update: intake => callbacks.onUpdate({ intake }),
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

it('never starts on an eligible surface without a click or remembered choice', async () => {
  render(<StrictMode><Harness /></StrictMode>)
  await act(async () => {})
  expect(transports.every(bridge => bridge.start.mock.calls.length === 0)).toBe(true)
})
