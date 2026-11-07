import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import ProjectSolveReview, { ProjectSolarSchemaForm } from './ProjectSolveReview.jsx'
import { createSolarProjectClient } from './solarProjectClient.js'
import workerUnavailable from './__fixtures__/workerUnavailable.json'
import { projectSolarWorkerRefusal } from './solarProjectCompletion.js'
import { SEED_SETTINGS } from './solarSettingsModel.js'
import { PROJECT_SOLAR_WORKSPACE_LABELS, PROJECT_SOLAR_WORKSPACE_REASONS } from './ProjectSolarReadPanel.jsx'

const B = { kind: 'project', organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222', drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444' }
const J = '55555555-5555-4555-8555-555555555555'
const K = '66666666-6666-4666-8666-666666666666'
const digest = 'sha256:' + 'a'.repeat(64)
const proposal = (count = 2) => ({ state: 'proposal', jobId: J, candidateSha256: 'b'.repeat(64),
  frameId: 'frame-1', phase: 'initial',
  summary: Array.from({ length: count }, (_, i) => ({ panel_count: 12, endpoints: [`p-${i}-first`, `p-${i}-last`] })) })
const context = () => ({ context: { organization_id: B.organization_id, project_id: B.project_id,
  drawing_id: B.drawing_id, input_version_id: B.input_version_id, head_version_id: B.input_version_id,
  is_head: true, intake_sha256: 'c'.repeat(64) }, intake: { solar_design_graph_sha256: 'd'.repeat(64),
  solar_design_graph: { rev: 6, settings: { ...SEED_SETTINGS },
    project: { name: 'Field', zip_code: '', latitude: null, longitude: null } } } })
const http = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data })
const clients = []
afterEach(() => { cleanup(); for (const client of clients.splice(0)) client.dispose() })
function fixture() {
  const h = { current: true, result: http({ job_id: K, status: 'submitted' }, 202) }
  const proof = 'lco1.' + 'e'.repeat(64)
  h.fetch = vi.fn(async (url) => {
    if (url.endsWith('/checkout')) return http({ organization_id: B.organization_id, project_id: B.project_id,
      drawing_id: B.drawing_id, acquired: true, checkout_capability: proof,
      checkout: { holder: 'editor', fence: '1', acquired: '2026-01-01T00:00:00Z', expires: '2099-01-01T00:00:00Z' } })
    if (url.includes('/context?')) return http(context())
    if (url.endsWith('/api/run')) return h.result
    throw new Error('Unexpected request')
  })
  h.client = createSolarProjectClient({ fetchImpl: h.fetch })
  h.client.setScope(B, 'epoch-1')
  clients.push(h.client)
  h.props = { solarBinding: B, authorityKey: 'epoch-1', projectClient: h.client, catalogDigest: digest,
    newProjectIntentKey: vi.fn(() => 'review-intent-1'), onProjectSubmit: vi.fn((...args) => h.client.submitRun(...args)),
    isCurrent: () => h.current, onDiscard: vi.fn(), onSolveAgain: vi.fn(), proposal: proposal(), busy: false }
  h.runs = () => h.fetch.mock.calls.filter(([url]) => url.endsWith('/api/run'))
  return h
}

it.each(['solar-solve-proposal', 'solar-correct-string'])('G20b %s releases a worker-refused schema form', async (name) => {
  const h = fixture()
  h.result = http(workerUnavailable, 503)
  h.props.newProjectIntentKey.mockImplementation(() => 'review-intent-' + (h.props.newProjectIntentKey.mock.calls.length + 1))
  const tool = { name, params: { type: 'object', properties: {} } }
  render(<ProjectSolarSchemaForm {...h.props} tool={tool} />)
  if (name === 'solar-solve-proposal') {
    fireEvent.change(screen.getByLabelText('Frame'), { target: { value: 'frame-1' } })
  }
  const button = screen.getByRole('button', { name: name === 'solar-solve-proposal' ? 'Run solve' : 'Correct strings' })
  fireEvent.click(button)
  const refusal = projectSolarWorkerRefusal(name)
  await waitFor(() => expect(screen.getByRole('status').textContent).toBe(`${refusal.message} (${refusal.reasonCode})`))
  expect(screen.queryByText(PROJECT_SOLAR_WORKSPACE_REASONS.run_outcome_unknown)).toBeNull()
  expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  expect(button.disabled).toBe(false)
  expect(document.querySelector('fieldset').disabled).toBe(false)
  fireEvent.click(button)
  await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledTimes(2))
  expect(h.props.newProjectIntentKey).toHaveBeenCalledTimes(2)
  expect(h.props.onProjectSubmit.mock.calls[1][0]).not.toBe(h.props.onProjectSubmit.mock.calls[0][0])
})

