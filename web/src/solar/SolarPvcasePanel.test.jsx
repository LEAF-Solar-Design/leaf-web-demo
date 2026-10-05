import React from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarPvcasePanel from './SolarPvcasePanel.jsx'
import { validatePvcaseCommittedGraph, PVCASE_REASONS } from './solarPvcaseModel.js'

const H = 'a'.repeat(64)
const B = 'b'.repeat(64)
const clone = (v) => JSON.parse(JSON.stringify(v))
const context = (extra = {}) => ({ tenantId: 'synthetic-tenant', orgId: 'synthetic-org', projectId: 'p', drawingId: 'solar', drawingVersion: 7, graphRev: 0, checkoutCapability: 'synthetic-checkout', ...extra })
const artifact = (assignment = false, v = 7, bytes = 274972) => ({
  schema: 'leaf.solar-artifact-ref.v1', artifact_id: assignment ? B : H, media_type: 'application/json',
  filename: assignment ? 'PVcaseAssignments.json' : 'pvcase-g33-source.json', byte_length: bytes,
  content_sha256: B, source_version: v, download: `/api/drawings/solar/artifacts/${assignment ? B : H}`,
})
const source = () => ({ schema: 'leaf.pvcase-g33-source.v1', kind: 'pvcase-g33', drawing_id: 'solar', project_id: 'p',
  source_version: 7, graph_rev: 0, graph_sha256: H, source: artifact() })
