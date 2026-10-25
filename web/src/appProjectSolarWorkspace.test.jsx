import React from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import ProjectSolarWorkspace from './solar/ProjectSolarWorkspace.jsx'
import ProjectSolarGraphForm from './solar/ProjectSolarGraphForm.jsx'
import SolarStepEditor from './solar/SolarStepEditor.jsx'
import SolarFlowRail from './solar/SolarFlowRail.jsx'
import { useAppProjectSolarRuntime } from './solar/useAppProjectSolarRuntime.js'
import { sameSolarDrawingBinding } from './solar/solarDrawingBinding.js'
import { PROJECT_SOLAR_APP_REASONS, projectSolarAppReason } from './solar/solarProjectAppModel.js'
import { SOLAR_OVERLAY_REASONS } from './solar/useSolarGraphOverlay.js'
import { REASONS } from './lib/ribbonClusters.js'
import { SEED_SETTINGS } from './solar/solarSettingsModel.js'
import useEscapeOwner, { escapeOwnerStack } from './lib/useEscapeOwner.js'

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8').replace(/\r\n/g, '\n')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
function nodes(predicate) {
  const found = []
  const walk = (node, parent) => {
    if (!node || typeof node.type !== 'string') return
    if (predicate(node)) found.push({ node, parent })
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach((child) => walk(child, node))
      else if (value && typeof value.type === 'string') walk(value, node)
    }
  }
  walk(tree, null)
  return found
}
const text = (node) => source.slice(node.start, node.end)
const evaluate = (node, inputs) => Function(...Object.keys(inputs),
  `return (${text(node)})`)(...Object.values(inputs))
// Interpret the actual App AST, selecting branches before reading their JSX
// or props. Component bindings are supplied explicitly by each rendered row.
function element(node, inputs, components = {}) {
  const bindings = { ...inputs, ...components }
  if (node.type === 'LogicalExpression') {
    const left = element(node.left, inputs, components)
    if (node.operator === '&&') return left && element(node.right, inputs, components)
    if (node.operator === '||') return left || element(node.right, inputs, components)
    if (node.operator === '??') return left ?? element(node.right, inputs, components)
    throw new Error('Unsupported logical operator: ' + node.operator)
  }
  if (node.type === 'ConditionalExpression') {
    return element(evaluate(node.test, bindings) ? node.consequent : node.alternate, inputs, components)
  }
  if (node.type === 'JSXExpressionContainer') {
    if (node.expression.type === 'JSXEmptyExpression') return null
    return element(node.expression, inputs, components)
  }
  if (node.type === 'JSXText') return node.value.trim() || null
  if (node.type === 'JSXFragment') {
    return React.createElement(React.Fragment, null, ...node.children.map((child) => element(child, inputs, components)))
  }
  if (node.type !== 'JSXElement') {
    if (node.type.startsWith('JSX')) throw new Error('Unsupported JSX form: ' + node.type)
    return evaluate(node, bindings)
  }
  if (node.openingElement.name.type !== 'JSXIdentifier') throw new Error('Unsupported JSX name')
  const name = node.openingElement.name.name
  const component = components[name] ?? (name[0] === name[0].toLowerCase() ? name : null)
  if (!component) throw new Error('Missing component binding: ' + name)
  const props = {}
  for (const attribute of node.openingElement.attributes) {
    if (attribute.type !== 'JSXAttribute' || attribute.name.type !== 'JSXIdentifier') throw new Error('Unsupported JSX attribute')
    if (attribute.value !== null && !['JSXExpressionContainer', 'StringLiteral'].includes(attribute.value.type)) {
      throw new Error('Unsupported JSX attribute value: ' + attribute.value.type)
    }
    props[attribute.name.name] = attribute.value === null ? true :
      attribute.value.type === 'JSXExpressionContainer' ? evaluate(attribute.value.expression, bindings) : attribute.value.value
  }
  return React.createElement(component, props, ...node.children.map((child) => element(child, inputs, components)).filter((child) => child !== null))
}
const declaration = (name) => nodes((node) => node.type === 'VariableDeclarator' && node.id.name === name)[0]
const B = {
  kind: 'project',
  organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222',
  drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444',
}
const D = 'sha256:' + 'c'.repeat(64)
const STEP = ['solar-size-strings', 'solar-string-add', 'solar-string-multi-add',
  'solar-string-conductors', 'solar-assign-equipment']
