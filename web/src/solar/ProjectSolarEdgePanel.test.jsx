import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { createHash, webcrypto } from 'node:crypto'
import ProjectSolarEdgePanel from './ProjectSolarEdgePanel.jsx'
import { createSolarProjectClient } from './solarProjectClient.js'

const O = '11111111-1111-1111-1111-111111111111', P = '22222222-2222-2222-2222-222222222222'
const D = '33333333-3333-3333-3333-333333333333', V = '44444444-4444-4444-4444-444444444444'
const NEXT = '55555555-5555-5555-5555-555555555555'
const binding = { kind: 'project', organization_id: O, project_id: P, drawing_id: D, input_version_id: V }
const digest = 'sha256:' + 'a'.repeat(64), sourceId = 'b'.repeat(64), reportId = 'c'.repeat(64)
const counts = { pdf_matrices: 1, pdf_panels: 2, matchable_grids: 1, bridge_grids: 0, frames: 1,
  matched_frames: 1, group_strings: 1, bridge_strings: 0, strings: 1, assigned_panels: 2, unassigned_panels: 0, partial_strings: 0 }
const ready = { admission_checked: true, admission: { status_code: 200, error: null },
  entitled: true, engine_ready: true, implemented: true, input_ready: true, runnable: true, refusal_reasons: [] }
const row = { name: 'solar-solaredge-accept', catalog_digest: digest, availability: ready }
const reply = (value) => ({ ok: true, status: 200, json: async () => value })
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done }); return { promise, resolve } }
function edge(selected, report = false) {
  const id = report ? reportId : sourceId
  const scope = { organization_id: selected.organization_id, project_id: selected.project_id, drawing_id: selected.drawing_id }
  return { ...scope, input_version_id: selected.input_version_id, source_intake_sha256: 'd'.repeat(64),
    graph_sha256: 'e'.repeat(64), graph_project_id: 'design-1',
    schema: report ? 'leaf.solar-project-solaredge-report-result.v1' : 'leaf.solar-project-import-source.v1',
    kind: report ? 'solaredge-report' : 'solaredge-pdf',
    ...(report ? { counts, source_artifact_id: sourceId } : { page_count: 1 }),
    [report ? 'report' : 'source']: { ...scope, source_version_id: selected.input_version_id,
      source_intake_sha256: 'd'.repeat(64), graph_sha256: 'e'.repeat(64), artifact_id: id,
      content_sha256: 'f'.repeat(64), byte_length: 100, schema: 'leaf.solar-project-artifact-ref.v1',
      media_type: report ? 'application/json' : 'application/pdf', filename: report ? 'solaredge-report.json' : 'solaredge-source.pdf',
      download: `/api/projects/${P}/drawings/${D}/solar-artifacts/${id}` } }
}
const clients = []
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.dispose()); vi.restoreAllMocks(); vi.unstubAllGlobals() })
function setup({ isHead = true, reportBytes, downloadResponse, reportCounts } = {}) {
  let client
  const fetchImpl = vi.fn(async (url) => {
    const selected = client.getSnapshot().binding
    if (url.includes('/context?')) return reply({ context: { ...selected, head_version_id: isHead ? selected.input_version_id : NEXT,
      is_head: isHead, intake_sha256: 'd'.repeat(64) }, intake: { solar_design_graph: { rev: 7 }, solar_design_graph_sha256: 'e'.repeat(64) } })
    if (url.endsWith('/solaredge-pdf')) return reply(edge(selected))
    if (url.endsWith('/report')) {
      const value = edge(selected, true)
      if (reportCounts) value.counts = reportCounts
      if (reportBytes) Object.assign(value.report, { byte_length: reportBytes.length,
        content_sha256: createHash('sha256').update(reportBytes).digest('hex') })
      return reply(value)
    }
    if (url.includes('/solar-artifacts/')) return downloadResponse
      ? downloadResponse() : new Response(reportBytes, { headers: { 'Content-Type': 'application/json' } })
    if (url.includes('/api/capabilities')) return reply({ families: [{ capabilities: [row] }] })
    throw new Error('Unexpected request')
  })
  client = createSolarProjectClient({ fetchImpl })
  client.setScope(binding, 'session-1'); clients.push(client)
  const props = { solarBinding: binding, authorityKey: 'session-1', projectClient: client, catalogDigest: digest,
    newProjectIntentKey: vi.fn(() => 'accept-1'), onProjectSubmit: vi.fn(async () => ({ ok: true })), isCurrent: () => true }
  const view = render(<ProjectSolarEdgePanel {...props} />)
  return { client, fetchImpl, props, view }
}
async function upload() {
  const file = await screen.findByLabelText('SolarEdge layout PDF')
  fireEvent.change(file, { target: { files: [new File(['%PDF'], 'layout.pdf', { type: 'application/pdf' })] } })
  fireEvent.click(screen.getByRole('button', { name: 'Upload', exact: true }))
  await screen.findByLabelText('Alignment tolerance')
}
async function report() {
  fireEvent.change(screen.getByLabelText('Alignment tolerance'), { target: { value: '0.5' } })
  fireEvent.click(screen.getByRole('button', { name: 'Build report' }))
  await screen.findByRole('region', { name: 'Report review' })
}

