// @vitest-environment jsdom
import { useRef, useState } from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { parse } from '@babel/parser'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import DemoTour from './DemoTour.jsx'
import { TOUR_STEPS } from './tourScript.js'
import AuthorPanel from '../components/AuthorPanel.jsx'
import ResultPanel from '../components/ResultPanel.jsx'
import useJobController from '../controllers/useJobController.js'
import useAuthorStageController from '../controllers/useAuthorStageController.js'
import { humanizeError } from '../errorHumanize.js'
import { matchPrompt } from '../mock/mockNlPrompt.js'
import { runMock } from '../mock/mockEngine.js'
import {
  confirmRunIntent, createCatalogToolSnapshot, createRunIntentState,
  dismissRunIntent, prepareCatalogRunParams, stageRunIntent,
} from '../runIntent.js'

// Follow the App callback seam used by appProjectSolarDrawing.test.jsx: execute
// the live declarations and read their real JSX bindings, without mounting the
// unrelated WebGL, account and project surfaces. Comments are not declarations.
const source = readFileSync(resolve(process.cwd(), 'src/App.jsx'), 'utf8')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
const app = tree.program.body.find(node => node.type === 'ExportDefaultDeclaration').declaration
const declarations = app.body.body.filter(node => node.type === 'VariableDeclaration').flatMap(node => node.declarations)
const evaluate = (node, scope) => Function(...Object.keys(scope), `return (${source.slice(node.start, node.end)})`)(...Object.values(scope))
function callback(name, scope) {
  const matches = declarations.filter(node => node.id.name === name)
  expect(matches, `one live ${name}`).toHaveLength(1)
  return evaluate(matches[0].init.arguments[0], scope)
}
function element(name) {
  const found = []
  function visit(node) {
    if (!node || typeof node !== 'object') return
    if (node.type === 'JSXElement' && node.openingElement.name.name === name) found.push(node)
    for (const child of Object.values(node)) {
      if (Array.isArray(child)) child.forEach(visit)
      else if (child && typeof child === 'object') visit(child)
    }
  }
  visit(app)
  expect(found, `one live ${name}`).toHaveLength(1)
  return found[0]
}
const tourElement = element('DemoTour')
const authorElement = element('AuthorPanel')
function props(node, names, scope) {
  return Object.fromEntries(names.map(name => {
    const attr = node.openingElement.attributes.find(attr => attr.name?.name === name)
    expect(attr, `JSX binding ${name}`).toBeTruthy()
    return [name, evaluate(attr.value.expression, scope)]
  }))
}