const receipt = (operation = 'convert', c = context(), jobId = 'job-1') => ({
  schema_version: 'leaf.solar-graph-commit.v1', adapter: 'local-graph-commit', tenant_id: c.tenantId,
  job_id: jobId, tool: `solar-pvcase-${operation}`, project_id: c.projectId, drawing_id: c.drawingId,
  request_sha256: H, new_version: { drawing_id: c.drawingId, version: c.drawingVersion + 6, parent: c.drawingVersion },
  before_graph_sha256: H, graph_sha256: B, intake_sha256: H, before_rev: c.graphRev, after_rev: c.graphRev + 1,
  drawing_changed: true, replayed: false,
})
const runEnvelope = (r) => ({ ok: true, tool: r.tool, version: '1.0.0', result: r, overlay: null, timing_ms: 1, cost: 0, error: null, degraded_mode: false })
const binding = (r = receipt(), c = context()) => ({ context: c, source: source(), tool: r.tool, jobId: r.job_id })
const readReceipt = (c = context()) => ({
  schema_version: 'leaf.solar-graph-read.v1', adapter: 'local-graph-read', tenant_id: c.tenantId, job_id: 'job-1',
  tool: 'solar-pvcase-export', project_id: c.projectId, drawing_id: c.drawingId, request_sha256: H, source_version: c.drawingVersion,
  representation: 'intake', graph_sha256: B, output_sha256: H, output_bytes: 900, drawing_changed: false,
  output: { summary: { schema: 'leaf.pvcase-g33-export.v1', panels: 2345, strings: 88, electrical_sizing: 'not-evaluated',
    source: { artifact_id: H, content_sha256: B } }, artifact: artifact(true, c.drawingVersion, 475691) },
})
const entityId = (kind, n) => `leaf:${kind}:00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
// Synthetic graph projection of the checked-in Roof inventory, not a server conversion fixture.
const roofIntakeText = readFileSync(resolve(process.cwd(), '..', 'docs', 'parity', 'evidence', 'rooftop', 'pvcase', 'intake.json'), 'utf8').trim()
const roofIntake = JSON.parse(roofIntakeText)
function roofGraph(solved = false, rev = 1) {
  const g = { graph_schema_version: 1, rev, parent_rev: rev - 1, project: { id: 'p' },
    frames: [], panels: [], strings: [], inverters: [], electrical_zones: [], routes: [], schedules: [], extra: { synthetic: true } }
  let n = 1
  for (const group of roofIntake.panel_groups) {
    const f = { id: entityId('frame', n++), kind: 'frame', rev, panel_refs: [], panel_assignments: [], matrix: [], sequences: [], electrical_zone_ref: null }
    for (const row of group.rows) for (const cell of row) {
      if (cell?.code !== 1 || !cell.id) continue
      const p = { id: entityId('panel', n++), kind: 'panel', rev, frame_ref: f.id, assignment: { string_ref: null, seq: null }, matrix_cell: { row: 0, col: f.panel_refs.length } }
      g.panels.push(p); f.panel_refs.push(p.id)
    }
    if (f.panel_refs.length) g.frames.push(f)
  }
  if (solved) {
    // Synthetic 88-string membership used only to exercise collection proofs.
    for (let i = 0, at = 0; i < 88; i++) {
      const length = Math.ceil((g.panels.length - at) / (88 - i))
      const refs = g.panels.slice(at, at + length).map((p, seq) => {
        p.assignment = { string_ref: entityId('string', n), seq }; return p.id
      })
      g.strings.push({ id: entityId('string', n++), kind: 'string', rev, module_count: length, ordered_panel_refs: refs, inverter_ref: null })
      at += length
    }
  }
  const panels = new Map(g.panels.map((p) => [p.id, p]))
  for (const f of g.frames) {
    f.panel_assignments = f.panel_refs.map((ref) => ({ panel_ref: ref, ...panels.get(ref).assignment, inverter_id: null, string_input_number: null }))
    f.matrix = [f.panel_assignments.map((a) => ({ ...a }))]
    f.sequences = g.strings.map((s) => ({ string_ref: s.id, ordered_panel_refs: s.ordered_panel_refs.filter((ref) => f.panel_refs.includes(ref)) })).filter((s) => s.ordered_panel_refs.length)
  }
  return g
}
const view = (g = roofGraph(), v = 13) => ({ intake: { solar_design_graph: g }, version: v, head: 99, latest: 100, error: null, degraded_mode: false })
const catalog = (operation) => ({ name: `solar-pvcase-${operation}`, version: '1.0.0', catalog_digest: H, capabilities: [] })
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r }); return { promise, resolve } }

const rows = ['convert', 'solve', 'export'].map(catalog)
const ok = (value) => ({ ok: true, status: 200, value })
function clientOf() {
  return {
    uploadSource: vi.fn(async () => ok(source())),
    convert: vi.fn(async ({ context: c }) => ok(receipt('convert', c))),
    solve: vi.fn(async ({ context: c }) => ok(receipt('solve', c))),
    exportAssignments: vi.fn(async ({ context: c }) => ok(readReceipt(c))),
    observeJob: vi.fn(async ({ context: c, operation }) => ok(receipt(operation, c))),
    readCommittedGraph: vi.fn(async ({ context: c, receipt: r }) => validatePvcaseCommittedGraph(view(roofGraph(r.tool.endsWith('solve'), r.after_rev), r.new_version.version), { context: c, receipt: r })),
  }
}
const props = (client, extra = {}) => ({ context: context(), client, catalogRows: rows, heldCheckout: true, ...extra })
const click = (name) => fireEvent.click(screen.getByRole('button', { name }))
const choose = (name = 'synthetic-roof.json') => fireEvent.change(screen.getByLabelText('G33 JSON file'), { target: { files: [new File(['{}'], name, { type: 'application/json' })] } })
async function admitted() { choose(); click('Upload G33 source'); await waitFor(() => expect(screen.getByRole('button', { name: 'Convert G33 source' })).toBeEnabled()) }
async function converted() { await admitted(); click('Convert G33 source'); await screen.findByText('11 panel groups, 2,345 panels, 0 strings, 0 inverters') }
async function solved() { await converted(); click('Run parity solve'); await screen.findByText('88 strings, 2,345 panels assigned to a string, 0 inverters with assignments') }
const verifiedDownload = async ({ ref }) => ok({ artifactId: ref.artifact_id, mediaType: ref.media_type, filename: ref.filename, byteLength: ref.byte_length, bytes: new Uint8Array(ref.byte_length) })
afterEach(() => { cleanup(); vi.restoreAllMocks() })
it('PVP01 requires an explicit conversion', async () => {
  const client = clientOf(); render(<SolarPvcasePanel {...props(client)} />)
  choose(); expect(client.uploadSource).not.toHaveBeenCalled(); expect(client.convert).not.toHaveBeenCalled()
  click('Upload G33 source'); await screen.findByLabelText('Source provenance')
  expect(screen.getByText('274,972')).toBeInTheDocument(); expect(screen.getByText('Source upload version')).toBeInTheDocument()
  expect(client.convert).not.toHaveBeenCalled()
  click('Convert G33 source'); await waitFor(() => expect(client.convert).toHaveBeenCalledTimes(1))
})
it('PVP02 shows conversion counts after read-back', async () => {
  const client = clientOf(); const pending = deferred(); const onResult = vi.fn()
  client.readCommittedGraph.mockImplementationOnce(({ context: c, receipt: r }) => pending.promise.then((v) => validatePvcaseCommittedGraph(v, { context: c, receipt: r })))
  render(<SolarPvcasePanel {...props(client, { onResult })} />)
  await admitted(); click('Convert G33 source')
  await screen.findByText('Committed version 13, graph revision 1')
  expect(screen.queryByText('11 panel groups, 2,345 panels, 0 strings, 0 inverters')).not.toBeInTheDocument()
  expect(onResult.mock.calls[0][0].kind).toBe('committed')
  await act(async () => pending.resolve(view()))
  await screen.findByText('11 panel groups, 2,345 panels, 0 strings, 0 inverters')
  expect(onResult.mock.calls.map(([v]) => v.kind)).toEqual(['committed', 'counts'])
})
it('PVP03 shows solve counts after read-back', async () => {
  const client = clientOf(); render(<SolarPvcasePanel {...props(client)} />)
  await converted(); expect(client.solve).not.toHaveBeenCalled()
  click('Run parity solve')
  await screen.findByText('88 strings, 2,345 panels assigned to a string, 0 inverters with assignments')
  expect(screen.getByText(PVCASE_REASONS.electrical)).toBeInTheDocument()
  expect(screen.queryByText('11 panel groups, 2,345 panels, 0 strings, 0 inverters')).not.toBeInTheDocument()
  expect(client.solve.mock.calls[0][0].context).toMatchObject({ drawingVersion: 13, graphRev: 1 })
})
it('PVP04 keeps one pending operation', async () => {
  const client = clientOf(); const pending = deferred()
  client.uploadSource.mockReturnValueOnce(pending.promise)
  const download = vi.fn(verifiedDownload); const save = vi.fn()
  render(<SolarPvcasePanel {...props(client, { download, save })} />)
  choose(); act(() => { click('Upload G33 source'); click('Upload G33 source') })
  expect(client.uploadSource).toHaveBeenCalledTimes(1); expect(screen.getByRole('status')).toHaveTextContent(PVCASE_REASONS.pending)
  await act(async () => pending.resolve(ok(source())))
  const runPending = deferred(); client.convert.mockReturnValueOnce(runPending.promise)
  act(() => { click('Convert G33 source'); click('Convert G33 source') })
  expect(client.convert).toHaveBeenCalledTimes(1)
  const readPending = deferred()
  client.readCommittedGraph.mockImplementationOnce(({ context: c, receipt: r }) => readPending.promise.then((v) => validatePvcaseCommittedGraph(v, { context: c, receipt: r })))
  await act(async () => runPending.resolve(ok(receipt())))
  await screen.findByText('Committed version 13, graph revision 1')
  click('Convert G33 source'); expect(client.convert).toHaveBeenCalledTimes(1)
  await act(async () => readPending.resolve(view()))
  const downloadPending = deferred(); download.mockReturnValueOnce(downloadPending.promise)
  act(() => { click('Download G33 source'); click('Download G33 source') })
  expect(download).toHaveBeenCalledTimes(1)
  await act(async () => downloadPending.resolve(await verifiedDownload({ ref: artifact() })))
  expect(save).toHaveBeenCalledTimes(1)
})
it('PVP05 retains selected input on refusal', async () => {
  const client = clientOf(); render(<SolarPvcasePanel {...props(client)} />)
  await converted()
  client.solve.mockResolvedValueOnce({ ok: false, code: 'BAD_PARAMS', status: 409, retryable: false })
  click('Run parity solve'); await screen.findByRole('alert')
  expect(screen.getByRole('alert')).toHaveTextContent(PVCASE_REASONS.request)
  expect(screen.getByText(PVCASE_REASONS.retained)).toBeInTheDocument()
  expect(screen.getByText('synthetic-roof.json')).toBeInTheDocument()
  expect(screen.getByText('Committed version 13, graph revision 1')).toBeInTheDocument()
  expect(screen.getByText('11 panel groups, 2,345 panels, 0 strings, 0 inverters')).toBeInTheDocument()
  choose('synthetic-other.json')
  expect(screen.queryByLabelText('Source provenance')).not.toBeInTheDocument()
  expect(screen.queryByText('Committed version 13, graph revision 1')).not.toBeInTheDocument()
})
it('PVP06 ignores old drawing responses', async () => {
  const client = clientOf(); const pending = deferred(); const onResult = vi.fn()
  client.convert.mockReturnValueOnce(pending.promise)
  const rendered = render(<SolarPvcasePanel {...props(client, { onResult })} />)
  await admitted(); click('Convert G33 source')
  rendered.rerender(<SolarPvcasePanel {...props(client, { context: context({ drawingId: 'other' }), onResult })} />)
  rendered.rerender(<SolarPvcasePanel {...props(client, { onResult })} />)
  await act(async () => pending.resolve(ok(receipt())))
  expect(client.readCommittedGraph).not.toHaveBeenCalled(); expect(onResult).not.toHaveBeenCalled()
  expect(screen.queryByText('Committed version 13, graph revision 1')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toBeEmptyDOMElement()
})
it('PVP07 ignores old project responses', async () => {
  const client = clientOf(); const pending = deferred(); const onResult = vi.fn()
  client.uploadSource.mockReturnValueOnce(pending.promise)
  const rendered = render(<SolarPvcasePanel {...props(client, { onResult })} />)
  choose(); click('Upload G33 source')
  rendered.rerender(<SolarPvcasePanel {...props(client, { context: context({ projectId: 'other' }), onResult })} />)
  await act(async () => pending.resolve(ok(source())))
  expect(screen.queryByLabelText('Source provenance')).not.toBeInTheDocument(); expect(onResult).not.toHaveBeenCalled()
})
it('PVP08 ignores version changes and unmounts', async () => {
  for (const unmount of [false, true]) for (const mode of ['run', 'read', 'download']) {
    const client = clientOf(); const pending = deferred(); const save = vi.fn(); const download = vi.fn(() => pending.promise)
    const onResult = vi.fn()
    if (mode === 'run') client.convert.mockReturnValueOnce(pending.promise)
    if (mode === 'read') client.readCommittedGraph.mockImplementationOnce(({ context: c, receipt: r }) => pending.promise.then((v) => validatePvcaseCommittedGraph(v, { context: c, receipt: r })))
    const rendered = render(<SolarPvcasePanel {...props(client, { save, download, onResult })} />)
    await admitted()
    if (mode === 'download') click('Download G33 source')
    else click('Convert G33 source')
    if (mode === 'read') await screen.findByText('Committed version 13, graph revision 1')
    const before = onResult.mock.calls.length
    if (unmount) rendered.unmount()
    else rendered.rerender(<SolarPvcasePanel {...props(client, { context: context({ drawingVersion: 8 }), save, download, onResult })} />)
    await act(async () => pending.resolve(mode === 'read' ? view() : mode === 'run' ? ok(receipt()) : await verifiedDownload({ ref: artifact() })))
    expect(save).not.toHaveBeenCalled(); expect(onResult).toHaveBeenCalledTimes(before)
    if (mode === 'run') expect(client.readCommittedGraph).not.toHaveBeenCalled()
    cleanup()
  }
})
it('PVP09 refuses absent catalog tools in words', async () => {
  for (const operation of ['convert', 'solve', 'export']) {
    const client = clientOf(); const rendered = render(<SolarPvcasePanel {...props(client)} />)
    if (operation === 'convert') await admitted()
    if (operation === 'solve') await converted()
    if (operation === 'export') await solved()
    for (const catalogRows of [[], rows.map((r) => r.name === 'solar-pvcase-' + operation ? { ...r, catalog_digest: '' } : r)]) {
      rendered.rerender(<SolarPvcasePanel {...props(client, { catalogRows })} />)
      const name = operation === 'convert' ? 'Convert G33 source' : operation === 'solve' ? 'Run parity solve' : 'Make assignment output'
      expect(screen.getByRole('button', { name })).toBeDisabled()
      expect(screen.getAllByText(PVCASE_REASONS.catalog).length).toBeGreaterThan(0)
      click(name)
    }
    expect(client[operation === 'export' ? 'exportAssignments' : operation]).not.toHaveBeenCalled(); cleanup()
  }
})
it('PVP10 downloads verified bytes with provenance', async () => {
  const client = clientOf(); const download = vi.fn(verifiedDownload); const save = vi.fn()
  render(<SolarPvcasePanel {...props(client, { download, save })} />)
  await solved(); click('Make assignment output'); await screen.findByText(/Assignment output version 19;/)
  click('Download G33 source'); await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Download assignment output' })).toBeEnabled())
  click('Download assignment output'); await waitFor(() => expect(save).toHaveBeenCalledTimes(2))
  expect(save.mock.calls[0].slice(1)).toEqual(['application/json', 'pvcase-g33-source.json'])
  expect(save.mock.calls[1].slice(1)).toEqual(['application/json', 'PVcaseAssignments.json'])
  expect(save.mock.calls[1][0].byteLength).toBe(475691)
  expect(download.mock.calls.map(([v]) => v.ref.source_version)).toEqual([7, 19])
  expect(download.mock.calls.every(([v]) => v.current === false)).toBe(true)
})
it('PVP11 recovers uncertain outcomes by observation', async () => {
  const client = clientOf()
  client.convert.mockResolvedValueOnce({ ok: false, code: 'timeout', retryable: true, jobId: 'job-1', status: null })
  render(<SolarPvcasePanel {...props(client)} />)
  await admitted(); click('Convert G33 source'); await screen.findByRole('button', { name: 'Check retained job' })
  expect(screen.getByRole('button', { name: 'Convert G33 source' })).toBeDisabled()
  click('Check retained job'); await screen.findByText('11 panel groups, 2,345 panels, 0 strings, 0 inverters')
  expect(client.convert).toHaveBeenCalledTimes(1); expect(client.observeJob).toHaveBeenCalledTimes(1)
  expect(client.observeJob.mock.calls[0][0].jobId).toBe('job-1')
  expect(client.readCommittedGraph.mock.calls[0][0].receipt.new_version.version).toBe(13)
})
it('PVP12 announces results and explains disabled controls', async () => {
  const client = clientOf(); const rendered = render(<SolarPvcasePanel {...props(client, { heldCheckout: false })} />)
  expect(screen.getByRole('button', { name: 'Upload G33 source' })).toHaveAccessibleDescription(PVCASE_REASONS.fileRequired)
  choose(); click('Upload G33 source'); await screen.findByLabelText('Source provenance')
  expect(screen.getByRole('status')).toHaveAttribute('aria-atomic', 'true')
  expect(screen.getByRole('status')).toHaveTextContent(PVCASE_REASONS.admitted)
  expect(screen.getByRole('button', { name: 'Convert G33 source' })).toHaveAccessibleDescription(PVCASE_REASONS.checkout)
  rendered.rerender(<SolarPvcasePanel {...props(client)} />)
  click('Convert G33 source'); await screen.findByText('11 panel groups, 2,345 panels, 0 strings, 0 inverters')
  expect(screen.getByRole('status')).toHaveTextContent(PVCASE_REASONS.converted)
  await waitFor(() => expect(screen.getByLabelText('G33 result')).toHaveFocus())
})
it('PVP13 preserves commits when counts cannot be read', async () => {
  const client = clientOf(); const onResult = vi.fn()
  client.readCommittedGraph.mockResolvedValueOnce({ ok: false, code: 'countsUnavailable' })
  render(<SolarPvcasePanel {...props(client, { onResult })} />)
  await admitted(); click('Convert G33 source'); await screen.findByRole('button', { name: 'Retry reading counts' })
  expect(screen.getByRole('alert')).toHaveTextContent(PVCASE_REASONS.countsUnavailable)
  expect(screen.getByText('Committed version 13, graph revision 1')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Run parity solve' })).toHaveAccessibleDescription(PVCASE_REASONS.countsUnavailable)
  expect(onResult.mock.calls.map(([v]) => v.kind)).toEqual(['committed'])
  act(() => { click('Retry reading counts'); click('Retry reading counts') }); await screen.findByText('11 panel groups, 2,345 panels, 0 strings, 0 inverters')
  expect(client.uploadSource).toHaveBeenCalledTimes(1); expect(client.convert).toHaveBeenCalledTimes(1)
  expect(client.readCommittedGraph.mock.calls.map(([v]) => v.receipt.new_version.version)).toEqual([13, 13])
})
it('PVP14 ignores late read-back after context change', async () => {
  for (const change of ['drawing', 'project', 'version', 'roundtrip', 'unmount']) {
    const client = clientOf(); const pending = deferred(); const onResult = vi.fn()
    client.readCommittedGraph.mockImplementationOnce(({ context: c, receipt: r }) => pending.promise.then((v) => validatePvcaseCommittedGraph(v, { context: c, receipt: r })))
    const rendered = render(<SolarPvcasePanel {...props(client, { onResult })} />)
    await admitted(); click('Convert G33 source'); await screen.findByText('Committed version 13, graph revision 1')
    const before = onResult.mock.calls.length
    if (change === 'unmount') rendered.unmount()
    else {
      const c = context(change === 'project' ? { projectId: 'other' } : change === 'version' ? { drawingVersion: 8 } : { drawingId: 'other' })
      rendered.rerender(<SolarPvcasePanel {...props(client, { context: c, onResult })} />)
      if (change === 'roundtrip') rendered.rerender(<SolarPvcasePanel {...props(client, { onResult })} />)
    }
    await act(async () => pending.resolve(view()))
    expect(onResult).toHaveBeenCalledTimes(before)
    expect(screen.queryByText('11 panel groups, 2,345 panels, 0 strings, 0 inverters')).not.toBeInTheDocument()
    expect(client.solve).not.toHaveBeenCalled()
    if (change !== 'unmount') expect(screen.getByRole('status')).toBeEmptyDOMElement()
    cleanup()
  }
})
it('PVP15 new admission clears conversion results', async () => {
  for (const identity of ['artifact', 'hash']) {
    const client = clientOf(); const pending = deferred(); render(<SolarPvcasePanel {...props(client)} />)
    await converted()
    const next = source()
    if (identity === 'artifact') { next.source.artifact_id = B; next.source.download = `/api/drawings/solar/artifacts/${B}` }
    else next.source.content_sha256 = H
    client.uploadSource.mockResolvedValueOnce(ok(next))
    click('Upload G33 source'); await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(PVCASE_REASONS.admitted))
    expect(screen.getByLabelText('Source provenance')).toHaveTextContent(identity === 'artifact' ? B : H)
    expect(screen.queryByText('Committed version 13, graph revision 1')).not.toBeInTheDocument()
    expect(screen.queryByText('11 panel groups, 2,345 panels, 0 strings, 0 inverters')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run parity solve' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Run parity solve' })).toHaveAccessibleDescription(PVCASE_REASONS.conversionRequired)
    expect(client.convert).toHaveBeenCalledTimes(1); expect(client.solve).not.toHaveBeenCalled()
    client.readCommittedGraph.mockImplementationOnce(({ context: c, receipt: r }) => pending.promise.then((v) => validatePvcaseCommittedGraph(v, { context: c, receipt: r })))
    click('Convert G33 source'); await screen.findByText('Committed version 13, graph revision 1')
    expect(client.convert.mock.calls[1][0].source.source).toEqual(next.source)
    expect(screen.getByRole('button', { name: 'Run parity solve' })).toBeDisabled()
    await act(async () => pending.resolve(view()))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run parity solve' })).toBeEnabled())
    cleanup()
  }
})
it('PVP16 new admission clears solve and download results', async () => {
  const client = clientOf(); const download = vi.fn(verifiedDownload); const save = vi.fn(); const onResult = vi.fn()
  render(<SolarPvcasePanel {...props(client, { download, save, onResult })} />)
  await solved(); click('Make assignment output'); await screen.findByText(/Assignment output version 19;/)
  for (const name of ['Download G33 source', 'Download assignment output']) {
    await waitFor(() => expect(screen.getByRole('button', { name })).toBeEnabled()); click(name)
    await waitFor(() => expect(screen.getByRole('button', { name })).toBeEnabled())
  }
  expect(save).toHaveBeenCalledTimes(2)
  const before = onResult.mock.calls.length
  const next = source(); next.source.artifact_id = B; next.source.content_sha256 = H; next.source.download = `/api/drawings/solar/artifacts/${B}`
  client.uploadSource.mockResolvedValueOnce(ok(next)); click('Upload G33 source')
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(PVCASE_REASONS.admitted))
  expect(screen.getByLabelText('Source provenance')).toHaveTextContent(B)
  expect(screen.queryByText('88 strings, 2,345 panels assigned to a string, 0 inverters with assignments')).not.toBeInTheDocument()
  expect(screen.queryByText('Committed version 19, graph revision 2')).not.toBeInTheDocument()
  expect(screen.queryByText(/Assignment output version 19;/)).not.toBeInTheDocument()
  for (const name of ['Make assignment output', 'Download assignment output']) {
    expect(screen.getByRole('button', { name })).toBeDisabled(); click(name)
  }
  expect(save).toHaveBeenCalledTimes(2); expect(onResult).toHaveBeenCalledTimes(before)
  expect(client.exportAssignments).toHaveBeenCalledTimes(1); expect(client.solve).toHaveBeenCalledTimes(1)
  click('Download G33 source'); await waitFor(() => expect(save).toHaveBeenCalledTimes(3))
  expect(download.mock.calls[2][0].ref).toEqual(next.source)
  expect(onResult).toHaveBeenCalledTimes(before)
})
it('PVP17 unchanged or refused admission preserves results', async () => {
  const client = clientOf(); render(<SolarPvcasePanel {...props(client)} />)
  await converted()
  client.uploadSource.mockResolvedValueOnce(ok(clone(source())))
  click('Upload G33 source'); await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(PVCASE_REASONS.admitted))
  expect(screen.getByText('Committed version 13, graph revision 1')).toBeInTheDocument()
  expect(screen.getByText('11 panel groups, 2,345 panels, 0 strings, 0 inverters')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Run parity solve' })).toBeEnabled()
  client.uploadSource.mockResolvedValueOnce({ ok: false, status: 400, code: 'refused', retryable: false })
  click('Upload G33 source'); await screen.findByRole('alert')
  expect(screen.getByRole('alert')).toHaveTextContent(PVCASE_REASONS.refused)
  expect(screen.getByText('Committed version 13, graph revision 1')).toBeInTheDocument()
  expect(screen.getByText('11 panel groups, 2,345 panels, 0 strings, 0 inverters')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Run parity solve' })).toBeEnabled()
  expect(screen.getByText('synthetic-roof.json')).toBeInTheDocument()
  expect(screen.getByLabelText('G33 JSON file').files[0].name).toBe('synthetic-roof.json')
  expect(client.convert).toHaveBeenCalledTimes(1); expect(client.solve).not.toHaveBeenCalled()
})
it('PVP18 count recovery survives download refusal', async () => {
  for (const operation of ['convert', 'solve']) {
    const client = clientOf(); const pending = deferred(); const onResult = vi.fn(); const save = vi.fn()
    const download = vi.fn(async () => ({ ok: false, status: 403, code: 'FORBIDDEN', retryable: false }))
    render(<SolarPvcasePanel {...props(client, { download, save, onResult })} />)
    if (operation === 'convert') await admitted()
    else await converted()
    client.readCommittedGraph.mockResolvedValueOnce({ ok: false, code: 'countsUnavailable' })
    click(operation === 'convert' ? 'Convert G33 source' : 'Run parity solve')
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(PVCASE_REASONS.countsUnavailable))
    const retained = client.readCommittedGraph.mock.calls.at(-1)[0]
    const before = client.readCommittedGraph.mock.calls.length
    const dependent = operation === 'convert' ? 'Run parity solve' : 'Make assignment output'
    click('Download G33 source'); await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(PVCASE_REASONS.forbidden))
    expect(save).not.toHaveBeenCalled()
    expect(screen.getByText(`Committed version ${retained.receipt.new_version.version}, graph revision ${retained.receipt.after_rev}`)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry reading counts' })).toBeEnabled()
    expect(screen.getByRole('button', { name: dependent })).toBeDisabled()
    expect(screen.getByRole('button', { name: dependent })).toHaveAccessibleDescription(PVCASE_REASONS.countsUnavailable)
    client.readCommittedGraph.mockImplementationOnce(({ context: c, receipt: r }) => pending.promise.then((v) => validatePvcaseCommittedGraph(v, { context: c, receipt: r })))
    act(() => { click('Retry reading counts'); click('Retry reading counts') })
    expect(client.readCommittedGraph).toHaveBeenCalledTimes(before + 1)
    const retried = client.readCommittedGraph.mock.calls.at(-1)[0]
    expect(retried.receipt).toBe(retained.receipt); expect(retried.context).toBe(retained.context)
    expect(client.uploadSource).toHaveBeenCalledTimes(1); expect(client.convert).toHaveBeenCalledTimes(1)
    expect(client.solve).toHaveBeenCalledTimes(operation === 'solve' ? 1 : 0); expect(client.exportAssignments).not.toHaveBeenCalled()
    await act(async () => pending.resolve(view(roofGraph(operation === 'solve', retained.receipt.after_rev), retained.receipt.new_version.version)))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry reading counts' })).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: dependent })).toBeEnabled()
    expect(onResult.mock.calls.at(-1)[0].kind).toBe('counts')
    cleanup()
  }
})
