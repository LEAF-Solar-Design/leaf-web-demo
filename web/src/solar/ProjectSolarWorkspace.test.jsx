import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { readFileSync } from 'node:fs'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, it, expect, vi } from 'vitest'
import ProjectSolarWorkspace from './ProjectSolarWorkspace.jsx'
import ProjectSolarGraphForm from './ProjectSolarGraphForm.jsx'
import { PROJECT_SOLAR_WORKSPACE_REASONS, projectSolarWorkspaceReason } from './ProjectSolarReadPanel.jsx'
import { SEED_SETTINGS } from './solarSettingsModel.js'

afterEach(cleanup)
afterEach(() => vi.restoreAllMocks())

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
function fixture(name = STEP[0], { code = null, realGraph = false, rows = null } = {}) {
  const h = { row: Row(name, code), current: true, catalogCurrent: true, epoch: 0,
    snapshot: { binding: B, authorityKey: 'epoch-1' }, listeners: new Set(), graphless: false,
    rails: [], steps: [], graphs: [], local: null, code, realGraph, outcome: accepted() }
  h.rows = rows || [h.row]
  h.scopeController = new AbortController()
  h.key = vi.fn(() => 'intent-e2b')
  h.client = {
    getSnapshot: vi.fn(() => h.snapshot),
    subscribe: vi.fn((listener) => { h.listeners.add(listener); return () => h.listeners.delete(listener) }),
    captureCatalogScope: vi.fn(() => { const captured = h.epoch; return () => captured === h.epoch }),
    readContext: vi.fn(async () => ({ ok: true, status: 200, data: context(h.graphless) })),
    prepareRun: vi.fn((request) => Object.freeze({ binding: B, authorityKey: 'epoch-1', tool: request.tool,
      params: request.params, catalogDigest: request.catalogDigest, idempotencyKey: request.idempotencyKey })),
    catalog: vi.fn(async () => ({ ok: true, status: 200, data: { families: [{ capabilities: [Row(name, h.code)] }] } })),
    readSolar: vi.fn(async ({ tool }) => readResult(tool)), downloadArtifact: vi.fn(),
    submitRun: vi.fn(), dispose: vi.fn(), cancel: vi.fn(), readIntake: vi.fn(), readVersions: vi.fn(),
  }
  h.catalog = { binding: B, authorityKey: 'epoch-1', client: h.client,
    families: [{ capabilities: h.rows }], isCurrent: () => h.catalogCurrent }
  h.submit = vi.fn(() => Promise.resolve(h.outcome))
  h.close = vi.fn()
  h.flow = vi.fn()
  function Rail(props) {
    h.rails.push(props)
    return <nav data-testid="recorded-rail">
      {h.rows.map((row, index) => <button key={index} disabled={!props.onOpenStep || props.busy}
        onClick={() => props.onOpenStep?.(row)}>{'Rail ' + (row && typeof row === 'object' ? row.name : 'invalid')}</button>)}
      <button onClick={() => props.onFlowChange('outputs')}>Switch flow</button>
    </nav>
  }
  function RecordingHost({ kind, props }) {
    const [draft, setDraft] = useState('')
    const local = useRef(null)
    if (!local.current) local.current = { controller: new AbortController(), alive: true }
    h.local = local.current
    useEffect(() => () => { local.current.alive = false; local.current.controller.abort() }, [])
    return <section data-testid={'recorded-' + kind}>
      <input aria-label="Fixture draft" value={draft} onChange={(event) => setDraft(event.target.value)} />
      <button disabled={props.busy || !props.onProjectSubmit} onClick={() => props.onProjectSubmit?.(
        h.intent(), { binding: B, authorityKey: 'epoch-1', signal: local.current.controller.signal,
          isCurrent: () => local.current.alive })}>Submit fixture</button>
      <button onClick={props.onClose}>Close</button>
    </section>
  }
  function Editor(props) {
    h.steps.push(props)
    return <RecordingHost kind="step" props={props} />
  }
  function Graph(props) {
    h.graphs.push(props)
    return realGraph ? <ProjectSolarGraphForm {...props} /> : <RecordingHost kind="graph" props={props} />
  }
  h.props = { solarBinding: B, authorityKey: 'epoch-1', projectClient: h.client, projectCatalog: h.catalog,
    isCurrent: () => h.current, FlowRail: Rail, StepEditor: Editor, GraphForm: Graph, openTool: null,
    onOpenTool: vi.fn((row) => h.update({ openTool: row })), onClose: h.close, onFlowChange: h.flow,
    newProjectIntentKey: h.key, onProjectSubmit: h.submit, busy: false,
    projectRunMemory: null, projectPending: null, projectOutcome: null, submissionSignal: h.scopeController.signal }
  h.intent = () => h.client.prepareRun({ tool: h.props.openTool.name,
    params: { drawing_id: B.drawing_id, expected_rev: 6 }, catalogDigest: D, idempotencyKey: 'intent-e2b' })
  h.mount = (changes = {}) => { h.props = { ...h.props, ...changes }; h.view = render(<ProjectSolarWorkspace {...h.props} />); return h.view }
  h.update = (changes = {}) => { h.props = { ...h.props, ...changes }; h.view.rerender(<ProjectSolarWorkspace {...h.props} />) }
  h.open = (row = h.row) => fireEvent.click(screen.getByRole('button', { name: 'Rail ' + row.name }))
  h.inspect = (row = h.row) => fireEvent.click(screen.getByRole('button', { name: row.label || row.name }))
  h.emit = () => { for (const listener of [...h.listeners]) listener() }
  return h
}
const last = (values) => values[values.length - 1]
const optionsFor = (h) => ({ binding: B, authorityKey: 'epoch-1', signal: h.local.controller.signal,
  isCurrent: () => h.local.alive })