const GRAPH = ['solar-panels-from-drawing', 'solar-homeruns', 'solar-schedule', 'solar-feeders']
const READ = ['solar-string-data', 'solar-electrical-schedules', 'solar-cable-export']
const JOB = '55555555-5555-4555-8555-555555555555'
const accepted = () => ({ ok: true, status: 202, data: { job_id: JOB, status: 'submitted' } })
function Row(name, code = null) {
  return { name, label: name, catalog_digest: D, params: { type: 'object', properties: {} },
    availability: { admission_checked: true, admission: code === null ? { status_code: 200, error: null }
      : { status_code: 409, error: { reason_code: code, message: 'private message /srv/secret' } },
    entitled: true, engine_ready: true, implemented: true, input_ready: code === null, runnable: code === null,
    refusal_reasons: code === null ? [] : [code] } }
}
function context(graphless = false) {
  return { context: { organization_id: B.organization_id, project_id: B.project_id,
    drawing_id: B.drawing_id, input_version_id: B.input_version_id, head_version_id: B.input_version_id,
    is_head: true, intake_sha256: 'a'.repeat(64) },
  intake: graphless ? {} : { solar_design_graph: { rev: 6, settings: { ...SEED_SETTINGS },
    project: { name: 'Field', zip_code: '', latitude: null, longitude: null } }, solar_design_graph_sha256: 'b'.repeat(64) } }
}
function readResult(name) {
  const output = { result: 57.4, units: 'A' }
  return { ok: true, status: 200, data: { result: {
    schema: 'leaf.solar-project-read.v1', adapter: 'project-local-graph-read',
    organization_id: B.organization_id, project_id: B.project_id, drawing_id: B.drawing_id,
    input_version_id: B.input_version_id, input_intake_sha256: 'a'.repeat(64), graph_sha256: 'b'.repeat(64),
    tool: name, tool_manifest_sha256: D, graph_project_id: 'private-graph-project',
    head_version_id: B.input_version_id, is_head: true, current_required: true,
    request_sha256: 'e'.repeat(64), output_sha256: 'f'.repeat(64),
    output_bytes: Buffer.byteLength(JSON.stringify(output)), drawing_changed: false, output,
  } } }
}
function deferred() {
  let resolve
  const promise = new Promise((yes) => { resolve = yes })
  return { promise, resolve }
}

const seat = nodes(n => n.type === 'JSXElement' && n.openingElement.name.name === 'SolarFlowSeat')[0].node
const hostBranch = seat.children.filter(n => n.type === 'JSXExpressionContainer')[1].expression
const hostStatus = seat.children.find(n => n.type === 'JSXExpressionContainer')
// The refusal is a record bound to the scope it was raised in, and a value derived from it during
// render. Both declarations are required: a missing one fails the harness instead of hiding it.
const refusalState = nodes(n => n.type === 'VariableDeclaration' && n.declarations.some(d =>
  d.id.type === 'ArrayPattern' && d.id.elements[0]?.name === 'projectSolarRefusalRecord'))[0]?.node
const refusalDerived = nodes(n => n.type === 'VariableDeclaration' && n.declarations.some(d =>
  d.id.type === 'Identifier' && d.id.name === 'projectSolarRefusal'))[0]?.node
if (!refusalState || !refusalDerived) throw new Error('App.jsx must declare the refusal record and its derived value')
const solarOverlayStatusOf = Function('SOLAR_OVERLAY_REASONS',
  `${text(nodes(n => n.type === 'FunctionDeclaration' && n.id?.name === 'solarOverlayStatusOf')[0].node)}; return solarOverlayStatusOf`)(SOLAR_OVERLAY_REASONS)
const adapterSource = source.slice(declaration('solarFlowSeated').parent.start,
  nodes(n => n.type === 'CallExpression' && n.callee.name === 'useEscapeOwner' && n.arguments[0]?.value === 'project-solar-form')[0].parent.end)
const useAdapter = Function('React', 'sameSolarDrawingBinding', 'useEscapeOwner', 'PROJECT_SOLAR_APP_REASONS', 'REASONS', 'projectSolarAppReason', `
  const PROJECT_SOLAR_SUPPORTED_TOOLS = ${text(declaration('PROJECT_SOLAR_SUPPORTED_TOOLS').node.init)}
  return function useAdapter(input) {
    const { useRef, useLayoutEffect, useState } = React
    const { projectMode, projectModeRef, projectSolar, projectSolarRuntime, catalogState,
      ENV_SOLAR_FLOW_RAIL, ENV_CAD_EDIT, ENV_SOLAR_SETTINGS_FORM, drafting, surfaceSlots,
      phoneViewport, setStudioDrawer, returnToDrawing, ribbon, activeRibbonTab,
      onCloseSolarFlowStep, setSolarFormTool } = input
    ${text(refusalState)}
    ${text(refusalDerived)}
    ${adapterSource}
    ${text(declaration('ribbonClusters').parent)}
    return { ribbonClusters, solarFlowSeated, projectSolarHostEnabled, projectSolarRows,
      projectSolarRefusal: typeof projectSolarRefusal === 'undefined' ? null : projectSolarRefusal,
      projectSolarWorkspaceRef, projectSolarOpenerRef, onOpenProjectSolarTool, onCloseProjectSolarTool }
  }
`)(React, sameSolarDrawingBinding, useEscapeOwner, PROJECT_SOLAR_APP_REASONS, REASONS, projectSolarAppReason)

