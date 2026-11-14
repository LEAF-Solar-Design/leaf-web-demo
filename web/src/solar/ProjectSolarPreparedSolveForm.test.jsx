import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ProjectSolarPreparedSolveForm } from './ProjectSolarPreparedSolveForm.jsx'
import { SEED_SETTINGS } from './solarSettingsModel.js'

afterEach(cleanup)
const B = { kind: 'project', organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222', drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444' }
const FRAME = 'leaf:frame:00000000-0000-4000-8000-000000000001'
const REQUEST = { grid: { Dwgname: 'Saved roof', Sequences: [2, 1, 1, 0], Rows: [], Modify: [] } }
function fixture() {
  const h = { current: true, revision: 6 }
  const data = (options) => ({ context: { ...B, head_version_id: B.input_version_id, is_head: true, intake_sha256: 'a'.repeat(64) },
    intake: { solar_design_graph_sha256: 'b'.repeat(64), solar_design_graph: {
      rev: 6, settings: { ...SEED_SETTINGS }, project: { name: 'Saved roof', zip_code: '', latitude: null, longitude: null } } },
    solve_preparation: { available: true, graph_revision: h.revision,
      frames: [{ id: FRAME, name: 'West roof', panel_count: 3 }],
      ...(options.solveFrame ? { frame_ref: FRAME, request: REQUEST } : {}) } })
  h.client = { getSnapshot: () => ({ binding: B, authorityKey: 'epoch' }),
    captureCatalogScope: () => () => h.current,
    readContext: vi.fn(async (options) => ({ ok: true, data: data(options) })),
    prepareRun: vi.fn((intent) => intent) }
  h.props = { projectClient: h.client, solarBinding: B, authorityKey: 'epoch', isCurrent: () => h.current,
    catalogDigest: 'sha256:' + 'c'.repeat(64), newProjectIntentKey: vi.fn(() => 'intent-1'),
    onProjectSubmit: vi.fn(async () => ({ ok: true, status: 202, data: { job_id: '55555555-5555-4555-8555-555555555555' } })),
    onClose: vi.fn(), onDraftChange: vi.fn() }
  h.mount = () => render(<ProjectSolarPreparedSolveForm {...h.props} />)
  h.choose = async () => {
    await screen.findByRole('option', { name: 'West roof (3 panels)' })
    fireEvent.change(screen.getByLabelText('Solver connection'), { target: { value: 'verified-connection' } })
    fireEvent.change(screen.getByLabelText('Panel group'), { target: { value: FRAME } })
  }
  return h
}

it('submits only a fresh server-derived frame request without raw request/frame inputs', async () => {
  const h = fixture(); h.mount(); await h.choose()
  expect(screen.queryByLabelText('Request')).toBeNull()
  expect(screen.queryByLabelText('Grant ref')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Run solve' }))
  await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledOnce())
  expect(h.client.readContext.mock.calls[1][0]).toMatchObject({ solveFrame: FRAME })
  expect(h.client.prepareRun.mock.calls[0][0]).toEqual({ tool: 'solar-solve-proposal',
    params: { grant_ref: 'verified-connection', request: REQUEST, drawing_id: B.drawing_id },
    catalogDigest: h.props.catalogDigest, idempotencyKey: 'intent-1',
    solveContext: { frame_ref: FRAME, phase: 'initial', expected_rev: 6 } })
  expect(screen.getByRole('button', { name: 'Run solve' })).toBeDisabled()
})

it('refuses a mismatched fresh preparation revision without any submission', async () => {
  const h = fixture(); h.mount(); await h.choose(); h.revision = 7
  fireEvent.click(screen.getByRole('button', { name: 'Run solve' }))
  await waitFor(() => expect(screen.getByRole('status')).toBeTruthy())
  expect(h.client.prepareRun).not.toHaveBeenCalled()
  expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
})

it('does not resubmit after an unknown transport outcome', async () => {
  const h = fixture(); h.props.onProjectSubmit.mockResolvedValue({ ok: false, status: 502 })
  h.mount(); await h.choose(); const button = screen.getByRole('button', { name: 'Run solve' })
  fireEvent.click(button)
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/outcome is unknown/i))
  fireEvent.click(button)
  expect(h.props.onProjectSubmit).toHaveBeenCalledOnce()
  expect(button).toBeDisabled()
})

it('ignores late choices after the captured scope becomes stale', async () => {
  const h = fixture(); let finish
  h.client.readContext.mockReturnValue(new Promise((resolve) => { finish = resolve }))
  h.mount(); h.current = false
  await act(async () => finish({ ok: true, data: {} }))
  expect(screen.queryByRole('option', { name: /West roof/ })).toBeNull()
  expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
})