const closeWorkspace = () => fireEvent.click(last(screen.getAllByRole('button', { name: 'Close' })))
function noHost() {
  expect(screen.queryByTestId('recorded-step')).toBeNull()
  expect(screen.queryByTestId('recorded-graph')).toBeNull()
  expect(screen.queryByTestId('solar-settings-form')).toBeNull()
  expect(screen.queryByTestId('project-solar-graph-form')).toBeNull()
  expect(document.querySelector('.project-solar-read-panel')).toBeNull()
}
function stepContract(h) {
  expect(screen.getAllByTestId('recorded-step')).toHaveLength(1)
  const props = last(h.steps)
  expect(props.row).toBe(h.row)
  expect(props.solarBinding).toBe(B)
  expect(last(h.rails).solarBinding).toBe(B)
  expect(props.projectClient).toBe(h.client)
  expect(props.authorityKey).toBe('epoch-1')
  expect(props.catalogDigest).toBe(D)
  expect(props.newProjectIntentKey).toBe(h.key)
  expect(typeof props.onProjectSubmit).toBe('function')
  expect(typeof props.onClose).toBe('function')
  expect(props.busy).toBe(false)
  expect(h.graphs).toHaveLength(0)
  expect(screen.queryByTestId('solar-settings-form')).toBeNull()
  expect(document.querySelector('.project-solar-read-panel')).toBeNull()
  expect(h.client.readIntake).not.toHaveBeenCalled()
  expect(h.client.readVersions).not.toHaveBeenCalled()
  return props
}
const runButton = () => screen.getByRole('button', { name: 'Review & run' })
const insertion = () => screen.getByRole('textbox', { name: 'Insertion point (x, y, optional z)' })
const graphFeedback = () => screen.getByTestId('project-solar-graph-feedback').textContent
async function expectGraphRefusal(h, code, sentence) {
  await waitFor(() => expect(graphFeedback()).toContain(sentence))
  expect(graphFeedback()).toContain(code)
  expect(last(h.graphs).tool).toBe(h.row)
  expect(runButton().disabled).toBe(true)
  expect(h.view.container.textContent).not.toContain('private message')
  expect(h.view.container.textContent).not.toContain('/srv/secret')
  expect(h.submit).not.toHaveBeenCalled()
}