it.each([true, false])('G20b commit worker refusal classification: %s', async (worker) => {
  const h = fixture()
  h.result = http(worker ? workerUnavailable : { error: { reason_code: 'OTHER' } }, 503)
  render(<ProjectSolveReview {...h.props} />)
  fireEvent.click(screen.getByRole('button', { name: 'Commit proposal' }))
  const refusal = projectSolarWorkerRefusal('solar-commit-solve')
  await waitFor(() => expect(screen.getByRole('status').textContent).toBe(worker
    ? `${refusal.message} (${refusal.reasonCode})` : `${PROJECT_SOLAR_WORKSPACE_REASONS.run_outcome_unknown} (OTHER)`))
  if (worker) {
    expect(screen.queryByText(PROJECT_SOLAR_WORKSPACE_REASONS.run_outcome_unknown)).toBeNull()
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
    expect(screen.getByRole('button', { name: PROJECT_SOLAR_WORKSPACE_LABELS.solve_again_action }).disabled).toBe(false)
  }
})

it('W1C5 renders frame, phase, counts and endpoints, bounded at 200 rows', () => {
  const h = fixture()
  const view = render(<ProjectSolveReview {...h.props} proposal={proposal(203)} />)
  expect(screen.getByText('frame-1')).toBeInTheDocument()
  expect(screen.getByText('initial')).toBeInTheDocument()
  expect(screen.getByText('203')).toBeInTheDocument()
  expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(201)
  expect(screen.getByText('p-199-last')).toBeInTheDocument()
  expect(screen.queryByText('p-200-first')).toBeNull()
  expect(screen.getByText('More proposed strings not shown: 3.')).toBeInTheDocument()
  expect(screen.getByText(PROJECT_SOLAR_WORKSPACE_REASONS.proposal_more_strings.replace('{count}', '3'))).toBeInTheDocument()
  expect(screen.getByRole('region', { name: PROJECT_SOLAR_WORKSPACE_LABELS.proposal_review_label })).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: PROJECT_SOLAR_WORKSPACE_LABELS.proposal_review_title })).toBeInTheDocument()
  expect(view.container.textContent).toContain(PROJECT_SOLAR_WORKSPACE_REASONS.proposal_unpublished)
  for (const key of ['frame_label', 'phase_label', 'string_count_label']) {
    expect(screen.getByText(PROJECT_SOLAR_WORKSPACE_LABELS[key])).toBeInTheDocument()
  }
  expect(screen.getByRole('table')).toHaveAccessibleName(PROJECT_SOLAR_WORKSPACE_LABELS.proposed_strings_label)
  expect(screen.getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual(
    ['string_label', 'panels_label', 'start_label', 'end_label'].map((key) => PROJECT_SOLAR_WORKSPACE_LABELS[key]))
  expect(screen.getByRole('button', { name: PROJECT_SOLAR_WORKSPACE_LABELS.commit_proposal_action })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: PROJECT_SOLAR_WORKSPACE_LABELS.discard_action }))
  expect(h.props.onDiscard).toHaveBeenCalledOnce()
  expect(h.runs()).toHaveLength(0)
})