function store(initial) {
  let snapshot = initial
  const listeners = new Set()
  return { getSnapshot: () => snapshot, subscribe: vi.fn(fn => { listeners.add(fn); return () => listeners.delete(fn) }),
    change(patch) { snapshot = { ...snapshot, ...patch }; [...listeners].forEach(fn => fn()) } }
}
const drain = async () => { for (let i = 0; i < 45; i++) await Promise.resolve() }
const response = data => ({ ok: true, status: 200, data })
const OUTPUT = '66666666-6666-4666-8666-666666666666'
async function setup({ names = ['solar-settings', ...STEP, ...GRAPH, ...READ], ...changes } = {}) {
  const h = { rows: names.map(name => Row(name)), token: { live: true }, epoch: 0,
    legacy: Object.fromEntries(['run', 'checkoutCapability', 'readIntake', 'saveSettings', 'saveGraph', 'flow', 'close', 'form'].map(k => [k, vi.fn()])),
    key: vi.fn(() => 'intent-g2b'), returnToDrawing: vi.fn(), steps: [], graphs: [], rails: [], workspaces: [] }
  h.client = store({ binding: B, authorityKey: 'epoch-1' })
  h.job = deferred()
  Object.assign(h.client, {
    captureCatalogScope: vi.fn(() => { const epoch = h.epoch; return () => h.epoch === epoch }),
    readContext: vi.fn(async () => response(context())),
    prepareRun: vi.fn(request => Object.freeze({ ...request, binding: h.client.getSnapshot().binding,
      authorityKey: h.client.getSnapshot().authorityKey })),
    catalog: vi.fn(async () => response({ families: [{ capabilities: h.rows }] })),
    submitRun: vi.fn(async () => accepted()), readJob: vi.fn(() => h.job.promise),
    readSolar: vi.fn(async ({ tool }) => readResult(tool)), downloadArtifact: vi.fn(),
    readIntake: h.legacy.readIntake,
  })
  h.ws = store({ orgId: B.organization_id, openProjectId: B.project_id, canonicalVersionId: B.input_version_id })
  h.ws.rehydrate = vi.fn(async () => h.ws.getSnapshot().workspace)
  h.ws.selectCanonicalVersion = vi.fn(version => h.ws.change({ canonicalVersionId: version }))
  h.session = store({ status: 'active', generation: 1 })
  const current = h.token
  h.owner = { ready: true, projectClient: h.client, scope: { binding: B, authorityKey: 'epoch-1' }, isCurrent: () => current.live }
  h.receiptToken = { live: true }
  h.receipt = { client: h.client, binding: B, authorityKey: 'epoch-1', families: [{ capabilities: h.rows }],
    isCurrent: () => h.receiptToken.live }
  h.originalTools = ['cad-line', 'solar-clear-selection', 'undo', 'redo', 'engine-save', 'version-open', 'engine-command',
    'solar-strings', ...names.slice(0, 2)].map(id => ({ id, label: id, disabled: id.startsWith('solar-'), onClick: h.legacy.run }))
  h.input = { projectMode: true, projectModeRef: { current: true }, projectSolar: h.owner,
    catalogState: { projectCatalog: h.receipt }, ENV_SOLAR_FLOW_RAIL: true, ENV_CAD_EDIT: true,
    ENV_SOLAR_SETTINGS_FORM: true, drafting: true, surfaceSlots: { toolbar: { profile: 'solar' } },
    phoneViewport: false, activeRibbonTab: 'solar', ribbon: { clusters: [{ id: 'main', tools: h.originalTools }] },
    externalBusy: false, ...changes }
  const completedVersionRef = { current: null }
  function Workspace(props) { h.workspaces.push(props); return <ProjectSolarWorkspace {...props} /> }
  function Step(props) { h.steps.push(props); return <SolarStepEditor {...props} /> }
  function Graph(props) { h.graphs.push(props); return <ProjectSolarGraphForm {...props} /> }
  function Rail(props) { h.rails.push(props); return <SolarFlowRail {...props} /> }
  function Harness({ input }) {
    const [drawer, setDrawer] = React.useState('tools')
    const runtime = useAppProjectSolarRuntime({ projectMode: input.projectMode, projectSolar: input.projectSolar,
      projectCatalog: input.catalogState.projectCatalog, workspaceController: h.ws, sessionController: h.session,
      completedVersionRef, hostEnabled: input.projectMode && input.ENV_SOLAR_FLOW_RAIL && input.ENV_CAD_EDIT &&
        input.ENV_SOLAR_SETTINGS_FORM && input.drafting && input.surfaceSlots.toolbar.profile === 'solar',
      externalBusy: input.externalBusy, writeBlockedReason: null })
    completedVersionRef.current = (_view, outcome, options) => runtime.complete(outcome, options.projectSolarCompletion)
    const adapter = useAdapter({ ...input, projectSolarRuntime: runtime, setStudioDrawer: setDrawer,
      returnToDrawing: h.returnToDrawing, onCloseSolarFlowStep: h.legacy.close, setSolarFormTool: h.legacy.form })
    h.runtime = runtime; h.adapter = adapter; h.drawer = drawer
    const solarOverlayStatus = evaluate(declaration('solarOverlayStatus').node.init, {
      projectMode: input.projectMode, projectSolarRefusal: adapter.projectSolarRefusal,
      solarOverlayStatusOf, projectDrawing: { reason: null }, solarOverlayReason: null })
    const bindings = { ...input, ...adapter, projectSolarRuntime: runtime, mintCorrelationId: h.key, solarOverlayStatus,
      projectSolarAppReason, projectDrawing: { mode: 'ready' }, onRequestCatalogRun: h.legacy.run, onSubmitSolarFlowStep: h.legacy.run,
      onCloseSolarFlowStep: h.legacy.close, solarFormTool: null, solarFlowEditor: null,
      solarFlowPending: null, catalog: { families: [] }, solarFlowFamiliesDrawingId: null, mock: true,
      catalogRunContext: {}, canOpenSolarSettingsForm: () => false, solarFlowRunsFor: () => ({}),
      solarFlowRuns: {}, running: false, onOpenSolarFlowStep: h.legacy.run, onSolarFlowChange: h.legacy.flow,
      authConfigured: false, signedIn: false, onLogin: () => {} }
    return <><div data-testid="seat">{element(hostBranch, bindings, { ProjectSolarWorkspace: Workspace,
      ProjectSolarGraphForm: Graph, SolarStepEditor: Step, SolarFlowRail: Rail })}</div>
      {element(hostStatus, bindings)}
      <nav aria-label="Fixture ribbon">{adapter.ribbonClusters.flatMap(c => c.tools).map(tool =>
        <button key={tool.id} data-testid={'ribbon-' + tool.id} disabled={tool.disabled}
          aria-controls={tool.controls} aria-expanded={tool.expanded} onClick={tool.onClick}>{tool.label}</button>)}</nav></>
  }
  h.view = render(<Harness input={h.input} />)
  h.update = async patch => { h.input = { ...h.input, ...patch }; await act(async () => { h.view.rerender(<Harness input={h.input} />); await drain() }) }
  h.open = async name => { const button = screen.getByTestId('ribbon-' + name); button.focus(); fireEvent.click(button); await act(drain); return button }
  h.switch = async (close = false) => {
    h.token.live = false; h.epoch++
    const binding = { ...B, project_id: OUTPUT }
    const token = { live: true }; h.token = token
    h.owner = { ...h.owner, scope: { binding, authorityKey: 'epoch-2' }, isCurrent: () => token.live }
    const receipt = { ...h.receipt, binding, authorityKey: 'epoch-2', isCurrent: () => true }
    await act(async () => { h.ws.change({ openProjectId: close ? null : OUTPUT }); h.client.change({ binding, authorityKey: 'epoch-2' }) })
    await h.update({ projectMode: !close, projectSolar: h.owner, catalogState: { projectCatalog: receipt },
      projectModeRef: { current: !close }, ...(close ? { ENV_CAD_EDIT: false } : {}) })
  }
  await act(drain)
  return h
}
const last = values => values.at(-1)
const tool = (h, name) => h.adapter.ribbonClusters.flatMap(c => c.tools).find(t => t.id === name)
const absent = h => expect(h.view.container.querySelector('.project-solar-workspace')).toBeNull()
async function submitGraph(h, name = 'solar-panels-from-drawing') {
  await h.open(name)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  await waitFor(() => expect(h.client.submitRun).toHaveBeenCalledOnce())
  await act(drain)
}
function noLegacy(h) { Object.values(h.legacy).forEach(spy => expect(spy).not.toHaveBeenCalled()) }

