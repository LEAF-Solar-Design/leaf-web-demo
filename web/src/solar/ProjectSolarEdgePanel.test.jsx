import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
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
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.dispose()) })
function setup({ isHead = true } = {}) {
  let client
  const fetchImpl = vi.fn(async (url) => {
    const selected = client.getSnapshot().binding
    if (url.includes('/context?')) return reply({ context: { ...selected, head_version_id: isHead ? selected.input_version_id : NEXT,
      is_head: isHead, intake_sha256: 'd'.repeat(64) }, intake: { solar_design_graph: { rev: 7 }, solar_design_graph_sha256: 'e'.repeat(64) } })
    if (url.endsWith('/solaredge-pdf')) return reply(edge(selected))
    if (url.endsWith('/report')) return reply(edge(selected, true))
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
