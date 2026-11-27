import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ProjectSolarPreparedSolveForm } from './ProjectSolarPreparedSolveForm.jsx'
import { SEED_SETTINGS } from './solarSettingsModel.js'
import { readProjectSolarCompletion } from './solarProjectCompletion.js'

afterEach(cleanup)
const B = { kind: 'project', organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222', drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444' }
const JOB = '55555555-5555-4555-8555-555555555555'
const FRAME = 'leaf:frame:00000000-0000-4000-8000-000000000001'
const REQUEST = { grid: { Dwgname: 'Saved roof', Sequences: [2, 1, 1, 0], Rows: [], Modify: [] } }
function fixture() {
  const h = { current: true, revision: 6, digest: 'b'.repeat(64),
    frames: [{ id: FRAME, name: 'West roof', panel_count: 3 }] }
  const data = (options) => ({ context: { ...B, head_version_id: B.input_version_id, is_head: true, intake_sha256: 'a'.repeat(64) },
    intake: { solar_design_graph_sha256: 'b'.repeat(64), solar_design_graph: {
      rev: 6, settings: { ...SEED_SETTINGS }, project: { name: 'Saved roof', zip_code: '', latitude: null, longitude: null } } },
    solve_preparation: { available: true, graph_revision: h.revision, graph_sha256: h.digest,
      frames: h.frames, solve_history_available: h.historyAvailable,
      ...(options.solveFrame ? { frame_ref: FRAME, request: REQUEST } : {}) } })
  h.client = { getSnapshot: () => ({ binding: B, authorityKey: 'epoch' }),
    captureCatalogScope: () => () => h.current,
    readContext: vi.fn(async (options) => ({ ok: true, data: data(options) })),
    prepareRun: vi.fn((intent) => intent) }
  h.props = { projectClient: h.client, solarBinding: B, authorityKey: 'epoch', isCurrent: () => h.current,
    catalogDigest: 'sha256:' + 'c'.repeat(64), newProjectIntentKey: vi.fn(() => 'intent-1'),
    onProjectSubmit: vi.fn(async () => ({ ok: true, status: 202, data: { job_id: JOB } })),
    onClose: vi.fn(), onDraftChange: vi.fn() }
  h.mount = () => render(<ProjectSolarPreparedSolveForm {...h.props} />)
  h.choose = async () => {
    await screen.findByRole('option', { name: 'West roof (3 panels)' })
    fireEvent.change(screen.getByLabelText('Solver connection'), { target: { value: 'verified-connection' } })
    fireEvent.change(screen.getByLabelText('Panel group'), { target: { value: FRAME } })
  }
  return h
}

it('shows saved group coverage for complete, partial and untouched groups', async () => {
  const h = fixture()
  h.frames = [
    { ...h.frames[0], committed_panel_count: 3, remaining_panel_count: 0 },
    { id: 'east', name: 'East roof', panel_count: 6, committed_panel_count: 4, remaining_panel_count: 2 },
    { id: 'south', name: 'South roof', panel_count: 9, committed_panel_count: 0, remaining_panel_count: 9 },
    { id: 'north', name: 'North roof', panel_count: 6, committed_panel_count: 4, remaining_panel_count: 4 },
  ]
  const view = h.mount()
  await screen.findByRole('table', { name: 'Saved panel group progress' })
  expect(screen.getByRole('row', { name: 'West roof 3 0' })).toBeInTheDocument()
  expect(screen.getByRole('row', { name: 'East roof 4 2' })).toBeInTheDocument()
  expect(screen.getByRole('row', { name: 'South roof 0 9' })).toBeInTheDocument()
  expect(screen.getByRole('row', { name: 'North roof Unavailable Unavailable' })).toBeInTheDocument()
  expect(screen.getByRole('option', { name: 'South roof (9 panels)' })).toBeInTheDocument()
  h.client.readContext.mockImplementationOnce(() => new Promise(() => {}))
  view.rerender(<ProjectSolarPreparedSolveForm {...h.props} authorityKey="next-epoch" />)
  expect(screen.queryByRole('table', { name: 'Saved panel group progress' })).toBeNull()
})

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