const tools = [
  { name: 'count-by-layer', engine_op: 'count_by_layer', capabilities: ['drawing.read'], params: {} },
  { name: 'highlight-panels-near-edge', engine_op: 'highlight_near_edge', capabilities: ['drawing.read'], params: {} },
  { name: 'measure-panel-area', engine_op: 'measure_area', capabilities: ['drawing.read'], params: {} },
  { name: 'delete-marked-panel', engine_op: 'delete_marked_panel', capabilities: ['drawing.write'], params: {} },
]
const intake = { layers: ['Panels'], polylines: [
  { handle: '7FA3', layer: 'Panels', pts: [[0, 0], [10, 0], [10, 20], [0, 20]] },
] }
const context = { tenantId: 'demo', drawingId: 'demo', drawingVersion: 1 }
const isWriteTool = tool => (tool?.capabilities || []).includes('drawing.write')
const noop = () => {}
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function mountBeat(id, { runTool = vi.fn(), stageAuthorTool = vi.fn(), publish = vi.fn() } = {}) {
  const dispatch = vi.fn(), track = vi.fn()
  let current
  function WiredTour() {
    const [index, setIndex] = useState(TOUR_STEPS.findIndex(step => step.id === id))
    const [tourLanded, setTourLanded] = useState(true)
    const [authorSeed, setAuthorSeed] = useState('')
    const [authorSignal, setAuthorSignal] = useState(0)
    const [prompt, setPrompt] = useState('')
    const [route, setRoute] = useState(null)
    const tourStepRef = useRef(index)
    const cannedSeq = useRef(0)
    const runIntentStateRef = useRef(createRunIntentState('tour-test'))
    const runIntentSessionRef = useRef('tour-test')
    const runIntentSeqRef = useRef(0)
    const catalogRunContextRef = useRef(context)
    const confirmEmitRef = useRef(null)
    const tourDispatchRef = useRef(false)
    const jobs = useJobController({ mock: true })
    const authorStage = useAuthorStageController({ mock: true, stageAuthorTool })
    const dismissRoute = () => setRoute(null)
    const scope = {
      mock: true, tourOn: true, tools, prompt, authorSeed, authorSignal, tourStepRef,
      cannedSeq, setTourLanded, tourLanded, tourDispatchRef,
      runIntentStateRef, runIntentSessionRef, runIntentSeqRef, catalogRunContextRef,
      confirmEmitRef, dismissRoute, track, TOUR_STEPS, authorStage,
      confirmRunIntent, createCatalogToolSnapshot, dismissRunIntent, stageRunIntent,
      isWriteTool, humanizeError, setRunErr: jobs.reportError,
      running: jobs.running, routing: false, previewing: false, writeLocked: false,
      canRunWrite: true, engineDirty: false, engineDirtyRef: { current: false },
      refuseProjectSolar: () => false, catalogRunContext: context,
      prepareRunParams: (tool, params, opts) => prepareCatalogRunParams(tool, params, context, {}, opts),
      closeStartForChange: noop, agentMode: null, setSelectedTool: noop,
      setOverlayStale: noop, markRefreshFailure: noop, lastRunRef: { current: null },
      runJob: jobs.runJob, runTool, shown: intake,
      ENV_SOLAR_SETTINGS_FORM: false, ENV_SOLAR_FLOW_RAIL: false,
      onPromptChange: value => { setPrompt(value); dismissRoute() },
    }
    scope.armDecision = callback('armDecision', scope)
    scope.commitCatalogDecision = decision => {
      const armed = scope.armDecision(decision)
      setRoute(armed)
      return armed
    }
    scope.catalogActions = { dispatch: async text => {
      dispatch(text)
      const decision = matchPrompt(text, tools)
      scope.commitCatalogDecision(decision)
      if (decision.lane === 'build') {
        setAuthorSeed(text)
        setAuthorSignal(value => value + 1)
      }
      return decision
    } }
    for (const name of ['onDispatch', 'onRequestCatalogRun', 'onRun', 'onConfirmCatalogRun',
      'onAuthor', 'onTourAuthorSettled', 'onCannedPrompt']) scope[name] = callback(name, scope)
    current = { scope, jobs, route }
    const tourProps = props(tourElement, ['onCannedPrompt', 'landed', 'busy'], scope)
    const authorProps = props(authorElement, ['onAuthor', 'onStageSettled', 'seed', 'seedSignal',
      'seedAutoSubmit', 'stageActivity'], scope)
    return <>
      <input aria-label="Command" value={prompt} readOnly />
      <ResultPanel running={jobs.running} result={jobs.result} error={jobs.error} />
      {authorSeed && <AuthorPanel {...authorProps} onPublish={publish} />}
      <DemoTour {...tourProps} index={index} onIndexChange={next => {
        tourStepRef.current = next
        setIndex(next)
      }} />
    </>
  }
  render(<WiredTour />)
  return { dispatch, track, runTool, stageAuthorTool, publish, current: () => current }
}

async function finishTyping(id) {
  const step = TOUR_STEPS.find(step => step.id === id)
  await act(async () => { await vi.advanceTimersByTimeAsync(step.prompt.length * 22 + 50) })
}
const next = () => screen.getByRole('button', { name: 'Next' })

beforeEach(() => {
  vi.useFakeTimers()
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1600 })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 1000 })
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('tour run beats wait for the confirmed result', () => {
  it.each(['count', 'edge', 'measure'])('%s stays locked until its run resolves with a visible result', async id => {
    const pending = deferred()
    const runTool = vi.fn(() => pending.promise)
    const h = mountBeat(id, { runTool })
    expect(next()).toBeDisabled()
    await finishTyping(id)
    expect(h.dispatch).toHaveBeenCalledWith(TOUR_STEPS.find(step => step.id === id).prompt)
    expect(runTool).toHaveBeenCalledTimes(1)
    const [, tool, params, drawing] = runTool.mock.calls[0]
    expect(tool.name).toBe(TOUR_STEPS.find(step => step.id === id).tool)
    expect(h.track).toHaveBeenCalledWith('run.confirmed', expect.objectContaining({ tool: tool.name, source: 'tour' }))
    expect(h.current().scope.runIntentStateRef.current.consumedIds).toHaveLength(1)
    expect(next()).toBeDisabled()
    expect(screen.queryByText('Passed')).not.toBeInTheDocument()
    const envelope = runMock(tool, params, drawing)
    await act(async () => { pending.resolve(envelope); await pending.promise })
    expect(next()).toBeEnabled()
    expect(screen.getByText('Passed')).toBeInTheDocument()
    expect(h.current().jobs.result).toEqual(envelope)
    expect(document.querySelector('.result-card table')).toBeInTheDocument()
  })

  it('unlocks with the visible error when the run rejects', async () => {
    const pending = deferred()
    mountBeat('count', { runTool: vi.fn(() => pending.promise) })
    await finishTyping('count')
    expect(next()).toBeDisabled()
    await act(async () => { pending.reject(new Error('Demo run failed')); await pending.promise.catch(noop) })
    expect(next()).toBeEnabled()
    expect(document.querySelector('.result-panel')).toHaveTextContent('Demo run failed')
    expect(screen.queryByText('Passed')).not.toBeInTheDocument()
  })

  it('unlocks with an engine error envelope displayed', async () => {
    const pending = deferred()
    mountBeat('count', { runTool: vi.fn(() => pending.promise) })
    await finishTyping('count')
    await act(async () => { pending.resolve({ ok: false, tool: tools[0].name, error: 'Engine could not count' }) })
    expect(next()).toBeEnabled()
    expect(document.querySelector('.result-panel')).toHaveTextContent('Engine could not count')
  })
})