it('W1C5 commits the proposal job with fresh revision, catalog, idempotency and checkout headers', async () => {
  const h = fixture()
  await h.client.acquireCheckout()
  render(<ProjectSolveReview {...h.props} />)
  const commit = screen.getByRole('button', { name: 'Commit proposal' })
  fireEvent.click(commit); fireEvent.click(commit)
  await waitFor(() => expect(h.runs()).toHaveLength(1))
  expect(JSON.parse(h.runs()[0][1].body)).toEqual({ tool: 'solar-commit-solve',
    params: { drawing_id: B.drawing_id, expected_rev: 6 }, dwg: B.input_version_id,
    catalog_digest: digest, proposal_job_id: J })
  expect(h.runs()[0][1].headers).toMatchObject({ 'Idempotency-Key': 'review-intent-1',
    'X-Checkout-Capability': 'lco1.' + 'e'.repeat(64), 'X-Org-Id': B.organization_id, 'X-Project-Id': B.project_id })
  expect(h.props.onProjectSubmit).toHaveBeenCalledOnce()
  expect(h.props.newProjectIntentKey).toHaveBeenCalledOnce()
  await waitFor(() => expect(commit.disabled).toBe(true))
})

it.each(['SOLVE_PROPOSAL_STALE', 'SOLVE_PROPOSAL_NOT_SUCCEEDED'])('W1C5 shows %s and offers solve again', async (reason) => {
  const h = fixture()
  h.result = http({ error: { reason_code: reason, message: 'private server prose' } }, 409)
  render(<ProjectSolveReview {...h.props} />)
  fireEvent.click(screen.getByRole('button', { name: 'Commit proposal' }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain(reason))
  expect(screen.getByRole('status').textContent).toBe(`${PROJECT_SOLAR_WORKSPACE_REASONS.run_refused} (${reason})`)
  expect(screen.queryByText('private server prose')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: PROJECT_SOLAR_WORKSPACE_LABELS.solve_again_action }))
  expect(h.props.onSolveAgain).toHaveBeenCalledOnce()
  expect(screen.getByRole('button', { name: 'Commit proposal' }).disabled).toBe(true)
})

it('W1C5 shows an asynchronous commit refusal associated with the accepted job', async () => {
  const h = fixture()
  const view = render(<ProjectSolveReview {...h.props} />)
  fireEvent.click(screen.getByRole('button', { name: 'Commit proposal' }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('submitted'))
  view.rerender(<ProjectSolveReview {...h.props} projectOutcome={{ outcome: {
    state: 'failed', jobId: K, reasonCode: 'solve_proposal_stale' }, meta: {
    binding: B, authorityKey: 'epoch-1', tool: 'solar-commit-solve', isCurrent: () => true } }} />)
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('solve_proposal_stale'))
  expect(screen.getByRole('button', { name: 'Run solve again' })).toBeInTheDocument()
})

it('W1C5 refuses invalid proposals and stale or busy scopes before sending', async () => {
  const h = fixture()
  const view = render(<ProjectSolveReview {...h.props} proposal={{ ...proposal(), candidateSha256: 'bad' }} />)
  expect(screen.getByRole('status').textContent).toBe(PROJECT_SOLAR_WORKSPACE_REASONS.proposal_unreadable)
  expect(screen.queryByRole('button', { name: 'Commit proposal' })).toBeNull()
  view.rerender(<ProjectSolveReview {...h.props} busy />)
  expect(screen.getByRole('button', { name: 'Commit proposal' }).disabled).toBe(true)
  view.rerender(<ProjectSolveReview {...h.props} />)
  h.current = false
  fireEvent.click(screen.getByRole('button', { name: 'Commit proposal' }))
  await act(async () => { await Promise.resolve() })
  expect(h.runs()).toHaveLength(0)
})

it('W1C5 retires a pending context read on scope change or editor close', async () => {
  for (const close of [false, true]) {
    const h = fixture()
    let release
    const waiting = new Promise(resolve => { release = resolve })
    h.fetch.mockImplementationOnce(() => waiting)
    const view = render(<ProjectSolveReview {...h.props} />)
    fireEvent.click(screen.getByRole('button', { name: 'Commit proposal' }))
    await waitFor(() => expect(h.fetch).toHaveBeenCalledOnce())
    if (close) view.unmount()
    else h.current = false
    await act(async () => { release(http(context())); for (let i = 0; i < 20; i++) await Promise.resolve() })
    expect(h.runs()).toHaveLength(0)
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    view.unmount()
  }
})