it('R13G2B-02 workspace is absent outside project mode', async () => {
  const h = await setup({ projectMode: false, projectModeRef: { current: false } })
  absent(h)
  expect(h.adapter.ribbonClusters).toBe(h.input.ribbon.clusters)
})
it('R13G2B-03 workspace is absent on other profiles', async () => {
  for (const profile of ['cad', 'browser', 'ios']) {
    const h = await setup({ surfaceSlots: { toolbar: { profile } } })
    absent(h); h.view.unmount()
  }
})
it('R13G2B-04 all feature and cockpit fences are required', async () => {
  for (const flag of ['ENV_SOLAR_FLOW_RAIL', 'ENV_CAD_EDIT', 'ENV_SOLAR_SETTINGS_FORM', 'drafting']) {
    const h = await setup({ [flag]: false }); absent(h); h.view.unmount()
  }
})
it('R13G2B-06 foreign and rotated receipts cannot open', async () => {
  const h = await setup()
  const retained = tool(h, 'solar-settings').onClick
  for (const patch of [{ client: {} }, { authorityKey: 'foreign' }, { binding: { ...B, project_id: OUTPUT } }, { isCurrent: () => false }]) {
    await h.update({ catalogState: { projectCatalog: { ...h.receipt, ...patch } } })
    expect(h.adapter.projectSolarRows).toEqual([])
    expect(tool(h, 'solar-settings').disabled).toBe(true)
    act(() => retained())
    expect(h.runtime.openTool).toBeNull()
  }
  await h.update({ catalogState: { projectCatalog: h.receipt } })
  h.receiptToken.live = false
  act(() => retained())
  expect(h.runtime.openTool).toBeNull()
})
it('R13G2B-07 settings opens with canonical project scope', async () => {
  const h = await setup(); await h.open('solar-settings')
  expect(screen.getByTestId('solar-settings-form')).toBeTruthy()
  expect(h.runtime.openTool).toBe(h.rows[0])
  expect(last(h.workspaces).solarBinding).toBe(B)
  expect(last(h.workspaces).projectClient).toBe(h.client)
  expect(h.client.readContext).toHaveBeenCalled()
  noLegacy(h)
})
it('R13G2B-08 every step reaches the project editor branch', async () => {
  for (const name of STEP) {
    const h = await setup({ names: [name] }); await h.open(name)
    expect(screen.getByTestId('project-solar-step-boundary')).toBeTruthy()
    expect(last(h.steps).row).toBe(h.rows[0])
    expect(last(h.steps).solarBinding).toBe(B)
    expect(last(h.steps).projectClient).toBe(h.client)
    noLegacy(h); h.view.unmount()
  }
})
it('R13G2B-09 graph prepares and submits through the runtime with outcome feedback', async () => {
  const h = await setup(); await submitGraph(h)
  const prepared = h.client.submitRun.mock.calls[0][0]
  expect(h.client.prepareRun.mock.results.some(result => result.value === prepared)).toBe(true)
  expect(prepared.binding).toBe(B)
  expect(prepared.tool).toBe('solar-panels-from-drawing')
  expect(h.runtime.projectPending.tool).toBe(prepared.tool)
  await act(async () => { h.job.resolve(response({ job_id: JOB, org_id: B.organization_id, project_id: B.project_id,
    input_version_id: B.input_version_id, tool_name: prepared.tool,
    execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: B.drawing_id },
    status: 'failed', output_version_id: null, result: null, error: { error_code: 'PANELS_REQUIRED', retryable: false } })); await drain() })
  await waitFor(() => expect(h.runtime.projectOutcome?.outcome.state).toBe('failed'))
  expect(last(h.graphs).projectOutcome).toBe(h.runtime.projectOutcome)
  expect(h.runtime.projectOutcome.outcome.reasonCode).toBe('panels_required')
  expect(screen.getByTestId('project-solar-graph-feedback').textContent).toContain('panels_required')
})
it('R13G2B-10 read tools render canonical output without writing', async () => {
  const h = await setup({ names: ['solar-string-data'] }); await h.open('solar-string-data')
  fireEvent.click(screen.getByRole('button', { name: 'Read project output' }))
  await waitFor(() => expect(h.view.container.textContent).toContain('57.4'))
  expect(h.client.readSolar).toHaveBeenCalledOnce()
  expect(h.client.readSolar.mock.calls[0][0].tool).toBe('solar-string-data')
  expect(h.client.submitRun).not.toHaveBeenCalled(); noLegacy(h)
})
it('R13G2B-11 rendered ribbon submit leaves all legacy seams untouched', async () => {
  const h = await setup({ names: ['solar-panels-from-drawing'] })
  await submitGraph(h); noLegacy(h)
  expect(h.client.submitRun.mock.calls[0][0].binding).toBe(B)
})
it('R13G2B-12 missing canonical rows append exactly once on Solar', async () => {
  const h = await setup()
  expect(h.adapter.ribbonClusters.find(c => c.id === 'project-solar-tools')).toBeTruthy()
  const tools = h.adapter.ribbonClusters.flatMap(c => c.tools)
  for (const row of h.rows) expect(tools.filter(t => t.id === row.name)).toHaveLength(1)
  expect(tools.some(t => t.id === 'solar-strings')).toBe(false)
  await h.update({ activeRibbonTab: 'manage' })
  expect(h.adapter.ribbonClusters.some(c => c.id === 'project-solar-tools')).toBe(false)
  await h.open('solar-settings')
  expect(h.runtime.openTool).toBe(h.rows[0])
})
it('R13G2B-13 non-Solar and clear-selection records keep identity and operations', async () => {
  const h = await setup()
  for (const original of h.originalTools.filter(t => !t.id.startsWith('solar-') || t.id === 'solar-clear-selection')) {
    expect(tool(h, original.id)).toBe(original)
    expect(tool(h, original.id).onClick).toBe(original.onClick)
    tool(h, original.id).onClick()
  }
  expect(h.legacy.run).toHaveBeenCalledTimes(7)
})
it('R13G2B-14 unsupported Solar rows disable the legacy callback', async () => {
  const h = await setup({ names: ['solar-unknown'] })
  expect(tool(h, 'solar-unknown')).toMatchObject({ disabled: true, reason: PROJECT_SOLAR_APP_REASONS.unsupported_form, onClick: undefined })
  await h.open('solar-unknown'); expect(h.runtime.openTool).toBeNull(); noLegacy(h)
})