describe('Project Solar workspace composition', () => {
  it('R13E-30 settings dispatches to the existing R8 project host', async () => {
    const h = fixture('solar-settings')
    h.graphless = true
    h.mount()
    h.open()
    await waitFor(() => expect(screen.getByTestId('solar-settings-form').getAttribute('data-mode')).toBe('initialize'))
    expect(screen.getAllByTestId('solar-settings-form')).toHaveLength(1)
    expect(h.props.onOpenTool).toHaveBeenCalledWith(h.row)
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
    expect(h.client.readContext.mock.calls[0][0].signal).toBeInstanceOf(AbortSignal)
    expect(h.steps).toHaveLength(0)
    expect(h.graphs).toHaveLength(0)
    expect(document.querySelector('.project-solar-read-panel')).toBeNull()
    expect(h.client.readIntake).not.toHaveBeenCalled()
    expect(h.client.readVersions).not.toHaveBeenCalled()
    expect(h.client.submitRun).not.toHaveBeenCalled()
  })

  it('R13E-31 sizing dispatches to the manual-global R9 project host', () => {
    const h = fixture('solar-size-strings')
    h.mount()
    h.open()
    stepContract(h)
    expect(h.props.onOpenTool).toHaveBeenCalledWith(h.row)
    expect(document.querySelector('.solar-sizing-form')).toBeNull()
  })

  it('R13E-32 single string dispatches to the R10 project host', () => {
    const h = fixture('solar-string-add')
    h.mount()
    h.open()
    expect(stepContract(h).row.name).toBe('solar-string-add')
    expect(h.props.onOpenTool).toHaveBeenCalledWith(h.row)
    expect(document.querySelector('.solar-string-composer')).toBeNull()
  })

  it('R13E-33 multiple strings dispatches to the R10 project host', () => {
    const h = fixture('solar-string-multi-add')
    h.mount()
    h.open()
    expect(stepContract(h).row.name).toBe('solar-string-multi-add')
    expect(h.props.onOpenTool).toHaveBeenCalledWith(h.row)
    expect(document.querySelector('.solar-string-composer')).toBeNull()
  })

  it('R13E-34 conductors dispatches to the R9 project host', () => {
    const h = fixture('solar-string-conductors')
    h.row.params.properties = { material: { type: 'string', enum: ['copper', 'aluminum'] } }
    h.mount()
    h.open()
    expect(stepContract(h).row.params).toBe(h.row.params)
    expect(document.querySelector('.solar-conductor-form')).toBeNull()
  })

  it('R13E-35 equipment dispatches without converting publication into electrical validity', () => {
    const h = fixture('solar-assign-equipment', { code: 'EQUIPMENT_ASSIGNMENT_REQUIRED' })
    const memory = { artifactKey: 'scoped-memory', runs: { 'solar-assign-equipment': {
      binding: B, authorityKey: 'epoch-1', ok: true, code: null } } }
    const pending = { tool: h.row.name, binding: B, authorityKey: 'epoch-1' }
    const original = JSON.stringify(h.row.availability)
    h.mount({ projectRunMemory: memory, projectPending: pending })
    h.open()
    expect(stepContract(h).row.name).toBe('solar-assign-equipment')
    expect(last(h.rails).projectCatalog).toBe(h.catalog)
    expect(last(h.rails).projectRunMemory).toBe(memory)
    expect(last(h.rails).projectPending).toBe(pending)
    expect(last(h.rails).openName).toBe(h.row.name)
    expect(JSON.stringify(h.row.availability)).toBe(original)
    expect(h.row.availability.runnable).toBe(false)
    expect(h.view.container.textContent).not.toMatch(new RegExp('electrically ready', 'i'))
  })

  it('R13E-36 panel import receives the exact prepared submission contract', async () => {
    const h = fixture('solar-panels-from-drawing', { realGraph: true })
    const outcome = { meta: { binding: B, authorityKey: 'epoch-1', tool: h.row.name }, outcome: { state: 'running' } }
    const pending = deferred()
    h.submit.mockReturnValue(pending.promise)
    h.mount({ projectOutcome: outcome })
    h.open()
    await waitFor(() => expect(runButton().disabled).toBe(false))
    expect(last(h.graphs).tool).toBe(h.row)
    expect(last(h.graphs).projectOutcome).toBe(outcome)
    expect(last(h.graphs).submissionSignal).toBe(h.scopeController.signal)
    expect(h.client.prepareRun.mock.calls[0][0]).toEqual({ tool: h.row.name,
      params: { drawing_id: B.drawing_id, expected_rev: 6 }, catalogDigest: D, idempotencyKey: 'intent-e2b' })
    fireEvent.click(runButton())
    await waitFor(() => expect(h.submit).toHaveBeenCalledTimes(1))
    const intent = h.client.prepareRun.mock.results[0].value
    const [actual, options] = h.submit.mock.calls[0]
    expect(actual).toBe(intent)
    expect(options.binding).toBe(B)
    expect(options.authorityKey).toBe('epoch-1')
    expect(options.signal).toBe(h.scopeController.signal)
    expect(options.isCurrent()).toBe(true)
    await act(async () => { pending.resolve(h.outcome) })
    await waitFor(() => expect(graphFeedback()).toContain('The Solar request was submitted.'))
    h.view.unmount()
    // A recording host exposes the synchronous adapter return, including promise and outcome identity.
    for (const response of [accepted(), { ok: false, status: 409, reason: 'FUTURE_PREREQUISITE' }]) {
      const recording = fixture('solar-panels-from-drawing')
      const promised = Promise.resolve(response)
      recording.submit.mockReturnValue(promised)
      recording.mount({ openTool: recording.row })
      const prepared = recording.intent()
      const returned = last(recording.graphs).onProjectSubmit(prepared, optionsFor(recording))
      expect(returned).toBe(promised)
      expect(await returned).toBe(response)
      expect(recording.submit.mock.calls[0][0]).toBe(prepared)
      recording.view.unmount()
    }
  })

  it('R13E-37 homerun inspection preserves EQUIPMENT_ASSIGNMENT_REQUIRED verbatim', async () => {
    const code = 'EQUIPMENT_ASSIGNMENT_REQUIRED'
    const h = fixture('solar-homeruns', { code, realGraph: true })
    h.mount()
    expect(screen.getByRole('button', { name: h.row.name }).disabled).toBe(false)
    h.inspect()
    expect(h.props.onOpenTool).toHaveBeenCalledWith(h.row)
    await expectGraphRefusal(h, code,
      'Homeruns require valid equipment assignments. Review the electrical reasons on the inverter.')
    expect(h.row.availability.runnable).toBe(false)
  })

  it('R13E-38 schedule inspection preserves ROUTING_TOPOLOGY_REQUIRED and insertion input', async () => {
    const code = 'ROUTING_TOPOLOGY_REQUIRED'
    const h = fixture('solar-schedule', { code, realGraph: true })
    h.mount()
    h.inspect()
    fireEvent.change(insertion(), { target: { value: '1,2' } })
    await expectGraphRefusal(h, code, 'The schedule requires routing topology.')
    const input = insertion()
    const prepared = last(h.client.prepareRun.mock.calls)[0]
    expect(prepared).toEqual({ tool: h.row.name,
      params: { drawing_id: B.drawing_id, expected_rev: 6, insertion_point: [1, 2, 0] },
      catalogDigest: D, idempotencyKey: 'intent-e2b' })
    h.update({ projectPending: { tool: h.row.name, binding: B, authorityKey: 'epoch-1' } })
    expect(insertion()).toBe(input)
    expect(insertion().value).toBe('1,2')
    const previews = h.client.catalog.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Refresh preview' }))
    await waitFor(() => expect(h.client.catalog.mock.calls.length).toBeGreaterThan(previews))
    await expectGraphRefusal(h, code, 'The schedule requires routing topology.')
    expect(insertion()).toBe(input)
    expect(insertion().value).toBe('1,2')
  })

  it('R13E-39 feeder inspection preserves the returned prerequisite without inventing one', async () => {
    for (const [code, sentence] of [
      ['VALID_SETTINGS_REQUIRED', 'Feeders require valid L2 collector settings.'],
      ['STRING_COLLECTORS_REQUIRED', 'Feeders require string collectors.'],
      ['FUTURE_PREREQUISITE', 'The server refused this Solar request. Review the reported code.'],
    ]) {
      const h = fixture('solar-feeders', { code, realGraph: true })
      h.mount()
      h.inspect()
      await expectGraphRefusal(h, code, sentence)
      expect(h.props.onOpenTool).toHaveBeenCalledWith(h.row)
      expect(h.view.container.textContent).not.toContain('Homeruns require')
      if (code === 'FUTURE_PREREQUISITE') {
        expect(h.view.container.textContent).not.toContain('Feeders require valid L2 collector settings.')
        expect(h.view.container.textContent).not.toContain('Feeders require string collectors.')
      }
      h.view.unmount()
    }
  })

  it('R13E-40 project busy state prevents writes across every injected form host', async () => {
    for (const name of ['solar-settings', ...STEP, ...GRAPH]) {
      const h = fixture(name)
      const memory = { artifactKey: 'memory', runs: {} }
      h.mount({ openTool: h.row, projectRunMemory: memory })
      if (name === 'solar-settings') await waitFor(() => expect(screen.getByTestId('solar-settings-form').getAttribute('data-mode')).toBe('edit'))
      const initial = name === 'solar-settings' ? null : last(STEP.includes(name) ? h.steps : h.graphs)
      const retained = initial?.onProjectSubmit
      const prepared = initial ? h.intent() : null
      const options = initial ? optionsFor(h) : null
      h.update({ busy: true })
      expect(last(h.rails).busy).toBe(true)
      expect(last(h.rails).onOpenStep).toBeUndefined()
      expect(last(h.rails).projectRunMemory).toBe(memory)
      const boundary = h.view.container.querySelector('.project-solar-workspace-editor fieldset')
      expect(boundary.disabled).toBe(true)
      expect(last(screen.getAllByRole('button', { name: 'Close' })).closest('fieldset')).toBeNull()
      if (initial) {
        const live = last(STEP.includes(name) ? h.steps : h.graphs)
        expect(live.busy).toBe(true)
        expect(live.row || live.tool).toBe(h.row)
        expect(live.onProjectSubmit).toBeUndefined()
        expect(screen.getByRole('button', { name: 'Submit fixture' }).disabled).toBe(true)
        fireEvent.click(screen.getByRole('button', { name: 'Submit fixture' }))
        expect(retained(prepared, options)).toBeUndefined()
      } else {
        expect(screen.getByRole('button', { name: 'Apply settings' }).disabled).toBe(true)
        fireEvent.click(screen.getByRole('button', { name: 'Apply settings' }))
      }
      expect(h.submit).not.toHaveBeenCalled()
      h.update({ busy: false })
      expect(boundary.disabled).toBe(false)
      expect(last(h.rails).projectRunMemory).toBe(memory)
      if (initial) {
        const live = last(STEP.includes(name) ? h.steps : h.graphs)
        expect(live.row || live.tool).toBe(h.row)
        expect(typeof live.onProjectSubmit).toBe('function')
        expect(await live.onProjectSubmit(prepared, options)).toBe(h.outcome)
        expect(h.submit).toHaveBeenCalledTimes(1)
      }
      h.view.unmount()
    }
    for (const name of READ) {
      const h = fixture(name)
      h.mount({ openTool: h.row, busy: true })
      const read = screen.getByRole('button', { name: PROJECT_SOLAR_WORKSPACE_REASONS.read_action })
      expect(read.disabled).toBe(true)
      fireEvent.click(read)
      expect(h.client.readSolar).not.toHaveBeenCalled()
      h.update({ busy: false })
      expect(read.disabled).toBe(false)
      fireEvent.click(read)
      await waitFor(() => expect(h.client.readSolar).toHaveBeenCalledTimes(1))
      h.view.unmount()
    }
  })

  it('R13E-41 stale or contradictory catalog rows cannot open an actionable form', async () => {
    const attacks = [
      ['binding_invalid', (h) => { h.props.solarBinding = { ...B, input_version_id: 'bad' } }],
      ['client_unavailable', (h) => { h.props.authorityKey = '' }],
      ['client_unavailable', (h) => { h.props.authorityKey = 'x'.repeat(257) }],
      ['client_unavailable', (h) => { h.props.projectClient = null }],
      ['client_unavailable', (h) => { h.client.captureCatalogScope = undefined }],
      ['scope_stale', (h) => { h.snapshot = { binding: { ...B, drawing_id: B.project_id }, authorityKey: 'epoch-1' } }],
      ['scope_stale', (h) => { h.snapshot.authorityKey = 'epoch-2' }],
      ['scope_stale', (h) => { h.props.isCurrent = undefined }],
      ['scope_stale', (h) => { h.props.isCurrent = () => false }],
      ['scope_stale', (h) => { h.props.isCurrent = () => 1 }],
      ['scope_stale', (h) => { h.props.isCurrent = () => { throw new Error('expired') } }],
      ['catalog_unavailable', (h) => { h.catalog.client = { ...h.client } }],
      ['catalog_unavailable', (h) => { h.catalog.authorityKey = 'epoch-2' }],
      ['catalog_unavailable', (h) => { h.catalog.binding = { ...B, drawing_id: B.project_id } }],
      ['catalog_unavailable', (h) => { h.catalog.isCurrent = () => false }],
      ['catalog_unavailable', (h) => { h.catalog.isCurrent = () => 'true' }],
      ['catalog_unavailable', (h) => { h.catalog.isCurrent = () => { throw new Error('expired') } }],
      ['catalog_unavailable', (h) => { Object.defineProperty(h.catalog, 'isCurrent', { get() { throw new Error('expired') } }) }],
      ['catalog_unavailable', (h) => { h.props.openTool = { ...h.row } }],
      ['catalog_unavailable', (h) => { delete h.row.catalog_digest }],
      ['catalog_unavailable', (h) => { h.rows.push({ ...h.row, catalog_digest: 'sha256:' + 'd'.repeat(64) }) }],
      ['catalog_unavailable', (h) => { h.rows.push({ ...h.row, params: { type: 'object', properties: { extra: { type: 'string' } } } }) }],
      ['catalog_unavailable', (h) => { h.rows.push({ ...h.row, availability: { ...h.row.availability, runnable: true } }) }],
      ['catalog_unavailable', (h) => { h.rows.push({ ...h.row, availability: { ...h.row.availability,
        admission: { status_code: 200, error: null } } }) }],
      ['catalog_unavailable', (h) => { h.row.params_schema = { type: 'object', properties: { extra: { type: 'string' } } } }],
      ['catalog_unavailable', (h) => { h.rows.push(null) }],
      ['catalog_unavailable', (h) => { h.catalog.families.push({ capabilities: null }) }],
      ['catalog_unavailable', (h) => { h.row.params.properties.bad = { type: 'string', title: {} } }],
    ]
    for (const [reason, attack] of attacks) {
      const h = fixture('solar-homeruns', { code: 'EQUIPMENT_ASSIGNMENT_REQUIRED' })
      h.props.openTool = h.row
      attack(h)
      h.mount()
      noHost()
      expect(h.view.container.textContent).toContain(projectSolarWorkspaceReason(reason))
      if (last(h.rails)?.onOpenStep) act(() => last(h.rails).onOpenStep(h.props.openTool))
      const inspection = screen.queryByRole('button', { name: h.row.name })
      if (inspection && h.props.openTool === h.row) fireEvent.click(inspection)
      noHost()
      expect(h.submit).not.toHaveBeenCalled()
      expect(h.client.submitRun).not.toHaveBeenCalled()
      h.view.unmount()
    }
    // Equal declarations, including wire/normalized schemas and property-order differences, remain usable.
    const equal = fixture('solar-homeruns')
    equal.rows.push({ ...equal.row, params_schema: equal.row.params })
    equal.mount({ openTool: equal.row })
    expect(last(equal.graphs).tool).toBe(equal.row)
    equal.view.unmount()
    // Action-time validation also catches a change without a parent render.
    for (const variant of ['busy', 'owner', 'catalog', 'snapshot', 'duplicate', 'receipt', 'signal']) {
      const h = fixture('solar-homeruns')
      h.mount({ openTool: h.row })
      const retained = last(h.graphs).onProjectSubmit
      const open = last(h.rails).onOpenStep
      const prepared = h.intent(), options = optionsFor(h)
      if (variant === 'busy') h.update({ busy: true })
      if (variant === 'owner') h.current = false
      if (variant === 'catalog') h.catalogCurrent = false
      if (variant === 'snapshot') h.snapshot.authorityKey = 'epoch-2'
      if (variant === 'duplicate') h.rows.push({ ...h.row, catalog_digest: 'sha256:' + 'd'.repeat(64) })
      if (variant === 'receipt') h.update({ projectCatalog: { ...h.catalog } })
      if (variant === 'signal') h.update({ submissionSignal: new AbortController().signal })
      expect(retained(prepared, options)).toBeUndefined()
      act(() => open(h.row))
      expect(h.props.onOpenTool).not.toHaveBeenCalled()
      expect(h.submit).not.toHaveBeenCalled()
      if (variant !== 'busy') { h.update(); noHost() }
      h.view.unmount()
    }
    // The incoming form must still be current at the exact handoff boundary.
    const h = fixture('solar-string-add')
    h.mount({ openTool: h.row })
    const handoff = last(h.steps).onProjectSubmit, prepared = h.intent(), options = optionsFor(h)
    for (const invalid of [
      { ...options, isCurrent: () => false }, { ...options, isCurrent: () => 1 },
      { ...options, isCurrent: () => { throw new Error('expired') } }, { ...options, authorityKey: 'wrong' },
      { ...options, binding: { ...B, drawing_id: B.project_id } }, { ...options, signal: {} },
    ]) expect(handoff(prepared, invalid)).toBeUndefined()
    for (const invalid of [{ ...prepared, binding: null }, { ...prepared, authorityKey: 'wrong' },
      { ...prepared, tool: 'solar-string-multi-add' }, { ...prepared, catalogDigest: 'sha256:' + 'd'.repeat(64) }]) {
      expect(handoff(invalid, options)).toBeUndefined()
    }
    const accessor = { ...prepared }
    const readTool = vi.fn(() => prepared.tool)
    Object.defineProperty(accessor, 'tool', { get: readTool, enumerable: true })
    expect(handoff(accessor, options)).toBeUndefined()
    expect(readTool).not.toHaveBeenCalled()
    const aborted = new AbortController()
    aborted.abort()
    expect(handoff(prepared, { ...options, signal: aborted.signal })).toBeUndefined()
    expect(h.submit).not.toHaveBeenCalled()
    expect(await handoff(prepared, options)).toBe(h.outcome)
  })

  it('R13E-42 unsupported tools and missing injected hosts never reach standalone execution', async () => {
    for (const [name, missing, reason] of [
      ['solar-unknown', null, 'unsupported'], ['solar-settings', 'FlowRail', 'host_unavailable'],
      ['solar-string-add', 'StepEditor', 'host_unavailable'], ['solar-homeruns', 'GraphForm', 'host_unavailable'],
    ]) {
      const h = fixture(name)
      h.mount({ openTool: h.row, ...(missing ? { [missing]: undefined } : {}) })
      noHost()
      expect(h.view.container.textContent).toContain(projectSolarWorkspaceReason(reason))
      if (missing === 'FlowRail') expect(screen.queryByTestId('recorded-rail')).toBeNull()
      if (last(h.rails)?.onOpenStep) act(() => last(h.rails).onOpenStep(h.row))
      expect(h.props.onOpenTool).not.toHaveBeenCalled()
      expect(h.client.submitRun).not.toHaveBeenCalled()
      expect(h.client.readIntake).not.toHaveBeenCalled()
      h.view.unmount()
    }
    for (const missing of ['newProjectIntentKey', 'onProjectSubmit']) {
      const h = fixture('solar-string-add')
      h.mount({ openTool: h.row, [missing]: undefined })
      expect(last(h.steps).onProjectSubmit).toBeUndefined()
      expect(h.view.container.querySelector('.project-solar-workspace-editor fieldset').disabled).toBe(true)
      fireEvent.click(screen.getByRole('button', { name: 'Submit fixture' }))
      expect(h.submit).not.toHaveBeenCalled()
      expect(h.client.submitRun).not.toHaveBeenCalled()
      h.view.unmount()
    }
    const noOpening = fixture('solar-homeruns')
    noOpening.mount({ onOpenTool: undefined })
    expect(last(noOpening.rails).onOpenStep).toBeUndefined()
    expect(screen.getByRole('button', { name: noOpening.row.name }).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: noOpening.row.name }))
    noHost()
    noOpening.view.unmount()
    for (const name of READ) {
      const h = fixture(name)
      h.mount()
      h.inspect()
      expect(h.props.onOpenTool).toHaveBeenCalledWith(h.row)
      expect(document.querySelectorAll('.project-solar-read-panel')).toHaveLength(1)
      expect(h.steps).toHaveLength(0)
      expect(h.graphs).toHaveLength(0)
      fireEvent.click(screen.getByRole('button', { name: PROJECT_SOLAR_WORKSPACE_REASONS.read_action }))
      await waitFor(() => expect(screen.getByTestId('solar-read-headline').textContent).toBe('57.4 A'))
      expect(h.client.readSolar).toHaveBeenCalledTimes(1)
      expect(h.client.readSolar.mock.calls[0][0]).toEqual({ tool: name, params: { drawing_id: B.drawing_id },
        catalogDigest: D, current: true, signal: expect.any(AbortSignal) })
      expect(h.client.prepareRun).not.toHaveBeenCalled()
      expect(h.submit).not.toHaveBeenCalled()
      expect(h.client.submitRun).not.toHaveBeenCalled()
      h.view.unmount()
    }
    // Only scope methods are required by Workspace; operation refusal stays with the selected host.
    const missingOperation = fixture('solar-string-data')
    missingOperation.client.readSolar = undefined
    missingOperation.mount({ openTool: missingOperation.row })
    expect(document.querySelector('.project-solar-read-panel')).not.toBeNull()
    expect(screen.getByRole('button', { name: PROJECT_SOLAR_WORKSPACE_REASONS.read_action }).disabled).toBe(true)
    expect(missingOperation.view.container.textContent).toContain(PROJECT_SOLAR_WORKSPACE_REASONS.client_unavailable)
  })

  it('R13E-43 closing or switching flow clears presentation without disposing the accepted run', async () => {
    for (const switchFlow of [false, true]) {
      const h = fixture('solar-string-add')
      const memory = { artifactKey: 'memory', runs: {} }
      const pending = { tool: h.row.name, binding: B, authorityKey: 'epoch-1' }
      h.close.mockImplementation(() => { noHost() })
      h.flow.mockImplementation(() => { noHost(); expect(h.close).toHaveBeenCalledTimes(1) })
      h.mount({ openTool: h.row, projectRunMemory: memory, projectPending: pending })
      fireEvent.change(screen.getByRole('textbox', { name: 'Fixture draft' }), { target: { value: 'old draft' } })
      const local = h.local
      const prepared = h.intent()
      const callback = last(h.steps).onProjectSubmit
      expect(await callback(prepared, optionsFor(h))).toBe(h.outcome)
      const options = h.submit.mock.calls[0][1]
      expect(options.signal).toBe(h.scopeController.signal)
      expect(options.isCurrent()).toBe(true)
      if (switchFlow) fireEvent.click(screen.getByRole('button', { name: 'Switch flow' }))
      else closeWorkspace()
      noHost()
      expect(h.props.openTool).toBe(h.row)
      expect(local.controller.signal.aborted).toBe(true)
      expect(local.alive).toBe(false)
      expect(options.signal.aborted).toBe(false)
      expect(options.isCurrent()).toBe(true)
      expect(h.close).toHaveBeenCalledTimes(1)
      expect(h.flow).toHaveBeenCalledTimes(switchFlow ? 1 : 0)
      if (switchFlow) expect(h.flow).toHaveBeenCalledWith('outputs')
      expect(last(h.rails).projectRunMemory).toBe(memory)
      expect(last(h.rails).projectPending).toBe(pending)
      h.update({ busy: true })
      expect(options.isCurrent()).toBe(true)
      noHost()
      h.update({ busy: false })
      noHost()
      expect(callback(prepared, { binding: B, authorityKey: 'epoch-1', isCurrent: () => true })).toBeUndefined()
      h.open()
      expect(screen.getByRole('textbox', { name: 'Fixture draft' }).value).toBe('')
      expect(options.isCurrent()).toBe(true)
      h.update({ authorityKey: 'epoch-2' })
      noHost()
      expect(options.isCurrent()).toBe(false)
      expect(h.client.dispose).not.toHaveBeenCalled()
      expect(h.client.cancel).not.toHaveBeenCalled()
      expect(h.client.submitRun).not.toHaveBeenCalled()
      h.view.unmount()
    }
    // E2a's own cleanup aborts reads, while the accepted-run signal remains scope-owned.
    for (const switchFlow of [false, true]) {
      const h = fixture('solar-string-data')
      h.row.params.properties = { note: { type: 'string', default: '' } }
      const read = deferred()
      h.client.readSolar.mockReturnValueOnce(read.promise)
      h.mount({ openTool: h.row })
      fireEvent.change(screen.getByRole('textbox', { name: 'Note' }), { target: { value: 'old read draft' } })
      fireEvent.click(screen.getByRole('button', { name: PROJECT_SOLAR_WORKSPACE_REASONS.read_action }))
      await waitFor(() => expect(h.client.readSolar).toHaveBeenCalledTimes(1))
      const signal = h.client.readSolar.mock.calls[0][0].signal
      if (switchFlow) fireEvent.click(screen.getByRole('button', { name: 'Switch flow' }))
      else closeWorkspace()
      noHost()
      expect(signal.aborted).toBe(true)
      expect(h.scopeController.signal.aborted).toBe(false)
      await act(async () => { read.resolve(readResult(h.row.name)) })
      expect(screen.queryByTestId('solar-read-result')).toBeNull()
      h.update()
      noHost()
      h.inspect()
      expect(screen.getByRole('textbox', { name: 'Note' }).value).toBe('')
      expect(screen.queryByTestId('solar-read-result')).toBeNull()
      expect(h.client.dispose).not.toHaveBeenCalled()
      expect(h.client.cancel).not.toHaveBeenCalled()
      h.view.unmount()
    }
    // A new controlled selection transition is also allowed to reopen a retired presentation.
    const transitioned = fixture('solar-string-add')
    transitioned.mount({ openTool: transitioned.row })
    closeWorkspace()
    transitioned.update({ openTool: null })
    transitioned.update({ openTool: transitioned.row })
    expect(screen.getByTestId('recorded-step')).not.toBeNull()
  })

  it('R13E-45 an accepted handoff outlives the workspace and follows only its captured scope', async () => {
    // After a 202 the guard belongs to the captured scope: unmounting the workspace does not retire it.
    for (const change of ['snapshot', 'epoch', 'owner', 'catalog']) {
      const h = fixture('solar-string-add')
      h.mount({ openTool: h.row })
      expect(await last(h.steps).onProjectSubmit(h.intent(), optionsFor(h))).toBe(h.outcome)
      const options = h.submit.mock.calls[0][1]
      h.view.unmount()
      expect(options.isCurrent()).toBe(true)
      if (change === 'snapshot') h.snapshot = { binding: B, authorityKey: 'epoch-2' }
      if (change === 'epoch') h.epoch += 1
      if (change === 'owner') h.current = false
      if (change === 'catalog') h.catalogCurrent = false
      expect(options.isCurrent()).toBe(false)
    }
    // A catalog epoch that moves after submission ends the guard while the workspace is still mounted.
    const mounted = fixture('solar-string-add')
    mounted.mount({ openTool: mounted.row })
    expect(await last(mounted.steps).onProjectSubmit(mounted.intent(), optionsFor(mounted))).toBe(mounted.outcome)
    const guarded = mounted.submit.mock.calls[0][1]
    expect(guarded.isCurrent()).toBe(true)
    mounted.epoch += 1
    expect(guarded.isCurrent()).toBe(false)
    mounted.view.unmount()
    // StrictMode replays effects on mount; the handoff still submits exactly once without a further render.
    const strict = fixture('solar-string-add')
    strict.props = { ...strict.props, openTool: strict.row }
    strict.view = render(<React.StrictMode><ProjectSolarWorkspace {...strict.props} /></React.StrictMode>)
    const handoff = last(strict.steps).onProjectSubmit
    expect(await handoff(strict.intent(), { binding: B, authorityKey: 'epoch-1', isCurrent: () => true })).toBe(strict.outcome)
    expect(strict.submit).toHaveBeenCalledTimes(1)
    expect(strict.submit.mock.calls[0][1].isCurrent()).toBe(true)
    strict.view.unmount()
  })

  it('R13E-46 a catalog list with a hole or an accessor index is refused whatever its key count', () => {
    const holed = (values) => {
      const list = [...values, undefined]
      delete list[values.length]
      list.extra = 'keeps the key count'
      return list
    }
    const accessorAt = (values) => {
      const list = [...values]
      const first = list[0]
      Object.defineProperty(list, '0', { get: () => first, enumerable: true, configurable: true })
      return list
    }
    const attacks = [
      (h) => { h.catalog.families = [{ capabilities: holed(h.rows) }] },
      (h) => { h.catalog.families = holed([{ capabilities: h.rows }]) },
      (h) => { h.catalog.families = [{ capabilities: accessorAt(h.rows) }] },
      (h) => { h.catalog.families = accessorAt([{ capabilities: h.rows }]) },
    ]
    for (const attack of attacks) {
      const h = fixture('solar-homeruns')
      h.props.openTool = h.row
      attack(h)
      const list = h.catalog.families
      expect(Reflect.ownKeys(list).length).toBe(list.length + 1)
      h.mount()
      noHost()
      expect(h.view.container.textContent).toContain(projectSolarWorkspaceReason('catalog_unavailable'))
      expect(last(h.rails).projectCatalog).toBeNull()
      expect(h.submit).not.toHaveBeenCalled()
      h.view.unmount()
    }
    // The same rows in dense lists open the graph host.
    const dense = fixture('solar-homeruns')
    dense.mount({ openTool: dense.row })
    expect(last(dense.graphs).tool).toBe(dense.row)
  })

  it('R13E-47 a render React abandons leaves the committed scope and its accepted run current', async () => {
    // A transition renders new props, a sibling suspends so nothing it rendered commits, and an urgent
    // update returns to the committed props. Only committed renders may retire a scope.
    const h = fixture('solar-string-add')
    const never = new Promise(() => {})
    function Blocker({ hold }) { if (hold) throw never; return null }
    let setFrame
    function Host() {
      const [frame, set] = useState({ props: { ...h.props, openTool: h.row }, hold: false })
      setFrame = set
      return <React.Suspense fallback={<p>Suspended fixture</p>}>
        <ProjectSolarWorkspace {...frame.props} />
        <Blocker hold={frame.hold} />
      </React.Suspense>
    }
    h.props = { ...h.props, openTool: h.row }
    h.view = render(<Host />)
    expect(await last(h.steps).onProjectSubmit(h.intent(), optionsFor(h))).toBe(h.outcome)
    const options = h.submit.mock.calls[0][1]
    expect(options.isCurrent()).toBe(true)
    const rendered = h.rails.length
    act(() => { React.startTransition(() => setFrame((frame) => ({ props: { ...frame.props, authorityKey: 'epoch-2' }, hold: true }))) })
    expect(h.rails.length).toBeGreaterThan(rendered)
    expect(screen.queryByText('Suspended fixture')).toBeNull()
    expect(screen.getAllByTestId('recorded-step')).toHaveLength(1)
    expect(options.isCurrent()).toBe(true)
    // The committed editor still writes while the transition is suspended: its handlers read committed props.
    expect(await last(h.steps).onProjectSubmit(h.intent(), optionsFor(h))).toBe(h.outcome)
    expect(h.submit).toHaveBeenCalledTimes(2)
    act(() => { setFrame((frame) => ({ props: { ...frame.props, authorityKey: 'epoch-1' }, hold: false })) })
    expect(screen.getAllByTestId('recorded-step')).toHaveLength(1)
    expect(options.isCurrent()).toBe(true)
    expect(await last(h.steps).onProjectSubmit(h.intent(), optionsFor(h))).toBe(h.outcome)
    expect(h.submit).toHaveBeenCalledTimes(3)
    // A committed change of authority still retires the scope and the run that followed it.
    act(() => { setFrame((frame) => ({ props: { ...frame.props, authorityKey: 'epoch-2' }, hold: false })) })
    expect(options.isCurrent()).toBe(false)
    expect(screen.queryByTestId('recorded-step')).toBeNull()
    h.view.unmount()
  })

  it('R13E-49 an editor that closes itself while it mounts stays closed', () => {
    // A child's layout effect runs before the workspace's own, so a close requested during the
    // commit that opened the editor must not be overwritten by that commit's presentation.
    const h = fixture('solar-string-add')
    const closes = []
    function ClosingEditor(props) {
      h.steps.push(props)
      useLayoutEffect(() => { closes.push(true); props.onClose() }, [])
      return <section data-testid="recorded-step" />
    }
    h.mount({ openTool: h.row, StepEditor: ClosingEditor })
    expect(closes).toHaveLength(1)
    expect(screen.queryByTestId('recorded-step')).toBeNull()
    expect(h.close).toHaveBeenCalledTimes(1)
    h.view.unmount()
  })

  it('R13E-48 aborting the scope signal ends an accepted run, mounted or not', async () => {
    for (const unmountFirst of [false, true]) {
      const h = fixture('solar-string-add')
      h.mount({ openTool: h.row })
      expect(await last(h.steps).onProjectSubmit(h.intent(), optionsFor(h))).toBe(h.outcome)
      const options = h.submit.mock.calls[0][1]
      expect(options.signal).toBe(h.scopeController.signal)
      expect(options.isCurrent()).toBe(true)
      if (unmountFirst) h.view.unmount()
      h.scopeController.abort()
      expect(options.isCurrent()).toBe(false)
      if (!unmountFirst) h.view.unmount()
    }
  })

  it('R13E-44 composition owns no Viewer or App mount and all new sentences use the frozen map', () => {
    const source = readFileSync(`${process.cwd()}/src/solar/ProjectSolarWorkspace.jsx`, 'utf8')
    const imports = [...source.matchAll(new RegExp("from ['\"]([^'\"]+)['\"]", 'g'))].map((match) => match[1])
    expect(imports).toEqual(['react', './solarDrawingBinding.js', './SolarSettingsForm.jsx', './ProjectSolarReadPanel.jsx'])
    expect(source).not.toMatch(new RegExp('(?:Viewer|createProjectSolarRunController|useProjectSolarScope|createSolarProjectClient|fetch\\s*\\(|readIntake|readVersions|contextView|onSubmit=)'))
    expect(source).not.toMatch(new RegExp('(?:export\\s+const|const)\\s+\\w*REASONS\\s*='))
    expect(source).toContain("projectSolarWorkspaceReason(reason || 'busy')")
    expect(source).not.toContain('This editor is not available in the project Solar workspace.')
    expect(source).not.toContain('\r')
    expect(source.endsWith('\n')).toBe(true)
    expect(source.endsWith('\n\n')).toBe(false)
    const app = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
    // App mounts no workspace; its bytes are not pinned, because unrelated App records move them.
    expect(app).not.toContain('ProjectSolarWorkspace')
    for (const [reason, changes] of [
      ['binding_invalid', { solarBinding: null }], ['scope_stale', { isCurrent: () => false }],
      ['client_unavailable', { projectClient: null }], ['catalog_unavailable', { projectCatalog: null }],
      ['unsupported', {}], ['host_unavailable', { FlowRail: undefined }],
    ]) {
      const h = fixture('solar-unknown')
      h.mount({ openTool: h.row, ...changes })
      expect(h.view.container.querySelector('p[role="status"]').textContent).toBe(projectSolarWorkspaceReason(reason))
      noHost()
      h.view.unmount()
    }
    expect(Object.keys(PROJECT_SOLAR_WORKSPACE_REASONS)).toHaveLength(12)
    expect(projectSolarWorkspaceReason('not_a_reason')).toBeNull()
  })
})