it('ProjectSolarSchemaForm starts from a valid draft and reports changes', () => {
  const h = fixture()
  const tool = { name: 'solar-solve-proposal', params: { type: 'object', properties: {
    grant_ref: { type: 'string', default: 'default-grant' }, request: { type: 'object' },
  } } }
  const draft = { params: { grant_ref: 'kept-grant', request: { grid: { name: 'original' } } },
    context: { frame_ref: 'kept-frame', phase: 'background' } }
  const onDraftChange = vi.fn()
  const view = render(<ProjectSolarSchemaForm {...h.props} tool={tool} draft={draft} onDraftChange={onDraftChange} />)
  expect(screen.getByLabelText('Grant ref').value).toBe('kept-grant')
  expect(screen.getByLabelText('Request').value).toBe('{"grid":{"name":"original"}}')
  expect(screen.getByLabelText('Frame').value).toBe('kept-frame')
  expect(screen.getByLabelText('Phase').value).toBe('1')
  expect(onDraftChange).not.toHaveBeenCalled()
  draft.params.grant_ref = 'changed source'
  draft.params.request.grid.name = 'changed source'
  draft.context.frame_ref = 'changed source'
  fireEvent.change(screen.getByLabelText('Frame'), { target: { value: 'new-frame' } })
  expect(onDraftChange).toHaveBeenLastCalledWith({
    params: { grant_ref: 'kept-grant', request: { grid: { name: 'original' } } },
    context: { frame_ref: 'new-frame', phase: 'background' },
  })
  const reported = onDraftChange.mock.calls.at(-1)[0]
  expect(reported.params).not.toBe(draft.params)
  expect(reported.params.request).not.toBe(draft.params.request)
  expect(reported.context).not.toBe(draft.context)
  reported.params.request.grid.name = 'changed report'
  reported.context.frame_ref = 'changed report'
  fireEvent.change(screen.getByLabelText('Grant ref'), { target: { value: 'new-grant' } })
  expect(onDraftChange).toHaveBeenLastCalledWith({
    params: { grant_ref: 'new-grant', request: { grid: { name: 'original' } } },
    context: { frame_ref: 'new-frame', phase: 'background' },
  })
  expect(screen.getByLabelText('Frame').value).toBe('new-frame')
  expect(onDraftChange).toHaveBeenCalledTimes(2)
  view.unmount()

  for (const invalid of [null, [], { params: null, context: {} }, { params: [], context: {} },
    { params: 'invalid', context: {} }, { params: {}, context: null }, { params: {}, context: [] }]) {
    const changes = vi.fn()
    const fallback = render(<ProjectSolarSchemaForm {...h.props} tool={tool} draft={invalid} onDraftChange={changes} />)
    expect(screen.getByLabelText('Grant ref').value).toBe('default-grant')
    expect(screen.getByLabelText('Request').value).toBe('')
    expect(screen.getByLabelText('Frame').value).toBe('')
    expect(screen.getByLabelText('Phase').value).toBe('0')
    expect(changes).not.toHaveBeenCalled()
    fallback.unmount()
  }
})