it('W1C5 the project ribbon runs solve, reviews its candidate, then commits and recovers from refusal', async () => {
  const h = await setup({ names: ['solar-solve-proposal', 'solar-commit-solve', 'solar-correct-string'] })
  await h.open('solar-solve-proposal')
  fireEvent.change(screen.getByLabelText('Frame'), { target: { value: 'frame-1' } })
  fireEvent.click(screen.getByRole('button', { name: 'Run solve' }))
  await waitFor(() => expect(h.client.submitRun).toHaveBeenCalledOnce())
  expect(h.client.submitRun.mock.calls[0][0]).toMatchObject({ tool: 'solar-solve-proposal',
    solveContext: { frame_ref: 'frame-1', phase: 'initial', expected_rev: 6 } })
  await act(async () => {
    h.job.resolve(response({ job_id: JOB, org_id: B.organization_id, project_id: B.project_id,
      input_version_id: B.input_version_id, tool_name: 'solar-solve-proposal', status: 'complete',
      execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: B.drawing_id },
      output_version_id: null, error: null, result: { schema: 'leaf.solar-solve-proposal.v1',
        candidate_sha256: 'a'.repeat(64), frame_id: 'frame-1', phase: 'initial',
        candidate_summary: [{ panel_count: 2, endpoints: ['p1', 'p2'] }],
        request_sha256: 'b'.repeat(64), response_sha256: 'c'.repeat(64),
        execution_provenance: { schema: 'leaf.solar-solve-proposal.v1' } } }))
    await drain()
  })
  expect(h.runtime.busy).toBe(false)
  expect(h.runtime.projectOutcome.outcome.state).toBe('proposal')
  expect(screen.getByRole('region', { name: 'Solve proposal review' })).toBeInTheDocument()
  expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
  const commitJob = deferred()
  h.client.readJob.mockImplementation(() => commitJob.promise)
  fireEvent.click(screen.getByRole('button', { name: 'Commit proposal' }))
  await waitFor(() => expect(h.client.submitRun).toHaveBeenCalledTimes(2))
  expect(h.client.submitRun.mock.calls[1][0]).toMatchObject({ tool: 'solar-commit-solve', proposalJobId: JOB,
    params: { drawing_id: B.drawing_id, expected_rev: 6 }, catalogDigest: D, idempotencyKey: 'intent-g2b' })
  await act(async () => {
    commitJob.resolve(response({ job_id: JOB, org_id: B.organization_id, project_id: B.project_id,
      input_version_id: B.input_version_id, tool_name: 'solar-commit-solve', status: 'failed',
      execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: B.drawing_id },
      result: null, output_version_id: null, error: { error_code: 'GRAPH_JOB_FAILED',
        reason_code: 'SOLVE_PROPOSAL_STALE', retryable: false } }))
    await drain()
  })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Run solve again' })).toBeInTheDocument())
  expect(h.view.container.textContent).toContain('solve_proposal_stale')
  fireEvent.click(screen.getByRole('button', { name: 'Run solve again' }))
  expect(screen.getByRole('button', { name: 'Run solve' })).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Solve proposal review' })).toBeNull()
  await h.open('solar-correct-string')
  expect(screen.getByRole('form', { name: 'Correct strings' })).toBeInTheDocument()
  noLegacy(h)
})
it('R13G2B-15 busy runtime supplies the frozen running reason', async () => {
  const h = await setup({ externalBusy: true })
  for (const row of h.rows) expect(tool(h, row.name)).toMatchObject({ disabled: true, reason: REASONS.running, onClick: undefined })
})
it('R13G2B-16 switch or close retires the mounted form and retained submission', async () => {
  for (const close of [true, false]) {
    const h = await setup(); await h.open('solar-panels-from-drawing')
    await waitFor(() => expect(h.client.prepareRun).toHaveBeenCalled())
    const props = last(h.graphs), prepared = h.client.prepareRun.mock.results.at(-1).value
    expect(last(h.workspaces).onProjectSubmit).toBe(h.runtime.onProjectSubmit)
    const oldForm = screen.getByTestId('project-solar-graph-form')
    await h.switch(close)
    expect(oldForm.isConnected).toBe(false)
    await act(async () => { await props.onProjectSubmit(prepared, { binding: B, authorityKey: 'epoch-1', isCurrent: () => true }); await drain() })
    expect(h.client.submitRun).not.toHaveBeenCalled(); noLegacy(h)
    if (close) absent(h)
    else expect(last(h.workspaces).solarBinding.project_id).toBe(OUTPUT)
    h.view.unmount()
  }
})
it('R13G2B-17 late mounted-form completion cannot enter the next project', async () => {
  const h = await setup(); await submitGraph(h)
  await h.switch()
  const reads = h.client.readContext.mock.calls.length
  await act(async () => { h.job.resolve(response({ job_id: JOB, org_id: B.organization_id, project_id: B.project_id,
    input_version_id: B.input_version_id, tool_name: 'solar-panels-from-drawing',
    execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: B.drawing_id },
    status: 'complete', output_version_id: OUTPUT, error: null,
    result: { output_version_id: OUTPUT, graph_commit: { output_version_id: OUTPUT } } })); await drain() })
  expect(h.ws.rehydrate).not.toHaveBeenCalled()
  expect(h.ws.selectCanonicalVersion).not.toHaveBeenCalled()
  expect(h.client.readContext).toHaveBeenCalledTimes(reads)
  expect(h.runtime.projectOutcome).toBeNull()
  expect(h.runtime.publishedVersionId).toBeNull(); noLegacy(h)
})
it('R13G2B-18 flow selection closes only the project editor', async () => {
  const h = await setup(); await h.open('solar-settings')
  const select = screen.getByTestId('solar-flow-select')
  const alternative = [...select.options].find(option => option.value !== select.value)
  expect(alternative).toBeTruthy()
  fireEvent.change(select, { target: { value: alternative.value } }); await act(drain)
  expect(h.runtime.openTool).toBeNull()
  expect(screen.queryByTestId('solar-settings-form')).toBeNull(); noLegacy(h)
})
it('R13G2B-19 accepted phone opening closes the drawer and focuses the host', async () => {
  const h = await setup({ phoneViewport: true }); await h.open('solar-settings')
  expect(h.drawer).toBe('none')
  expect(h.returnToDrawing).toHaveBeenCalledOnce()
  const host = document.getElementById('solar-tool-form')
  expect(host === document.activeElement || host.contains(document.activeElement)).toBe(true)
  expect(host.style.maxHeight).toBe('40dvh')
})
it('R13G2B-20 scoped Escape restores the opener or connected host fallback', async () => {
  for (const detached of [false, true]) {
    const h = await setup(); const opener = await h.open('solar-panels-from-drawing')
    const host = document.getElementById('solar-tool-form')
    opener.focus(); fireEvent.keyDown(opener, { key: 'Escape' })
    expect(h.runtime.openTool).not.toBeNull()
    if (detached) {
      // Remove through React so cleanup and the fallback both observe a real detached opener.
      await h.update({ ribbon: { clusters: [] }, activeRibbonTab: 'manage' })
      expect(opener.isConnected).toBe(false)
    }
    host.focus(); fireEvent.keyDown(host, { key: 'Escape' }); await act(drain)
    expect(h.runtime.openTool).toBeNull()
    expect(document.activeElement).toBe(detached ? host : opener)
    expect(escapeOwnerStack()).not.toContain('project-solar-form')
    const closed = h.runtime.openTool
    host.focus()
    expect(fireEvent.keyDown(host, { key: 'Escape' })).toBe(true)
    expect(h.runtime.openTool).toBe(closed)
    noLegacy(h); h.view.unmount()
  }
})
it('R13G2B-21 rejected phone opening preserves drawer and focus', async () => {
  const h = await setup({ phoneViewport: true })
  const button = screen.getByTestId('ribbon-solar-settings'), retained = tool(h, 'solar-settings').onClick
  button.focus(); h.receiptToken.live = false
  act(() => retained())
  expect(h.drawer).toBe('tools')
  expect(document.activeElement).toBe(button)
  expect(h.runtime.openTool).toBeNull()
  expect(h.returnToDrawing).not.toHaveBeenCalled()
})