describe('the author beat lands at staging, with no Publish click', () => {
  it('reports a settled stage once across publication and again for a new submission', async () => {
    const pending = deferred()
    const staged = { demo: true, tool: { name: 'staged-edge-check', description: 'Check the roof edge' },
      preview: 'Generated edge checker', code: 'return panels', receipt: { change_set_id: 'demo-stage', state: 'staged' } }
    const onAuthor = vi.fn(() => pending.promise)
    const onPublish = vi.fn(async () => ({ ...staged, published: true, demo_session_only: true }))
    const onStageSettled = vi.fn()
    const authorProps = { onAuthor, onPublish, onStageSettled, seed: 'Check the roof edge', seedAutoSubmit: true }
    const { rerender } = render(<AuthorPanel {...authorProps} seedSignal={1} />)
    expect(onStageSettled).not.toHaveBeenCalled()
    await act(async () => { pending.resolve(staged); await pending.promise })
    expect(onStageSettled).toHaveBeenCalledTimes(1)
    expect(onStageSettled).toHaveBeenLastCalledWith(1)

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Request publication' })) })
    expect(onPublish).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Published for this demo session only. Reloading restores the seeded demo catalog.')).toBeInTheDocument()
    expect(onStageSettled).toHaveBeenCalledTimes(1)

    await act(async () => { rerender(<AuthorPanel {...authorProps} seedSignal={2} />) })
    expect(onAuthor).toHaveBeenCalledTimes(2)
    expect(onStageSettled).toHaveBeenCalledTimes(2)
    expect(onStageSettled).toHaveBeenLastCalledWith(2)
  })

  it.each(['success', 'failure'])('unlocks on stage %s with the outcome visible', async outcome => {
    const pending = deferred()
    const h = mountBeat('author', { stageAuthorTool: vi.fn(() => pending.promise) })
    await finishTyping('author')
    expect(h.stageAuthorTool).toHaveBeenCalledTimes(1)
    expect(next()).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Request publication' })).not.toBeInTheDocument()
    await act(async () => {
      if (outcome === 'failure') pending.reject(new Error('Could not stage the demo tool'))
      else pending.resolve({ demo: true, tool: { name: 'staged-edge-check', description: 'Check the roof edge' },
        preview: 'Generated edge checker', code: 'return panels', receipt: { change_set_id: 'demo-stage', state: 'staged' } })
      await pending.promise.catch(noop)
    })
    expect(next()).toBeEnabled()
    if (outcome === 'success') {
      expect(document.querySelector('.authored')).toHaveTextContent('staged-edge-check')
      expect(screen.getByText('Demo preview staged. Publish to make it available in this demo.')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Request publication' })).toBeEnabled()
    } else {
      expect(document.querySelector('.author-panel .inline-error')).toHaveTextContent('Could not stage the demo tool')
    }
    expect(h.publish).not.toHaveBeenCalled()
    fireEvent.click(next())
    await finishTyping('version')
    expect(screen.getByText('Step 7 of 9')).toBeInTheDocument()
    expect(h.current().route.runIntent.toolName).toBe('delete-marked-panel')
    expect(h.runTool).not.toHaveBeenCalled()
    expect(next()).toBeEnabled()
    fireEvent.click(next())
    expect(screen.getByText('Step 8 of 9')).toBeInTheDocument()
    expect(next()).toBeEnabled()
  })
})