it.each([true, false])('shows the latest version-bound solve independently of saved counts (%s)', async (available) => {
  const h = fixture()
  h.historyAvailable = available
  h.frames = [
    { ...h.frames[0], committed_panel_count: 1, remaining_panel_count: 2,
      latest_solve: { status: 'failed', reason_code: 'CLOUD_REQUEST_REJECTED', message: 'Upstream details' } },
    { id: 'east', name: 'East roof', panel_count: 6, committed_panel_count: 0, remaining_panel_count: 6,
      latest_solve: { status: 'succeeded' } },
    { id: 'south', name: 'South roof', panel_count: 9, committed_panel_count: 0, remaining_panel_count: 9 },
  ]
  h.mount()
  await screen.findByRole('columnheader', { name: 'Latest solve' })
  expect(screen.getByRole('row', { name: `West roof 1 2 ${available ? 'Failed (CLOUD_REQUEST_REJECTED)' : 'Unavailable'}` }))
    .toBeInTheDocument()
  expect(screen.getByRole('row', { name: `East roof 0 6 ${available ? 'Proposal finished' : 'Unavailable'}` }))
    .toBeInTheDocument()
  expect(screen.getByRole('row', { name: `South roof 0 9 ${available ? 'No matching solve' : 'Unavailable'}` }))
    .toBeInTheDocument()
  expect(screen.queryByText('Upstream details')).toBeNull()
})

it('recommends review, diagnosis, continued work and new work in that order', async () => {
  const h = fixture()
  h.historyAvailable = true
  h.frames = [
    { id: 'start', name: 'South roof', panel_count: 30, committed_panel_count: 0, remaining_panel_count: 30 },
    { id: 'partial', name: 'West roof', panel_count: 12, committed_panel_count: 4, remaining_panel_count: 8 },
    { id: 'active', name: 'Garage', panel_count: 8, committed_panel_count: 0, remaining_panel_count: 8,
      latest_solve: { status: 'running' } },
    { id: 'failed', name: 'North roof', panel_count: 5, committed_panel_count: 0, remaining_panel_count: 5,
      latest_solve: { status: 'failed' } },
    { id: 'finished', name: 'East roof', panel_count: 4, committed_panel_count: 0, remaining_panel_count: 4,
      latest_solve: { status: 'succeeded' } },
    { id: 'complete', name: 'Porch', panel_count: 6, committed_panel_count: 6, remaining_panel_count: 0,
      latest_solve: { status: 'succeeded' } },
    { id: 'queued', name: 'Shed', panel_count: 6, committed_panel_count: 0, remaining_panel_count: 6,
      latest_solve: { status: 'queued' } },
  ]
  h.mount()
  const list = await screen.findByRole('list', { name: 'Recommended panel groups' })
  expect(within(list).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
    'East roof. Review the finished proposal.',
    'North roof. Check why the last solve failed.',
    'West roof. Continue the partly covered group.',
    'South roof. Start the group with no saved strings.',
  ])
})

it('sorts each action by remaining coverage and keeps saved order for ties', async () => {
  const h = fixture()
  h.historyAvailable = true
  h.frames = [
    { id: 'small', name: 'Small roof', panel_count: 12, committed_panel_count: 11, remaining_panel_count: 1 },
    { id: 'first', name: 'First roof', panel_count: 7, committed_panel_count: 2, remaining_panel_count: 5 },
    { id: 'second', name: 'Second roof', panel_count: 6, committed_panel_count: 1, remaining_panel_count: 5 },
    { id: 'invalid', name: 'Unknown roof', panel_count: 9, committed_panel_count: 0, remaining_panel_count: 7 },
  ]
  h.mount()
  const list = await screen.findByRole('list', { name: 'Recommended panel groups' })
  expect(within(list).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
    'First roof. Continue the partly covered group.',
    'Second roof. Continue the partly covered group.',
    'Small roof. Continue the partly covered group.',
  ])
  expect(screen.getByRole('row', { name: 'Unknown roof Unavailable Unavailable No matching solve' })).toBeInTheDocument()
  expect(screen.getByText('Some groups have no recommendation because their saved coverage is unavailable.')).toBeInTheDocument()
})