it('R13G2B-23 re-activating the open tool moves focus into the workspace', async () => {
  const h = await setup()
  const button = await h.open('solar-settings'), row = h.runtime.openTool
  const host = document.getElementById('solar-tool-form')
  expect(host.contains(document.activeElement)).toBe(true)
  button.focus()
  expect(document.activeElement).toBe(button)
  fireEvent.click(button); await act(drain)
  expect(h.runtime.openTool).toBe(row)
  expect(host.contains(document.activeElement)).toBe(true)
  expect(h.returnToDrawing).toHaveBeenCalledTimes(2)
})

it('R13G2B-24 a receipt that expires before activation is refused with a visible sentence', async () => {
  const h = await setup({ phoneViewport: true })
  const button = screen.getByTestId('ribbon-solar-settings')
  expect(button.disabled).toBe(false)
  expect(tool(h, 'solar-settings').reason).toBe('')
  button.focus()
  h.receiptToken.live = false // No render between expiry and the actual button click.
  fireEvent.click(button); await act(drain)
  expect(h.runtime.openTool).toBeNull()
  expect(screen.queryByTestId('solar-settings-form')).toBeNull()
  expect(screen.getByText(projectSolarAppReason('project_tools_unavailable'), { selector: '[role="status"]' })).toBeVisible()
  expect(button.disabled).toBe(true)
  expect(tool(h, 'solar-settings').reason).toBe(projectSolarAppReason('project_tools_unavailable'))
  expect(document.activeElement).toBe(button)
  expect(h.drawer).toBe('tools')
  expect(h.returnToDrawing).not.toHaveBeenCalled()
  noLegacy(h)
  // The receipt can recover without changing identity; another refusal must still repaint.
  h.receiptToken.live = true
  await h.update({})
  expect(button.disabled).toBe(false)
  h.receiptToken.live = false
  fireEvent.click(button); await act(drain)
  expect(button.disabled).toBe(true)
  expect(screen.getByTestId('solar-graph-overlay-status').textContent).toBe(projectSolarAppReason('project_tools_unavailable'))
  expect(h.runtime.openTool).toBeNull()
  expect(h.returnToDrawing).not.toHaveBeenCalled()
})

