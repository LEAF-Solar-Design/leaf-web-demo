import React, { StrictMode, useLayoutEffect, useRef } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import ProjectSolarReadPanel, { PROJECT_SOLAR_WORKSPACE_REASONS, projectSolarWorkspaceReason } from './ProjectSolarReadPanel.jsx'
import { createSolarProjectClient } from './solarProjectClient.js'
import { projectReadReason } from './solarReadResultModel.js'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const B = {
  kind: 'project',
  organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222',
  drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444',
}
const authorityKey = 'epoch-1'
const D = 'sha256:' + 'c'.repeat(64)
const intakeHash = 'a'.repeat(64)
const graphHash = 'b'.repeat(64)
const artifactId = 'd'.repeat(64)
const bodyHash = '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'
const ref = {
  organization_id: B.organization_id, project_id: B.project_id, drawing_id: B.drawing_id,
  source_version_id: B.input_version_id, source_intake_sha256: intakeHash, graph_sha256: graphHash,
  artifact_id: artifactId, media_type: 'application/json', filename: 'ElectricalSchedules.json',
  byte_length: 2, content_sha256: bodyHash, schema: 'leaf.solar-project-artifact-ref.v1',
  download: `/api/projects/${B.project_id}/drawings/${B.drawing_id}/solar-artifacts/${artifactId}`,
}
const output = { result: 57.4, units: 'A', artifact: ref }
function resultFor(tool) {
  return {
    schema: 'leaf.solar-project-read.v1', adapter: 'project-local-graph-read',
    organization_id: B.organization_id, project_id: B.project_id, drawing_id: B.drawing_id,
    input_version_id: B.input_version_id, input_intake_sha256: intakeHash, graph_sha256: graphHash,
    tool, tool_manifest_sha256: D, graph_project_id: 'private-graph-project',
    head_version_id: B.input_version_id, is_head: true, current_required: true,
    request_sha256: 'e'.repeat(64), output_sha256: 'f'.repeat(64),
    output_bytes: Buffer.byteLength(JSON.stringify(output)), drawing_changed: false, output,
  }
}
const stringSchema = { type: 'object', properties: {
  drawing_id: { type: 'string', default: 'foreign-drawing' },
  string_refs: { type: 'array', items: { type: 'string' }, default: [] },
}, required: ['drawing_id'] }
const scheduleSchema = { type: 'object', properties: {
  circuit_source: { type: 'string', enum: ['graph', 'labels'], default: 'graph' },
} }
const cableSchema = { type: 'object', properties: { project_location: { type: 'string', default: '' } } }
const success = (name = 'solar-string-data') => ({ ok: true, status: 200, data: { result: resultFor(name) } })
const refusal = (reason = 'UNRESOLVED_UNITS') => ({ ok: false, status: 409, reason,
  envelope: { error: { reason_code: reason, message: 'Private arbitrary server message' } } })