it('keeps saved coverage visible when solve history cannot support recommendations', async () => {
  const h = fixture()
  h.historyAvailable = false
  h.frames = [{ ...h.frames[0], committed_panel_count: 1, remaining_panel_count: 2 }]
  h.mount()
  expect(await screen.findByRole('row', { name: 'West roof 1 2 Unavailable' })).toBeInTheDocument()
  expect(screen.queryByRole('list', { name: 'Recommended panel groups' })).toBeNull()
  expect(screen.getByText('Recommendations are unavailable because solve history is unavailable.')).toBeInTheDocument()
})

it.each([
  [{ status: 'incomplete', completed_pieces: 0, total_pieces: 3, failed_piece_index: 0 },
    'Solve stopped before any pieces finished.'],
  [{ status: 'partial', completed_pieces: 1, total_pieces: 3, failed_piece_index: 1 },
    'Solve stopped after 1 of 3 pieces.'],
  [{ status: 'partial', completed_pieces: 3, total_pieces: 3, phase: 'assembly' },
    'Solve could not assemble a proposal from the 3 completed pieces.'],
  [{ status: 'partial', completed_pieces: 2, total_pieces: 3, failed_piece_index: 0 }, null],
])('renders validated split failure feedback (%j)', async (progress, detail) => {
  const h = fixture()
  const view = h.mount()
  await h.choose()
  const button = screen.getByRole('button', { name: 'Run solve' })
  fireEvent.click(button)
  await waitFor(() => expect(button).toBeDisabled())
  const tool = 'solar-solve-proposal'
  const job = { job_id: JOB, org_id: B.organization_id, project_id: B.project_id,
    input_version_id: B.input_version_id, tool_name: tool, status: 'failed',
    execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: B.drawing_id },
    error: { error_code: 'GRAPH_JOB_FAILED', reason_code: 'CLOUD_UPSTREAM_FAILURE', retryable: false,
      solve_progress: progress, message: 'Raw upstream details' } }
  const outcome = readProjectSolarCompletion({ job, binding: B, jobId: JOB, tool })
  const meta = { tool, authorityKey: 'epoch', binding: B, isCurrent: () => true }
  view.rerender(<ProjectSolarPreparedSolveForm {...h.props} projectOutcome={{ outcome, meta }} />)
  const status = await screen.findByRole('status')
  if (detail) {
    expect(status).toHaveTextContent(detail + ' The drawing keeps the last saved strings.')
  } else {
    expect(status).toHaveTextContent('cloud_upstream_failure')
    expect(status).not.toHaveTextContent('completed pieces')
  }
  expect(status).not.toHaveTextContent('Raw upstream details')
  await waitFor(() => expect(button).toBeEnabled())
  expect(h.props.onProjectSubmit).toHaveBeenCalledOnce()
})

it('shows bound running progress while retaining the solve lock', async () => {
  const h = fixture()
  const view = h.mount()
  await h.choose()
  const button = screen.getByRole('button', { name: 'Run solve' })
  fireEvent.click(button)
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/submitted/i))
  const outcome = { state: 'pending', jobId: JOB, message: 'Solved 2 of 3 pieces.' }
  const meta = { tool: 'solar-solve-proposal', authorityKey: 'epoch', binding: B, isCurrent: () => true }
  view.rerender(<ProjectSolarPreparedSolveForm {...h.props} projectOutcome={{ outcome, meta }} />)
  expect(screen.getByRole('status')).toHaveTextContent('Solved 2 of 3 pieces.')
  expect(button).toBeDisabled()
  expect(h.props.onProjectSubmit).toHaveBeenCalledOnce()
})

it('refuses a mismatched fresh preparation revision without any submission', async () => {
  const h = fixture(); h.mount(); await h.choose(); h.revision = 7
  fireEvent.click(screen.getByRole('button', { name: 'Run solve' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(
    'This saved drawing could not be prepared for Solve. Refresh it and try again.'))
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


it.each([undefined, null, 'f'.repeat(64)])('refuses a missing or mismatched fresh preparation digest (%s)', async (digest) => {
  const h = fixture(); h.mount(); await h.choose(); h.digest = digest
  fireEvent.click(screen.getByRole('button', { name: 'Run solve' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(
    'This saved drawing could not be prepared for Solve. Refresh it and try again.'))
  expect(h.client.prepareRun).not.toHaveBeenCalled()
  expect(h.props.newProjectIntentKey).not.toHaveBeenCalled()
  expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
})