it('W1C5 invalid membership JSON keeps the generic correction form from submitting', async () => {
  const h = fixture()
  const tool = { name: 'solar-correct-string', params: { type: 'object', properties: {
    memberships: { type: 'array' },
  }, required: ['memberships'] } }
  render(<ProjectSolarSchemaForm {...h.props} tool={tool} />)
  fireEvent.change(screen.getByLabelText('Memberships'), { target: { value: '[broken' } })
  expect(screen.getByRole('button', { name: 'Correct strings' }).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Correct strings' }))
  await act(async () => { await Promise.resolve() })
  expect(h.fetch).not.toHaveBeenCalled()
})

it('W1C5 SchemaForm submits correction memberships with the current graph revision', async () => {
  const h = fixture()
  const tool = { name: 'solar-correct-string', params: { type: 'object', properties: {
    memberships: { type: 'array', items: { type: 'object' } }, expected_rev: { type: 'integer' }, drawing_id: { type: 'string' },
  }, required: ['memberships', 'expected_rev', 'drawing_id'] } }
  render(<ProjectSolarSchemaForm {...h.props} tool={tool} />)
  expect(screen.queryByLabelText('Expected rev')).toBeNull()
  fireEvent.change(screen.getByLabelText('Memberships'), { target: { value: '[{"string_ref":"s1","ordered_panel_refs":["p1","p2"]}]' } })
  fireEvent.click(screen.getByRole('button', { name: 'Correct strings' }))
  await waitFor(() => expect(h.runs()).toHaveLength(1))
  expect(JSON.parse(h.runs()[0][1].body)).toMatchObject({ tool: tool.name,
    params: { drawing_id: B.drawing_id, expected_rev: 6, memberships: [{ string_ref: 's1', ordered_panel_refs: ['p1', 'p2'] }] } })
  expect(JSON.parse(h.runs()[0][1].body)).not.toHaveProperty('solve_context')
  expect(JSON.parse(h.runs()[0][1].body)).not.toHaveProperty('proposal_job_id')
})

it('W1C5 solve form carries solver input and the separate frame/phase/revision binding', async () => {
  const h = fixture()
  const tool = { name: 'solar-solve-proposal', params: { type: 'object', properties: {
    grant_ref: { type: 'string' }, request: { type: 'object' },
  }, required: ['grant_ref', 'request'] } }
  render(<ProjectSolarSchemaForm {...h.props} tool={tool} />)
  expect(screen.getByRole('form', { name: PROJECT_SOLAR_WORKSPACE_LABELS.solve_action })).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: PROJECT_SOLAR_WORKSPACE_LABELS.solve_action })).toBeInTheDocument()
  expect(screen.getByLabelText(PROJECT_SOLAR_WORKSPACE_LABELS.frame_label)).toBeInTheDocument()
  expect(screen.getByLabelText(PROJECT_SOLAR_WORKSPACE_LABELS.phase_label)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: PROJECT_SOLAR_WORKSPACE_LABELS.close_action })).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Grant ref'), { target: { value: 'grant-1' } })
  fireEvent.change(screen.getByLabelText('Request'), { target: { value: '{"grid":{}}' } })
  fireEvent.change(screen.getByLabelText('Frame'), { target: { value: 'frame-1' } })
  fireEvent.change(screen.getByLabelText('Frame'), { target: { value: '' } })
  expect(screen.getByRole('button', { name: 'Run solve' }).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('Frame'), { target: { value: 'frame-1' } })
  fireEvent.click(screen.getByRole('button', { name: 'Run solve' }))
  await waitFor(() => expect(h.runs()).toHaveLength(1))
  expect(JSON.parse(h.runs()[0][1].body)).toEqual({ tool: tool.name, params: {
    drawing_id: B.drawing_id, grant_ref: 'grant-1', request: { grid: {} } }, dwg: B.input_version_id,
  catalog_digest: digest, solve_context: { frame_ref: 'frame-1', expected_rev: 6, phase: 'initial' } })
})

it.each([
  ['pending', 'run_submitted'], ['succeeded', 'run_published'], ['proposal', 'proposal_ready'],
  ['outcome_unknown', 'run_outcome_unknown'], ['failed', 'run_refused'], ['refused', 'run_refused'],
])('W1C5 %s feedback uses the frozen workspace sentence', async (state, key) => {
  const h = fixture()
  const tool = { name: 'solar-correct-string', params: { type: 'object', properties: {} } }
  const view = render(<ProjectSolarSchemaForm {...h.props} tool={tool} />)
  expect(screen.getByRole('form', { name: PROJECT_SOLAR_WORKSPACE_LABELS.correct_strings_action })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: PROJECT_SOLAR_WORKSPACE_LABELS.correct_strings_action }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toBe(PROJECT_SOLAR_WORKSPACE_REASONS.run_submitted))
  view.rerender(<ProjectSolarSchemaForm {...h.props} tool={tool} projectOutcome={{ outcome: {
    state, jobId: K }, meta: { binding: B, authorityKey: 'epoch-1', tool: tool.name, isCurrent: () => true } }} />)
  await waitFor(() => expect(screen.getByRole('status').textContent).toBe(PROJECT_SOLAR_WORKSPACE_REASONS[key]))
})