it('uploads, matches, previews and hands one canonical acceptance to the existing run rail', async () => {
  const { props, fetchImpl } = setup()
  await upload(); await report()
  expect(screen.getByText(`Report source version: ${V}.`)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Accept tracking labels' }))
  await waitFor(() => expect(props.onProjectSubmit).toHaveBeenCalledTimes(1))
  const [intent, options] = props.onProjectSubmit.mock.calls[0]
  expect(intent).toMatchObject({ binding, authorityKey: 'session-1', tool: row.name,
    params: { drawing_id: D, expected_rev: 7, report_artifact_id: reportId }, catalogDigest: digest, idempotencyKey: 'accept-1' })
  expect(options.binding).toEqual(binding)
  expect(options.isCurrent()).toBe(true)
  expect(fetchImpl.mock.calls.every(([url]) => !url.endsWith('/api/run'))).toBe(true)
})

it('reuses the exact prepared identity after a refused handoff rather than minting a new acceptance', async () => {
  const { props } = setup()
  props.onProjectSubmit.mockResolvedValueOnce({ ok: false }).mockResolvedValueOnce({ ok: true })
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Accept tracking labels' }))
  await screen.findByText('Tracking labels were not staged for acceptance. Try again.')
  fireEvent.click(screen.getByRole('button', { name: 'Accept tracking labels' }))
  await waitFor(() => expect(props.onProjectSubmit).toHaveBeenCalledTimes(2))
  expect(props.newProjectIntentKey).toHaveBeenCalledTimes(1)
  expect(props.onProjectSubmit.mock.calls[0][0]).toBe(props.onProjectSubmit.mock.calls[1][0])
})

it('discards a preview when selection changes and never retargets acceptance', async () => {
  const { props, client, fetchImpl, view } = setup()
  await upload(); await report()
  const delayed = deferred()
  fetchImpl.mockImplementationOnce(() => delayed.promise)
  fireEvent.click(screen.getByRole('button', { name: 'Accept tracking labels' }))
  const signal = fetchImpl.mock.calls.at(-1)[1].signal
  const next = { ...binding, input_version_id: NEXT }
  act(() => { client.setScope(next, 'session-1'); view.rerender(<ProjectSolarEdgePanel {...props} solarBinding={next} />) })
  expect(signal.aborted).toBe(true)
  await act(async () => { delayed.resolve(reply({ families: [{ capabilities: [row] }] })); await Promise.resolve() })
  expect(props.onProjectSubmit).not.toHaveBeenCalled()
  expect(screen.queryByRole('region', { name: 'Report review' })).toBeNull()
})

it('preserves historical source inspection while refusing to accept into an older version', async () => {
  const { props } = setup({ isHead: false })
  await upload(); await report()
  expect(screen.getByRole('button', { name: 'Accept tracking labels' }).disabled).toBe(true)
  expect(props.onProjectSubmit).not.toHaveBeenCalled()
})

it('retains the review when the real catalog preview refuses checkout or policy', async () => {
  const { props, fetchImpl } = setup()
  await upload(); await report()
  fetchImpl.mockResolvedValueOnce(reply({ families: [{ capabilities: [{ ...row, availability: {
    ...ready, runnable: false, admission: { status_code: 409, error: { reason_code: 'CHECKOUT_REQUIRED' } }, refusal_reasons: ['CHECKOUT_REQUIRED'] } }] }] }))
  fireEvent.click(screen.getByRole('button', { name: 'Accept tracking labels' }))
  await screen.findByText('Tracking labels were not staged for acceptance. Try again.')
  expect(screen.getByRole('region', { name: 'Report review' })).toBeTruthy()
  expect(props.onProjectSubmit).not.toHaveBeenCalled()
})

const reportBytes = new TextEncoder().encode(JSON.stringify({ schema: 'leaf.solar-project-solaredge-report.v1',
  source: { content_sha256: '9'.repeat(64) }, strings: [{ panel_refs: ['p1', 'p2'], pdf_inverter_id: 4 }] }))
function downloads() {
  vi.stubGlobal('crypto', webcrypto)
  const createObjectURL = vi.fn(() => 'blob:verified-report'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', Object.assign(class extends URL {}, { createObjectURL, revokeObjectURL }))
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  return { createObjectURL, revokeObjectURL, click }
}

function matchReport(overrides = {}) {
  return new TextEncoder().encode(JSON.stringify({ schema: 'leaf.solar-project-solaredge-report.v1',
    organization_id: O, project_id: P, drawing_id: D, source_version_id: V,
    graph_sha256: 'e'.repeat(64), source_intake_sha256: 'd'.repeat(64),
    source: { artifact_id: sourceId }, matches: [
      { frame_ref: 'roof-east', pdf_grid: 0, candidates: [0], candidate_count: 1 },
      { frame_ref: 'roof-west', pdf_grid: null, candidates: [], candidate_count: 0, status: 'unmatched' },
      { frame_ref: 'roof-south', pdf_grid: null, candidates: [1, 2], candidate_count: 2, status: 'ambiguous' },
    ], ...overrides }))
}

it('S02 review displays competing groups with one candidate without choosing or accepting it', async () => {
  const save = downloads()
  const competitionCounts = { ...counts, frames: 2, matched_frames: 0,
    group_strings: 0, strings: 0, assigned_panels: 0, unassigned_panels: 4 }
  const frameRefs = [1, 2].map((n) => `leaf:frame:00000000-0000-4000-8000-${String(n).padStart(12, '0')}`)
  const bytes = matchReport({ graph_project_id: 'design-1', row_angle: 0,
    source: { artifact_id: sourceId, content_sha256: 'f'.repeat(64), byte_length: 100 },
    request: { alignment_tolerance: 0.5, selection_order: 'unknown' }, counts: competitionCounts,
    matches: frameRefs.map((frame_ref) => ({ frame_ref, status: 'ambiguous',
      pdf_grid: null, pdf_matrix: null, pdf_sub_grid: null, candidates: [0], candidate_count: 1 })),
    strings: [], unassigned_panel_refs: [1, 2, 3, 4].map((n) =>
      `leaf:panel:00000000-0000-4000-8000-${String(n).padStart(12, '0')}`) })
  const { props } = setup({ reportBytes: bytes, reportCounts: competitionCounts })
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Inspect group matches' }))
  const table = await screen.findByRole('table', { name: 'SolarEdge group matches' })
  expect(within(table).getAllByText('Ambiguous')).toHaveLength(2)
  expect(within(table).getAllByText('PDF grid 0')).toHaveLength(2)
  for (const ref of frameRefs) expect(within(table).getByText(ref)).toBeTruthy()
  expect(props.onProjectSubmit).not.toHaveBeenCalled()
  expect(save.createObjectURL).not.toHaveBeenCalled()
})

it('S02 correction builds a new immutable report, accepts its identity separately and resets to automatic review', async () => {
  downloads()
  const { fetchImpl, props } = setup()
  const originalFetch = fetchImpl.getMockImplementation(), correctedId = '8'.repeat(64)
  const automaticBytes = matchReport({ matches: [{ frame_ref: 'roof-east', pdf_grid: null,
    pdf_matrix: null, pdf_sub_grid: null, candidates: [0, 1], candidate_count: 2, status: 'ambiguous' }] })
  const correctedBytes = matchReport({ matches: [{ frame_ref: 'roof-east', pdf_grid: 1,
    pdf_matrix: 1, pdf_sub_grid: -1, candidates: [0, 1], candidate_count: 2 }] })
  fetchImpl.mockImplementation(async (url, options) => {
    if (url.endsWith('/report')) {
      const choices = JSON.parse(options.body).associations
      const corrected = choices?.['roof-east'] === 1, data = edge(binding, true)
      const bytes = corrected ? correctedBytes : automaticBytes
      const id = corrected ? correctedId : reportId
      data.counts = { ...counts, matched_frames: corrected ? 1 : 0, group_strings: corrected ? 1 : 0,
        strings: corrected ? 1 : 0, assigned_panels: corrected ? 2 : 0, unassigned_panels: corrected ? 0 : 2 }
      Object.assign(data.report, { artifact_id: id, download: `/api/projects/${P}/drawings/${D}/solar-artifacts/${id}`,
        byte_length: bytes.length, content_sha256: createHash('sha256').update(bytes).digest('hex') })
      return reply(data)
    }
    if (url.includes('/solar-artifacts/')) return new Response(url.includes(`/solar-artifacts/${correctedId}?`) ? correctedBytes : automaticBytes)
    return originalFetch(url, options)
  })
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Inspect group matches' }))
  const choice = await screen.findByLabelText('PDF choice for roof-east')
  fireEvent.change(choice, { target: { value: '1' } })
  expect(screen.getByRole('button', { name: 'Accept tracking labels' }).disabled).toBe(true)
  expect(props.onProjectSubmit).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Build report' }))
  await screen.findByRole('button', { name: 'Inspect group matches' })
  const reportCalls = () => fetchImpl.mock.calls.filter(([url]) => url.endsWith('/report'))
  expect(JSON.parse(reportCalls().at(-1)[1].body).associations).toEqual({ 'roof-east': 1 })
  fireEvent.click(screen.getByRole('button', { name: 'Inspect group matches' }))
  await screen.findByRole('table', { name: 'SolarEdge group matches' })
  expect(screen.getByLabelText('PDF choice for roof-east').value).toBe('1')
  expect(screen.getByText('Matched')).toBeTruthy()
  expect(props.onProjectSubmit).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Accept tracking labels' }))
  await waitFor(() => expect(props.onProjectSubmit).toHaveBeenCalledTimes(1))
  expect(props.onProjectSubmit.mock.calls[0][0].params.report_artifact_id).toBe(correctedId)
  fireEvent.click(screen.getByRole('button', { name: 'Clear association choices' }))
  expect(screen.getByRole('button', { name: 'Accept tracking labels' }).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Build report' }))
  await screen.findByRole('button', { name: 'Inspect group matches' })
  expect(JSON.parse(reportCalls().at(-1)[1].body)).not.toHaveProperty('associations')
  fireEvent.click(screen.getByRole('button', { name: 'Inspect group matches' }))
  await screen.findByText('Ambiguous')
  expect(screen.getByLabelText('PDF choice for roof-east').value).toBe('')
  expect(props.onProjectSubmit).toHaveBeenCalledTimes(1)
})

it('S02 correction refuses duplicate grid choices and clears choices when report inputs change', async () => {
  downloads()
  const bytes = matchReport({ matches: ['roof-east', 'roof-west'].map((frame_ref) => ({ frame_ref,
    pdf_grid: null, candidates: [0, 1], candidate_count: 2, status: 'ambiguous' })) })
  const { fetchImpl, props } = setup({ reportBytes: bytes })
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Inspect group matches' }))
  fireEvent.change(await screen.findByLabelText('PDF choice for roof-east'), { target: { value: '0' } })
  const west = screen.getByLabelText('PDF choice for roof-west')
  expect(within(west).getByRole('option', { name: 'Candidate grid 0' }).disabled).toBe(true)
  fireEvent.change(west, { target: { value: '0' } })
  expect(west.value).toBe('')
  fireEvent.change(screen.getByLabelText('Alignment tolerance'), { target: { value: '2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Build report' }))
  await screen.findByRole('button', { name: 'Inspect group matches' })
  const body = JSON.parse(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/report')).at(-1)[1].body)
  expect(body.alignment_tolerance).toBe(2)
  expect(body).not.toHaveProperty('associations')
  fireEvent.click(screen.getByRole('button', { name: 'Inspect group matches' }))
  expect((await screen.findByLabelText('PDF choice for roof-east')).value).toBe('')
  expect(props.onProjectSubmit).not.toHaveBeenCalled()
})

it('S02 correction controls stay locked while canonical acceptance is in flight', async () => {
  downloads()
  const { props } = setup({ reportBytes: matchReport() })
  const staging = deferred()
  props.onProjectSubmit.mockReturnValueOnce(staging.promise)
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Inspect group matches' }))
  await screen.findByLabelText('PDF choice for roof-south')
  fireEvent.click(screen.getByRole('button', { name: 'Accept tracking labels' }))
  await waitFor(() => expect(props.onProjectSubmit).toHaveBeenCalledTimes(1))
  const select = screen.getByLabelText('PDF choice for roof-south')
  expect(select.disabled).toBe(true)
  fireEvent.change(select, { target: { value: '1' } })
  expect(select.value).toBe('')
  await act(async () => { staging.resolve({ ok: true }); await Promise.resolve() })
  await waitFor(() => expect(screen.getByLabelText('PDF choice for roof-south').disabled).toBe(false))
})

it('S02 review shows matched, unmatched and ambiguous groups from verified report bytes without accepting', async () => {
  const save = downloads(), { props } = setup({ reportBytes: matchReport() })
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Inspect group matches' }))
  const table = await screen.findByRole('table', { name: 'SolarEdge group matches' })
  expect(table.textContent).toContain('roof-eastMatchedPDF grid 0')
  expect(table.textContent).toContain('roof-westUnmatchedNo PDF grid matches')
  expect(table.textContent).toContain('roof-southAmbiguousPDF grid 1, PDF grid 2')
  expect(props.onProjectSubmit).not.toHaveBeenCalled()
  expect(save.createObjectURL).not.toHaveBeenCalled()
})

it.each(['digest', 'identity', 'malformed'])('S02 review refuses %s failures without showing associations', async (failure) => {
  downloads()
  const bytes = failure === 'identity' ? matchReport({ source_version_id: NEXT })
    : failure === 'malformed' ? matchReport({ matches: [{ frame_ref: 'roof-east', candidates: null }] }) : matchReport()
  const { props } = setup({ reportBytes: bytes,
    ...(failure === 'digest' ? { downloadResponse: () => new Response(new Uint8Array(bytes.length).fill(32)) } : {}) })
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Inspect group matches' }))
  await screen.findByText('Group matches could not be verified. Try inspecting the report again.')
  expect(screen.queryByRole('table', { name: 'SolarEdge group matches' })).toBeNull()
  expect(props.onProjectSubmit).not.toHaveBeenCalled()
})

it('S02 review clears old associations when a replacement report starts', async () => {
  downloads()
  const { fetchImpl } = setup({ reportBytes: matchReport() })
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Inspect group matches' }))
  await screen.findByRole('table', { name: 'SolarEdge group matches' })
  const delayed = deferred()
  fetchImpl.mockImplementationOnce(() => delayed.promise)
  fireEvent.click(screen.getByRole('button', { name: 'Build report' }))
  expect(screen.queryByRole('table', { name: 'SolarEdge group matches' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Inspect group matches' })).toBeNull()
  await act(async () => { delayed.resolve(reply(edge(binding, true))); await Promise.resolve() })
  await screen.findByRole('button', { name: 'Inspect group matches' })
})

it('S02 review aborts an in-flight inspection when the selected version changes', async () => {
  downloads()
  const delayed = deferred()
  const { props, client, view, fetchImpl } = setup({ reportBytes: matchReport(), downloadResponse: () => delayed.promise })
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Inspect group matches' }))
  await waitFor(() => expect(fetchImpl.mock.calls.some(([url]) => url.includes('/solar-artifacts/'))).toBe(true))
  const signal = fetchImpl.mock.calls.at(-1)[1].signal
  const next = { ...binding, input_version_id: NEXT }
  act(() => { client.setScope(next, 'session-1'); view.rerender(<ProjectSolarEdgePanel {...props} solarBinding={next} />) })
  expect(signal.aborted).toBe(true)
  await act(async () => { delayed.resolve(new Response(matchReport())); await Promise.resolve() })
  expect(screen.queryByRole('table', { name: 'SolarEdge group matches' })).toBeNull()
  expect(props.onProjectSubmit).not.toHaveBeenCalled()
})

it('S02export downloads complete canonical report bytes and provenance through the actual project transport', async () => {
  const save = downloads(), { fetchImpl } = setup({ reportBytes })
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Download report' }))
  await screen.findByText('Report downloaded.')
  const calls = fetchImpl.mock.calls.filter(([url]) => url.includes('/solar-artifacts/'))
  expect(calls).toHaveLength(1)
  expect(calls[0][0]).toBe(`/api/projects/${P}/drawings/${D}/solar-artifacts/${reportId}?current=false`)
  const savedBytes = await new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(new Uint8Array(reader.result))
    reader.onerror = reject
    reader.readAsArrayBuffer(save.createObjectURL.mock.calls[0][0])
  })
  expect(Array.from(savedBytes)).toEqual(Array.from(reportBytes))
  expect(save.click.mock.instances[0].download).toBe('solaredge-report.json')
  expect(save.revokeObjectURL).toHaveBeenCalledWith('blob:verified-report')
  expect(document.querySelector('a[download]')).toBeNull()
})

it.each(['length', 'digest', 'transport'])('S02export saves no report after %s failure and retains the review', async (failure) => {
  const save = downloads()
  const wrong = new Uint8Array(reportBytes)
  wrong[0] ^= 1
  setup({ reportBytes, downloadResponse: () => failure === 'transport' ? Promise.reject(new Error('offline'))
    : new Response(failure === 'length' ? wrong.slice(1) : wrong) })
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Download report' }))
  await screen.findByText(failure === 'transport' ? 'Report download stopped. Reopen the current report and try again.'
    : 'Report download failed. The file did not match its saved report.')
  expect(save.createObjectURL).not.toHaveBeenCalled()
  expect(save.click).not.toHaveBeenCalled()
  expect(screen.getByRole('region', { name: 'Report review' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Accept tracking labels' }).disabled).toBe(false)
})

it.each(['project_id', 'drawing_id', 'input_version_id', 'authority'])('S02export never saves a pending report after %s changes', async (field) => {
  const save = downloads(), held = deferred()
  const { client } = setup({ reportBytes, downloadResponse: () => held.promise })
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Download report' }))
  act(() => client.setScope(field === 'authority' ? binding : { ...binding, [field]: NEXT },
    field === 'authority' ? 'session-2' : 'session-1'))
  await act(async () => { held.resolve(new Response(reportBytes)); await Promise.resolve() })
  expect(save.createObjectURL).not.toHaveBeenCalled()
  expect(save.click).not.toHaveBeenCalled()
  expect(screen.queryByText('Report downloaded.')).toBeNull()
})

it('S02export rechecks scope after asynchronous digest verification before creating a download', async () => {
  const save = downloads(), held = deferred()
  vi.stubGlobal('crypto', { subtle: { digest: vi.fn(() => held.promise) } })
  const { client } = setup({ reportBytes })
  await upload(); await report()
  fireEvent.click(screen.getByRole('button', { name: 'Download report' }))
  await waitFor(() => expect(globalThis.crypto.subtle.digest).toHaveBeenCalledOnce())
  act(() => client.setScope(binding, 'session-2'))
  await act(async () => held.resolve(createHash('sha256').update(reportBytes).digest().buffer))
  expect(save.createObjectURL).not.toHaveBeenCalled()
  expect(save.click).not.toHaveBeenCalled()
})
