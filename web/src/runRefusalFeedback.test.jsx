// @vitest-environment jsdom
import React from 'react'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import useJobController, { PENDING_RUN_KEY, savePendingRun } from './controllers/useJobController.js'
import SolarToolForm from './solar/SolarToolForm.jsx'
import { humanizeError } from './errorHumanize.js'

afterEach(cleanup)
const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
function find(predicate, node = tree) {
  if (!node || typeof node !== 'object') return null
  if (predicate(node)) return node
  for (const value of Object.values(node)) {
    for (const child of Array.isArray(value) ? value : [value]) {
      if (child && typeof child === 'object') {
        const found = find(predicate, child)
        if (found) return found
      }
    }
  }
  return null
}
function callback(name, env) {
  const node = find(n => n.type === 'VariableDeclarator' && n.id.name === name).init.arguments[0]
  return new Function(...Object.keys(env), `return (${source.slice(node.start, node.end)})`)(...Object.values(env))
}
const tool = { name: 'solar-test', label: 'Solar layout', capabilities: ['drawing.write'], params: { type: 'object', properties: {} } }
function refusalEnv(overrides = {}) {
  return {
    refuseProjectSolar: () => false, tools: [tool], mock: false, session: { status: 'active' },
    catalogRunContextRef: { current: { drawingVersion: 1 } }, isWriteTool: () => true,
    running: false, previewing: false, writeLocked: false, canRunWrite: true, engineDirty: false,
    setRunErr: vi.fn(), ...overrides,
  }
}

it('explains each refused run, including a second run while one is active', () => {
  for (const [state, reason] of [
    [{ session: { status: 'guest' } }, 'Sign in to run tools.'],
    [{ running: true }, 'Another run is in progress. Wait for it to finish, then try again.'],
    [{ previewing: true }, "You're viewing an older version. Return to the latest version to run tools."],
    [{ writeLocked: true }, "This drawing is locked for editing, so tools that change it can't run right now."],
    [{ canRunWrite: false }, "This drawing is locked for editing, so tools that change it can't run right now."],
    [{ engineDirty: true }, 'Save or discard your drawing edits before running Solar layout.'],
  ]) {
    const env = refusalEnv(state)
    expect(callback('armDecision', env)({ lane: 'run', tool: tool.name })).toBeUndefined()
    expect(env.setRunErr.mock.calls[0][0]).toBe(reason)
  }
})

it('shows a local sign-in refusal while signed-in job errors remain hidden', () => {
  const hook = renderHook(() => useJobController({ readsPaused: true, formatError: humanizeError }))
  act(() => hook.result.current.reportError('Old job error'))
  expect(hook.result.current.error).toBeNull()
  act(() => hook.result.current.reportError('Sign in to run tools.', { local: true }))
  expect(hook.result.current.error).toBe('Sign in to run tools.')
  act(() => hook.result.current.clearError())
  expect(hook.result.current.error).toBeNull()
})

it('keeps saved inputs through a refused resume and clears them only after arming', async () => {
  const map = new Map()
  const storage = { getItem: key => map.get(key) ?? null, setItem: (key, value) => map.set(key, value), removeItem: key => map.delete(key) }
  const record = savePendingRun(tool.name, { spacing: 8 }, storage)
  const hook = renderHook(() => useJobController({ storage, services: { listJobs: async () => [] }, pollIntervalMs: 1000000000 }))
  const env = {
    mock: false, pendingRun: record, refuseProjectSolar: () => false,
    preparePendingRun: hook.result.current.preparePendingRun, getTools: async () => [tool],
    onRequestCatalogRun: vi.fn(() => undefined), setRunErr: vi.fn(),
  }
  await act(async () => callback('onResumePendingRun', env)())
  expect(JSON.parse(storage.getItem(PENDING_RUN_KEY)).params).toEqual(record.params)
  expect(hook.result.current.pendingRun.params).toEqual(record.params)
  env.onRequestCatalogRun.mockReturnValue({ runIntent: { intentId: 'armed' } })
  await act(async () => callback('onResumePendingRun', env)())
  expect(storage.getItem(PENDING_RUN_KEY)).toBeNull()
  expect(hook.result.current.pendingRun).toBeNull()
})

it('leaves the Solar form open with its inputs when submission is refused', () => {
  const onClose = vi.fn()
  const editableTool = { ...tool, params: { type: 'object', properties: { spacing: { type: 'number', default: 4 } } } }
  render(<SolarToolForm tool={editableTool} onSubmit={() => undefined} onClose={onClose} />)
  fireEvent.change(screen.getByLabelText('Spacing'), { target: { value: '8' } })
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(onClose).not.toHaveBeenCalled()
  expect(screen.getByLabelText('Spacing').value).toBe('8')
  expect(screen.getByRole('button', { name: 'Review & run' })).toBeTruthy()
})

it('does not scroll after an authored run is refused', async () => {
  const env = {
    loadCatalogTools: async () => [tool], resolvePublishedCatalogTool: () => tool,
    setLastAuthoredTool: vi.fn(), commitCatalogDecision: () => undefined,
    showToast: vi.fn(), setTimeout: vi.fn(),
  }
  await callback('onUseAuthored', env)(tool)
  expect(env.setTimeout).not.toHaveBeenCalled()
})

it('retains confirmation after network, service, and sign-in errors', async () => {
  for (const cause of [new Error('Failed to fetch'), Object.assign(new Error('GET /api/tools -> 503'), { status: 503 }), Object.assign(new Error('GET /api/tools -> 401'), { status: 401 })]) {
    const intent = { intentId: 'saved', toolName: tool.name }
    const state = { current: { pending: intent } }
    const env = {
      refuseProjectSolar: () => false, mock: false, getTools: async () => { throw cause },
      runIntentStateRef: state, dismissRunIntent: vi.fn(), dismissRoute: vi.fn(),
      clearSolarFlowStaged: vi.fn(), ENV_SOLAR_FLOW_RAIL: true, setRunErr: vi.fn(), humanizeError,
    }
    await callback('onConfirmCatalogRun', env)(intent, tool, {})
    expect(state.current.pending).toBe(intent)
    expect(env.dismissRoute).not.toHaveBeenCalled()
    expect(env.setRunErr).toHaveBeenCalledWith(humanizeError(cause))
  }
})

it('wires a Stop button to the same interrupt as Escape and announces failures during a run', () => {
  const stop = find(n => n.type === 'JSXElement' && n.openingElement.name.name === 'button'
    && n.children.some(child => child.type === 'JSXText' && child.value.trim() === 'Stop'))
  expect(stop).not.toBeNull()
  expect(stop.openingElement.attributes.find(a => a.name?.name === 'onClick').value.expression.name).toBe('onStopRun')
  expect(source).toContain('onInterruptRun: onStopRun')
  const failure = find(n => n.type === 'JSXOpeningElement' && n.attributes.some(a => a.name?.name === 'className' && a.value?.value === 'strip-failed enter'))
  expect(failure.attributes.find(a => a.name?.name === 'role')?.value.value).toBe('alert')
  expect(source).toContain('{(runErr || routeErr) && (')
})