it('R13G2B-25 the refusal sentence clears on the next successful open and on a scope or receipt change', async () => {
  for (const change of ['success', 'authority', 'binding', 'receipt', 'client', 'project-mode']) {
    const h = await setup()
    h.receiptToken.live = false
    fireEvent.click(screen.getByTestId('ribbon-solar-settings')); await act(drain)
    const sentence = projectSolarAppReason('project_tools_unavailable')
    const status = () => screen.queryByText(sentence, { selector: '[role="status"]' })
    expect(status()).toBeVisible()
    await h.update({ phoneViewport: true }) // An unrelated render must retain the explanation.
    expect(status()).toBeVisible()
    if (change === 'success') {
      h.receiptToken.live = true
      await h.update({})
      expect(status()).toBeVisible()
      await h.open('solar-settings')
      expect(h.runtime.openTool).toBe(h.rows[0])
    } else if (change === 'receipt') {
      await h.update({ catalogState: { projectCatalog: { ...h.receipt } } })
    } else if (change === 'project-mode') {
      await h.update({ projectMode: false, projectModeRef: { current: false } })
    } else if (change === 'client') {
      await h.update({ projectSolar: { ...h.owner, projectClient: { ...h.client } } })
    } else {
      // Change each dependency independently; authority/binding also cover an in-place scope update.
      if (change === 'authority') h.owner.scope.authorityKey = 'epoch-2'
      if (change === 'binding') h.owner.scope.binding = { ...B, input_version_id: OUTPUT }
      await h.update({ projectSolar: h.owner })
    }
    expect(status()).toBeNull()
    expect(h.adapter.projectSolarRefusal).toBeNull()
    h.view.unmount()
  }
})