const rawFile = (bytes = new Uint8Array([123, 125])) => ({ ok: true, status: 200, data: {
  notModified: false, bytes, headers: {
    ETag: '"' + bodyHash + '"', 'X-Leaf-Artifact-Id': artifactId, 'Content-Type': 'application/json',
  },
} })
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function harness({ name = 'solar-string-data', schema = stringSchema, read, download } = {}) {
  let revision = 0
  let snapshot = { binding: B, authorityKey }
  const listeners = new Set()
  const unsubscribes = []
  const projectClient = {
    readSolar: vi.fn(read || (async () => success(name))),
    downloadArtifact: vi.fn(download || (async () => rawFile())),
    getSnapshot: vi.fn(() => snapshot),
    captureCatalogScope: vi.fn(() => { const captured = revision; return () => captured === revision }),
    subscribe: vi.fn((listener) => {
      listeners.add(listener)
      const unsubscribe = vi.fn(() => listeners.delete(listener))
      unsubscribes.push(unsubscribe)
      return unsubscribe
    }),
    prepareRun: vi.fn(), submitRun: vi.fn(), setScope: vi.fn(), dispose: vi.fn(),
  }
  const tool = { name, catalog_digest: D, params: schema, availability: { runnable: true } }
  const projectCatalog = { binding: B, authorityKey, client: projectClient,
    families: [{ capabilities: [tool] }], isCurrent: () => true }
  const save = vi.fn()
  const props = { tool, solarBinding: B, authorityKey, projectClient, projectCatalog,
    isCurrent: () => true, busy: false, save }
  return { props, projectClient, save, unsubscribes, listeners,
    change: (next = snapshot) => { snapshot = next; revision += 1; for (const listener of [...listeners]) listener() },
    notify: () => { for (const listener of [...listeners]) listener() },
  }
}
const button = () => screen.getByRole('button', { name: 'Read project output' })
const clickRead = () => fireEvent.click(button())
const flush = async () => { for (let index = 0; index < 16; index += 1) await Promise.resolve() }
const settle = async (pending, outcome) => act(async () => { pending.resolve(outcome); await flush() })
const assertNoOutput = (h) => {
  expect(screen.queryByTestId('solar-read-result')).toBeNull()
  expect(screen.queryByTestId('solar-read-download')).toBeNull()
  expect(h.projectClient.downloadArtifact).not.toHaveBeenCalled()
  expect(h.save).not.toHaveBeenCalled()
}
const assertNoRuns = (client) => {
  for (const method of ['prepareRun', 'submitRun', 'setScope', 'dispose']) expect(client[method]).not.toHaveBeenCalled()
}
function hashBuffer(bytes) {
  return new Uint8Array(createHash('sha256').update(bytes).digest()).buffer
}
function installDigest(implementation = async (_algorithm, bytes) => hashBuffer(bytes)) {
  const digest = vi.fn(implementation)
  vi.stubGlobal('crypto', { subtle: { digest } })
  return digest
}
const replaceRow = (props, tool) => ({ ...props, tool, projectCatalog: {
  ...props.projectCatalog, families: [{ capabilities: [tool] }],
} })