it('R13G2B-28 a refusal survives a scope object rebuilt with the same authority and binding', async () => {
  // useProjectSolarScope builds a new scope (and binding) object on every render while the owner is
  // not installed. Clearing the refusal on that identity took part in a render loop in the browser
  // (R13-G2b CI); the refusal now follows authority, binding, client and receipt, never identity.
  const sentence = projectSolarAppReason('project_tools_unavailable')
  // 'copied' rebuilds the scope once; 'rebuilt-every-render' hands every render a fresh scope and binding,
  // as useProjectSolarScope does while its owner is not installed. A scope that changes within one render
  // is not modelled: the hook returns the installed scope object whenever it reports ready.
  for (const shape of ['copied', 'rebuilt-every-render']) {
    const h = await setup()
    const status = () => screen.queryByText(sentence, { selector: '[role="status"]' })
    h.receiptToken.live = false
    fireEvent.click(screen.getByTestId('ribbon-solar-settings')); await act(drain)
    expect(status()).toBeVisible()
    const rebuild = () => { h.owner = { ...h.owner, scope: { binding: { ...B }, authorityKey: 'epoch-1' } } }
    rebuild()
    await h.update({ projectSolar: h.owner })
    for (let i = 0; i < 3; i++) {
      if (shape === 'rebuilt-every-render') rebuild()
      await h.update({ projectSolar: h.owner, phoneViewport: i % 2 === 0 })
    }
    expect(status()).toBeVisible()
    expect(h.adapter.projectSolarRefusal).toEqual({ reason: sentence })
    h.view.unmount()
  }
})

it('R13G2B-26 refusing an activation with an editor open preserves focus and drawer', async () => {
  const h = await setup({ phoneViewport: true })
  await h.open('solar-settings')
  const row = h.runtime.openTool, button = screen.getByTestId('ribbon-cad-line')
  const retained = tool(h, 'solar-settings').onClick
  await h.update({ externalBusy: true })
  button.focus()
  // Busy fieldsets can ignore focus(). A refused activation must not request it either.
  const focus = vi.spyOn(HTMLElement.prototype, 'focus')
  act(() => { expect(retained()).toBe(false) })
  await act(drain)
  expect(h.runtime.openTool).toBe(row)
  expect(document.activeElement).toBe(button)
  expect(focus).not.toHaveBeenCalled()
  expect(h.returnToDrawing).toHaveBeenCalledOnce()
  expect(h.drawer).toBe('none')
  expect(h.adapter.projectSolarRefusal).toEqual({ reason: projectSolarAppReason('project_tools_unavailable') })
})

it('R13G2B-27 a disabled host or inactive project refuses silently', async () => {
  for (const change of [{ ENV_CAD_EDIT: false }, { projectMode: false, projectModeRef: { current: false } },
    { projectModeRef: { current: false } }]) {
    const h = await setup({ phoneViewport: true, ...change })
    const button = screen.getByTestId('ribbon-cad-line')
    button.focus()
    act(() => { expect(h.adapter.onOpenProjectSolarTool(h.rows[0])).toBe(false) })
    await act(drain)
    expect(h.adapter.projectSolarRefusal).toBeNull()
    expect(screen.queryByText(projectSolarAppReason('project_tools_unavailable'), { selector: '[role="status"]' })).toBeNull()
    expect(h.runtime.openTool).toBeNull()
    expect(document.activeElement).toBe(button)
    expect(h.drawer).toBe('tools')
    expect(h.returnToDrawing).not.toHaveBeenCalled()
    noLegacy(h); h.view.unmount()
  }
})