describe('project Solar read ownership', () => {
  it('R13E-18 string data uses R7 readSolar and renders R12 output', async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ result: resultFor('solar-string-data') }),
      { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const real = createSolarProjectClient({ fetchImpl })
    real.setScope(B, authorityKey)
    // R7 freezes its API; a transparent spy facade keeps each production method's behavior.
    const projectClient = { ...real, readSolar: vi.fn((options) => real.readSolar(options)),
      prepareRun: vi.fn((...args) => real.prepareRun(...args)), submitRun: vi.fn((...args) => real.submitRun(...args)) }
    const h = harness()
    const projectCatalog = { ...h.props.projectCatalog, client: projectClient }
    const view = render(<ProjectSolarReadPanel {...h.props} projectClient={projectClient} projectCatalog={projectCatalog} />)
    clickRead()
    await waitFor(() => expect(screen.getByTestId('solar-read-headline').textContent).toBe('57.4 A'))
    expect(projectClient.readSolar).toHaveBeenCalledTimes(1)
    const call = projectClient.readSolar.mock.calls[0][0]
    expect(call).toEqual({ tool: 'solar-string-data', params: { string_refs: [], drawing_id: B.drawing_id },
      catalogDigest: D, current: true, signal: expect.any(AbortSignal) })
    expect(call.signal.aborted).toBe(false)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe(`/api/projects/${B.project_id}/drawings/${B.drawing_id}/versions/${B.input_version_id}/solar-reads`)
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ tool: 'solar-string-data', params: { string_refs: [], drawing_id: B.drawing_id },
      catalog_digest: D, current: true })
    expect(init.signal).toBeInstanceOf(AbortSignal)
    expect(projectClient.prepareRun).not.toHaveBeenCalled()
    expect(projectClient.submitRun).not.toHaveBeenCalled()
    expect(h.save).not.toHaveBeenCalled()
    view.unmount()
    real.dispose()
  })

  it('R13E-19 electrical schedules use R6 current reads without graph submission', async () => {
    const h = harness({ name: 'solar-electrical-schedules', schema: scheduleSchema })
    render(<ProjectSolarReadPanel {...h.props} />)
    fireEvent.change(screen.getByRole('combobox', { name: 'Circuit source' }), { target: { value: '1' } })
    clickRead()
    await waitFor(() => expect(screen.getByTestId('solar-read-headline').textContent).toBe('57.4 A'))
    expect(h.projectClient.readSolar).toHaveBeenCalledTimes(1)
    expect(h.projectClient.readSolar.mock.calls[0][0]).toEqual({ tool: 'solar-electrical-schedules',
      params: { circuit_source: 'labels', drawing_id: B.drawing_id }, catalogDigest: D, current: true, signal: expect.any(AbortSignal) })
    assertNoRuns(h.projectClient)
  })

  it('R13E-20 cable export uses R6 current reads without graph submission', async () => {
    const h = harness({ name: 'solar-cable-export', schema: cableSchema })
    render(<ProjectSolarReadPanel {...h.props} />)
    fireEvent.change(screen.getByRole('textbox', { name: 'Project location' }), { target: { value: 'Test site' } })
    clickRead()
    await waitFor(() => expect(screen.getByTestId('solar-read-headline').textContent).toBe('57.4 A'))
    expect(h.projectClient.readSolar.mock.calls[0][0]).toEqual({ tool: 'solar-cable-export',
      params: { project_location: 'Test site', drawing_id: B.drawing_id }, catalogDigest: D, current: true, signal: expect.any(AbortSignal) })
    assertNoRuns(h.projectClient)
  })

  it('R13E-21 read parameters retain user input and use the exact binding and digest', async () => {
    for (const shape of ['params', 'params_schema', 'both']) {
      const h = harness({ read: async () => refusal() })
      const row = { ...h.props.tool }
      if (shape !== 'params') row.params_schema = stringSchema
      if (shape === 'params_schema') delete row.params
      const view = render(<ProjectSolarReadPanel {...replaceRow(h.props, row)} />)
      expect(screen.queryByRole('textbox', { name: 'Drawing id' })).toBeNull()
      const field = screen.getByRole('textbox', { name: 'String refs' })
      fireEvent.change(field, { target: { value: '["S1"]' } })
      clickRead()
      await waitFor(() => expect(screen.getByText('UNRESOLVED_UNITS')).toBeInTheDocument())
      expect(field.value).toBe('["S1"]')
      clickRead()
      await waitFor(() => expect(h.projectClient.readSolar).toHaveBeenCalledTimes(2))
      await act(flush)
      for (const [request] of h.projectClient.readSolar.mock.calls) {
        expect(request.params).toEqual({ string_refs: ['S1'], drawing_id: B.drawing_id })
        expect(request.catalogDigest).toBe(D)
        expect(Object.isFrozen(request.params)).toBe(true)
        expect(Object.isFrozen(request.params.string_refs)).toBe(true)
      }
      act(() => {
        fireEvent.change(field, { target: { value: '["S1"' } })
        fireEvent.submit(view.container.querySelector('form'))
      })
      expect(field.value).toBe('["S1"')
      expect(button()).toBeDisabled()
      expect(screen.getByRole('status')).toHaveTextContent(PROJECT_SOLAR_WORKSPACE_REASONS.parameters_invalid)
      fireEvent.submit(view.container.querySelector('form'))
      expect(h.projectClient.readSolar).toHaveBeenCalledTimes(2)
      assertNoOutput(h)
      cleanup()
    }
  })

  it('R13E-22 missing or stale read dependencies never start transport', async () => {
    const variants = [
      [(p) => ({ ...p, solarBinding: undefined }), 'binding_invalid'],
      [(p) => ({ ...p, solarBinding: { ...B, project_id: 'bad' } }), 'binding_invalid'],
      [(p) => ({ ...p, authorityKey: undefined }), 'client_unavailable'],
      [(p) => ({ ...p, authorityKey: ' ' }), 'client_unavailable'],
      [(p) => ({ ...p, authorityKey: 'x'.repeat(257) }), 'client_unavailable'],
      [(p) => ({ ...p, isCurrent: undefined }), 'scope_stale'],
      [(p) => ({ ...p, isCurrent: () => false }), 'scope_stale'],
      [(p) => ({ ...p, isCurrent: () => { throw new Error('stale') } }), 'scope_stale'],
      [(p) => ({ ...p, isCurrent: () => 1 }), 'scope_stale'],
      [(p) => ({ ...p, projectCatalog: undefined }), 'catalog_unavailable'],
      [(p) => ({ ...p, projectCatalog: { ...p.projectCatalog, binding: { ...B, project_id: B.organization_id } } }), 'catalog_unavailable'],
      [(p) => ({ ...p, projectCatalog: { ...p.projectCatalog, authorityKey: 'epoch-2' } }), 'catalog_unavailable'],
      [(p) => ({ ...p, projectCatalog: { ...p.projectCatalog, client: {} } }), 'catalog_unavailable'],
      [(p) => ({ ...p, projectCatalog: { ...p.projectCatalog, isCurrent: undefined } }), 'catalog_unavailable'],
      [(p) => ({ ...p, projectCatalog: { ...p.projectCatalog, isCurrent: () => false } }), 'catalog_unavailable'],
      [(p) => ({ ...p, projectCatalog: { ...p.projectCatalog, isCurrent: () => { throw new Error('stale') } } }), 'catalog_unavailable'],
      [(p) => ({ ...p, projectCatalog: { ...p.projectCatalog, isCurrent: () => 'true' } }), 'catalog_unavailable'],
      [(p) => ({ ...p, projectCatalog: Object.defineProperty({ ...p.projectCatalog }, 'isCurrent',
        { get() { throw new Error('HOSTILE_RECEIPT_ACCESSOR') }, enumerable: true }) }), 'catalog_unavailable'],
      [(p) => ({ ...p, tool: { ...p.tool } }), 'catalog_unavailable'],
      [(p) => ({ ...p, tool: null }), 'catalog_unavailable'],
      [(p) => replaceRow(p, Object.assign([], p.tool)), 'catalog_unavailable'],
      [(p) => replaceRow(p, { ...p.tool, catalog_digest: undefined }), 'catalog_unavailable'],
      [(p) => replaceRow(p, { ...p.tool, params: undefined }), 'catalog_unavailable'],
      [(p) => replaceRow(p, { ...p.tool, params: { type: 'object', properties: null } }), 'catalog_unavailable'],
      [(p) => replaceRow(p, { ...p.tool, params_schema: cableSchema }), 'catalog_unavailable'],
      [(p) => ({ ...p, projectCatalog: { ...p.projectCatalog, families: [{ capabilities: [p.tool,
        { ...p.tool, catalog_digest: 'sha256:' + '0'.repeat(64) }] }] } }), 'catalog_unavailable'],
      [(p) => ({ ...p, projectCatalog: { ...p.projectCatalog, families: [{ capabilities: [p.tool,
        { ...p.tool, params: cableSchema }] }] } }), 'catalog_unavailable'],
      [(p) => replaceRow(p, { ...p.tool, name: 'solar-write-layout' }), 'unsupported'],
      [(p) => ({ ...p, busy: true }), 'busy'],
    ]
    for (const method of ['readSolar', 'getSnapshot', 'subscribe', 'captureCatalogScope', 'downloadArtifact']) {
      variants.push([(p) => ({ ...p, projectClient: { ...p.projectClient, [method]: undefined } }), 'client_unavailable'])
    }
    for (const [change, reason] of variants) {
      const h = harness()
      const view = render(<ProjectSolarReadPanel {...change(h.props)} />)
      expect(screen.getByRole('status')).toHaveTextContent(PROJECT_SOLAR_WORKSPACE_REASONS[reason])
      fireEvent.submit(view.container.querySelector('form'))
      expect(h.projectClient.readSolar).not.toHaveBeenCalled()
      assertNoOutput(h)
      assertNoRuns(h.projectClient)
      cleanup()
    }
    for (const snapshot of [{ binding: B, authorityKey: 'other' },
      { binding: { ...B, drawing_id: B.organization_id }, authorityKey }]) {
      const h = harness()
      h.change(snapshot)
      const view = render(<ProjectSolarReadPanel {...h.props} />)
      expect(screen.getByRole('status')).toHaveTextContent(PROJECT_SOLAR_WORKSPACE_REASONS.scope_stale)
      fireEvent.submit(view.container.querySelector('form'))
      expect(h.projectClient.readSolar).not.toHaveBeenCalled()
      cleanup()
    }
    const h = harness({ read: async () => refusal() })
    const tool = { ...h.props.tool, availability: { runnable: false } }
    render(<ProjectSolarReadPanel {...replaceRow(h.props, tool)} />)
    expect(button()).not.toBeDisabled()
    clickRead()
    await waitFor(() => expect(screen.getByText('UNRESOLVED_UNITS')).toBeInTheDocument())
    expect(h.projectClient.readSolar).toHaveBeenCalledTimes(1)
    expect(PROJECT_SOLAR_WORKSPACE_REASONS).toEqual({
      binding_invalid: 'Open a project drawing version to read Solar output.',
      scope_stale: 'The drawing or session changed. Read the output again.',
      client_unavailable: 'Project Solar reads are not available in this view.',
      catalog_unavailable: 'Refresh the Solar tools before reading this output.',
      unsupported: 'This tool is not available in the project Solar workspace.',
      host_unavailable: 'This editor is not available in the project Solar workspace.',
      busy: 'Wait for the current project action to finish.',
      parameters_invalid: 'Check the read parameters before trying again.',
      idle: 'Read Solar output for the selected drawing version.',
      reading: 'Reading Solar output for the selected drawing version.',
      read_refused: 'This Solar read could not finish. Your inputs are kept.',
      read_action: 'Read project output',
    })
    expect(Object.isFrozen(PROJECT_SOLAR_WORKSPACE_REASONS)).toBe(true)
    for (const [key, text] of Object.entries(PROJECT_SOLAR_WORKSPACE_REASONS)) expect(projectSolarWorkspaceReason(key)).toBe(text)
    expect(projectSolarWorkspaceReason('unknown')).toBeNull()
    expect(projectSolarWorkspaceReason('__proto__')).toBeNull()
    const source = readFileSync(`${process.cwd()}/src/solar/ProjectSolarReadPanel.jsx`, 'utf8')
    const imports = [...source.matchAll(new RegExp('from [\x27\x22]([^\x27\x22]+)', 'g'))].map((match) => match[1])
    expect(imports).toEqual(['react', '../components/SchemaForm.jsx', './SolarReadResult.jsx', './solarDrawingBinding.js'])
    for (const forbidden of ['prepareRun(', 'submitRun(', 'setScope(', 'dispose(', 'fetch(', 'App.jsx', 'Viewer']) expect(source).not.toContain(forbidden)
  })

  it('R13E-23 a server read refusal displays its exact code and creates no download', async () => {
    for (const [read, code] of [[async () => refusal(), 'UNRESOLVED_UNITS'],
      [async () => refusal('not a code<script>'), 'HTTP_ERROR'], [async () => refusal(undefined), 'UNRESOLVED_UNITS'],
      [async () => ({ ok: false }), 'HTTP_ERROR'], [async () => refusal('A'.repeat(129)), 'HTTP_ERROR'],
      [async () => refusal('0BAD'), 'HTTP_ERROR'], [async () => refusal('_BAD'), 'HTTP_ERROR'],
      [async () => { throw new Error('Private arbitrary server message') }, 'SIP_R7_TRANSPORT_FAILED']]) {
      const h = harness({ read })
      render(<ProjectSolarReadPanel {...h.props} />)
      const field = screen.getByRole('textbox', { name: 'String refs' })
      fireEvent.change(field, { target: { value: '["S1"]' } })
      clickRead()
      await waitFor(() => expect(screen.getByText(code)).toBeInTheDocument())
      expect(screen.getByRole('status')).toHaveTextContent(PROJECT_SOLAR_WORKSPACE_REASONS.read_refused)
      expect(screen.queryByText('Private arbitrary server message')).toBeNull()
      expect(field.value).toBe('["S1"]')
      assertNoOutput(h)
      cleanup()
    }
  })

  it('R13E-24 a scope change mid-read never publishes the old scope view', async () => {
    for (const kind of ['binding', 'authority', 'receipt', 'parent', 'snapshot', 'captured']) {
      for (const outcome of [success(), refusal()]) {
        const pending = deferred()
        const h = harness({ read: () => pending.promise })
        let live = true
        const props = { ...h.props, isCurrent: () => live,
          projectCatalog: { ...h.props.projectCatalog, isCurrent: () => live } }
        const view = render(<ProjectSolarReadPanel {...props} />)
        clickRead()
        const signal = h.projectClient.readSolar.mock.calls[0][0].signal
        if (kind === 'binding') view.rerender(<ProjectSolarReadPanel {...props} solarBinding={{ ...B, input_version_id: B.organization_id }} />)
        if (kind === 'authority') view.rerender(<ProjectSolarReadPanel {...props} authorityKey="epoch-2" />)
        if (kind === 'receipt') {
          view.rerender(<ProjectSolarReadPanel {...props} projectCatalog={{ ...props.projectCatalog, isCurrent: () => false }} />)
        }
        if (kind === 'parent') { live = false; view.rerender(<ProjectSolarReadPanel {...props} />) }
        if (kind === 'snapshot') act(() => h.change({ binding: B, authorityKey: 'epoch-2' }))
        if (kind === 'captured') act(() => h.change())
        expect(signal.aborted).toBe(true)
        assertNoOutput(h)
        await settle(pending, outcome)
        assertNoOutput(h)
        expect(screen.queryByText('UNRESOLVED_UNITS')).toBeNull()
        cleanup()
      }
    }
    for (const failure of [false, 'true', 'throws']) {
      const pending = deferred()
      const h = harness({ read: () => pending.promise })
      let live = true
      const predicate = () => { if (live === 'throws') throw new Error('stale'); return live }
      render(<ProjectSolarReadPanel {...h.props} isCurrent={predicate} />)
      clickRead()
      live = failure
      await settle(pending, success())
      expect(h.projectClient.readSolar.mock.calls[0][0].signal.aborted).toBe(true)
      assertNoOutput(h)
      cleanup()
    }
    // An outcome accessor can change ownership between the await guard and the publication guard.
    for (const ok of [true, false]) {
      const pending = deferred()
      const h = harness({ read: () => pending.promise })
      let live = true
      const predicate = () => live
      render(<ProjectSolarReadPanel {...h.props} isCurrent={predicate} />)
      clickRead()
      const outcome = ok ? { ok: true, get data() { live = false; return { result: resultFor('solar-string-data') } } } :
        { ok: false, get reason() { live = false; return 'UNRESOLVED_UNITS' } }
      await settle(pending, outcome)
      live = true
      act(() => h.notify())
      assertNoOutput(h)
      expect(screen.queryByText('UNRESOLVED_UNITS')).toBeNull()
      cleanup()
    }
    const h = harness()
    h.projectClient.captureCatalogScope.mockReturnValue(() => false)
    render(<ProjectSolarReadPanel {...h.props} />)
    clickRead()
    expect(h.projectClient.readSolar).not.toHaveBeenCalled()
    expect(screen.getByRole('status')).toHaveTextContent(PROJECT_SOLAR_WORKSPACE_REASONS.scope_stale)
  })

  it('R13E-25 A to B to A cannot revive an earlier read', async () => {
    const old = deferred(), fresh = deferred()
    const h = harness({ read: vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise) })
    const view = render(<ProjectSolarReadPanel {...h.props} />)
    clickRead()
    const oldSignal = h.projectClient.readSolar.mock.calls[0][0].signal
    const other = { ...B, input_version_id: B.organization_id }
    act(() => h.change({ binding: other, authorityKey }))
    view.rerender(<ProjectSolarReadPanel {...h.props} solarBinding={other}
      projectCatalog={{ ...h.props.projectCatalog, binding: other }} />)
    act(() => h.change({ binding: B, authorityKey }))
    view.rerender(<ProjectSolarReadPanel {...h.props} />)
    expect(oldSignal.aborted).toBe(true)
    clickRead()
    expect(h.projectClient.readSolar).toHaveBeenCalledTimes(2)
    await settle(old, success())
    assertNoOutput(h)
    expect(button()).toBeDisabled()
    clickRead()
    expect(h.projectClient.readSolar).toHaveBeenCalledTimes(2)
    await settle(fresh, success())
    expect(screen.getByTestId('solar-read-headline').textContent).toBe('57.4 A')
    expect(h.save).not.toHaveBeenCalled()
  })

  it('R13E-26 tool digest client or parameter replacement discards pending output', async () => {
    for (const phase of ['pending', 'presented']) {
      for (const kind of ['tool', 'digest', 'client', 'receipt', 'schema', 'parameter', 'raw']) {
        const old = deferred(), fresh = deferred()
        const h = harness({ read: vi.fn().mockReturnValueOnce(old.promise).mockReturnValue(fresh.promise) })
        const view = render(<ProjectSolarReadPanel {...h.props} />)
        clickRead()
        const oldSignal = h.projectClient.readSolar.mock.calls[0][0].signal
        if (phase === 'presented') {
          await settle(old, success())
          expect(screen.getByTestId('solar-read-headline')).toBeInTheDocument()
        }
        let next = h.props, client = h.projectClient
        if (kind === 'tool') next = replaceRow(next, { ...next.tool, name: 'solar-cable-export', params: cableSchema })
        if (kind === 'digest') next = replaceRow(next, { ...next.tool, catalog_digest: 'sha256:' + '0'.repeat(64) })
        if (kind === 'schema') next = replaceRow(next, { ...next.tool, params: { ...stringSchema,
          properties: { ...stringSchema.properties, string_refs: { type: 'array', default: ['DEFAULT'] } } } })
        if (kind === 'receipt') next = { ...next, projectCatalog: { ...next.projectCatalog } }
        if (kind === 'client') {
          client = harness({ read: () => fresh.promise }).projectClient
          next = { ...next, projectClient: client, projectCatalog: { ...next.projectCatalog, client } }
        }
        if (kind === 'parameter' || kind === 'raw') {
          fireEvent.change(screen.getByRole('textbox', { name: 'String refs' }),
            { target: { value: kind === 'parameter' ? '["NEW"]' : '["NEW"' } })
        } else view.rerender(<ProjectSolarReadPanel {...next} />)
        // A completed read has released its slot; only a pending read still owns an abortable controller.
        if (phase === 'pending') expect(oldSignal.aborted).toBe(true)
        assertNoOutput(h)
        if (phase === 'pending') await settle(old, success())
        assertNoOutput(h)
        if (kind === 'raw') {
          expect(screen.getByRole('textbox', { name: 'String refs' }).value).toBe('["NEW"')
          expect(button()).toBeDisabled()
          fireEvent.change(screen.getByRole('textbox', { name: 'String refs' }), { target: { value: '["FIXED"]' } })
        }
        clickRead()
        const request = client.readSolar.mock.calls.at(-1)[0]
        expect(request.tool).toBe(next.tool.name)
        expect(request.catalogDigest).toBe(next.tool.catalog_digest)
        expect(request.params.drawing_id).toBe(B.drawing_id)
        if (kind === 'parameter') expect(request.params.string_refs).toEqual(['NEW'])
        if (kind === 'raw') expect(request.params.string_refs).toEqual(['FIXED'])
        if (kind === 'schema') expect(request.params.string_refs).toEqual(['DEFAULT'])
        if (kind === 'tool') expect(request.params).toEqual({ drawing_id: B.drawing_id, project_location: '' })
        const data = { ...resultFor(next.tool.name), tool_manifest_sha256: next.tool.catalog_digest }
        await settle(fresh, { ok: true, status: 200, data: { result: data } })
        expect(screen.getByTestId('solar-read-headline').textContent).toBe('57.4 A')
        expect(h.save).not.toHaveBeenCalled()
        cleanup()
      }
    }
  })

  it('R13E-27 two clicks start one read and pending state remains visible', async () => {
    const pending = deferred()
    const h = harness({ read: () => pending.promise })
    const view = render(<ProjectSolarReadPanel {...h.props} />)
    const target = button()
    const form = view.container.querySelector('form')
    act(() => { target.click(); fireEvent.submit(form); target.click(); fireEvent.submit(form) })
    expect(h.projectClient.readSolar).toHaveBeenCalledTimes(1)
    expect(target).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent(PROJECT_SOLAR_WORKSPACE_REASONS.reading)
    view.rerender(<ProjectSolarReadPanel {...h.props} busy />)
    expect(h.projectClient.readSolar.mock.calls[0][0].signal.aborted).toBe(false)
    expect(screen.getByRole('status')).toHaveTextContent(PROJECT_SOLAR_WORKSPACE_REASONS.reading)
    assertNoOutput(h)
    await settle(pending, success())
    expect(screen.getByTestId('solar-read-headline').textContent).toBe('57.4 A')
  })

  it('R13E-28 unmount and StrictMode cleanup prevent late publication', async () => {
    const pending = deferred()
    const h = harness({ read: () => pending.promise })
    const view = render(<ProjectSolarReadPanel {...h.props} />)
    clickRead()
    const signal = h.projectClient.readSolar.mock.calls[0][0].signal
    view.unmount()
    expect(signal.aborted).toBe(true)
    expect(h.listeners.size).toBe(0)
    expect(h.unsubscribes[0]).toHaveBeenCalledTimes(1)
    await settle(pending, success())
    expect(h.save).not.toHaveBeenCalled()
    expect(screen.queryByTestId('solar-read-result')).toBeNull()

    const old = deferred(), fresh = deferred()
    const strict = harness({ read: vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise) })
    const setups = vi.fn(), cleanups = vi.fn()
    function Host() {
      const started = useRef(false)
      useLayoutEffect(() => {
        setups()
        if (!started.current) { started.current = true; fireEvent.submit(document.querySelector('form')) }
        return () => cleanups()
      }, [])
      return <ProjectSolarReadPanel {...strict.props} />
    }
    render(<StrictMode><Host /></StrictMode>)
    expect(setups).toHaveBeenCalledTimes(2)
    expect(cleanups).toHaveBeenCalledTimes(1)
    expect(strict.projectClient.readSolar).toHaveBeenCalledTimes(1)
    expect(strict.projectClient.readSolar.mock.calls[0][0].signal.aborted).toBe(true)
    expect(strict.unsubscribes[0]).toHaveBeenCalledTimes(1)
    clickRead()
    expect(strict.projectClient.readSolar).toHaveBeenCalledTimes(2)
    await settle(old, success())
    assertNoOutput(strict)
    expect(button()).toBeDisabled()
    await settle(fresh, success())
    expect(screen.getByTestId('solar-read-headline').textContent).toBe('57.4 A')
    expect(strict.save).not.toHaveBeenCalled()
  })

  it('R13E-29 verified downloads use the unchanged R12 save boundary', async () => {
    for (const kind of ['verified', 'mismatch', 'staleDigest']) {
      const pendingDigest = deferred()
      const digest = installDigest(kind === 'staleDigest' ? () => pendingDigest.promise : undefined)
      const h = harness({ download: async () => rawFile(kind === 'mismatch' ? new Uint8Array([91, 93]) : undefined) })
      render(<ProjectSolarReadPanel {...h.props} />)
      clickRead()
      await waitFor(() => expect(screen.getByTestId('solar-read-headline').textContent).toBe('57.4 A'))
      expect(h.projectClient.downloadArtifact).not.toHaveBeenCalled()
      expect(h.save).not.toHaveBeenCalled()
      fireEvent.click(screen.getByTestId('solar-read-download'))
      await waitFor(() => expect(digest).toHaveBeenCalledTimes(1))
      if (kind === 'staleDigest') {
        act(() => h.change({ binding: B, authorityKey: 'epoch-2' }))
        await settle(pendingDigest, hashBuffer(new Uint8Array([123, 125])))
        expect(screen.queryByTestId('solar-read-result')).toBeNull()
        expect(h.save).not.toHaveBeenCalled()
      } else if (kind === 'mismatch') {
        await waitFor(() => expect(screen.getByText(projectReadReason('SIP_R12_ARTIFACT_MISMATCH'))).toBeInTheDocument())
        expect(h.save).not.toHaveBeenCalled()
      } else {
        await waitFor(() => expect(h.save).toHaveBeenCalledTimes(1))
        expect(h.save.mock.calls[0]).toEqual([new Uint8Array([123, 125]), 'application/json', 'ElectricalSchedules.json'])
        expect(screen.getByText('Downloaded ElectricalSchedules.json.')).toBeInTheDocument()
      }
      expect(h.projectClient.downloadArtifact).toHaveBeenCalledTimes(1)
      expect(h.projectClient.downloadArtifact.mock.calls[0][0]).toEqual(ref)
      expect(h.projectClient.downloadArtifact.mock.calls[0][1].current).toBe(true)
      assertNoRuns(h.projectClient)
      cleanup()
      vi.unstubAllGlobals()
    }
    const source = readFileSync(`${process.cwd()}/src/solar/ProjectSolarReadPanel.jsx`, 'utf8')
    expect(source).toContain('save={save}')
    for (const forbidden of ['fetch(', 'createObjectURL', 'createElement', 'subtle.digest',
      'validateSolarProjectArtifactRef', 'createSolarProjectArtifactClient', 'save(', 'downloadArtifact(']) {
      expect(source).not.toContain(forbidden)
    }
  })
})
