import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CampaignPanel from './CampaignPanel.jsx'
import useCampaigns from './useCampaigns.js'

vi.mock('./useCampaigns.js', () => ({ default: vi.fn() }))

const P = '11111111-1111-1111-1111-111111111111'
const C = '33333333-3333-3333-3333-333333333333'
const Q = '55555555-5555-5555-5555-555555555555'
const row = { campaign_id: C, title: 'Release documents', prompt: 'Organize recipes\nKeep the original text.', status: 'accepted', dispatch: { available: false, action: 'mount-fleet-adapter' } }
let campaign

beforeEach(() => {
  campaign = {
    status: 'ready', refreshing: false, error: null, errorAction: null,
    execution: null, executionLoading: false, executionError: null,
    campaigns: [row], selectedId: C, selected: row, questions: [], answers: {}, pending: {},
    submit: vi.fn().mockResolvedValue({ campaign: row }),
    ask: vi.fn().mockResolvedValue({ question: { question_id: Q } }),
    answer: vi.fn().mockResolvedValue({ answer: { answer: 'Use PDF.' } }),
    select: vi.fn(), refetch: vi.fn().mockResolvedValue({ campaigns: [row] }),
  }
  useCampaigns.mockImplementation(() => campaign)
})
afterEach(cleanup)

const panel = props => <CampaignPanel projectId={P} projectName="Document studio" signedIn {...props} />

it('lets the user register native AWS release and shows setup required without host execution controls', async () => {
  campaign.allowedMachines = ['VM-C']
  campaign.enrollments = []
  campaign.enroll = vi.fn().mockResolvedValue({ enrollment: { enrollment_id: Q } })
  campaign.enableEnrollment = vi.fn()
  campaign.bindPublication = vi.fn()
  campaign.invokeCapability = vi.fn()
  campaign.capabilities = [{ change_set_id: 'host-publication', label: 'Host tool' }]
  const { rerender } = render(panel())
  expect(screen.getByLabelText('Registration capability').value).toBe('campaign.host-enrollment')
  fireEvent.change(screen.getByLabelText('Registration capability'), { target: { value: 'campaign.native-release' } })
  fireEvent.click(screen.getByRole('button', { name: 'Prepare native AWS release' }))
  await screen.findByText('Native release registration recorded.')
  expect(campaign.enroll).toHaveBeenCalledExactlyOnceWith('VM-C', 'campaign.native-release')
  campaign.enrollments = [{ enrollment_id: Q, machine_id: 'VM-C', state: 'pending',
    capability: 'campaign.native-release', readiness: 'setup_required',
    readiness_message: 'The release executor is not connected.',
    capability_link: { capability: 'campaign.native-release', state: 'pending_link' } }]
  rerender(panel())
  expect(screen.getByText('Native AWS release: Setup required. The release executor is not connected.')).toBeTruthy()
  for (const name of ['Enable', 'Run', 'Use capability', 'Use again', 'Bind published tool', 'Recover submission']) {
    expect(screen.queryByRole('button', { name })).toBeNull()
  }
  expect(screen.queryByText(/Verified uses:/)).toBeNull()
  expect(campaign.enableEnrollment).not.toHaveBeenCalled()
  expect(campaign.bindPublication).not.toHaveBeenCalled()
  expect(campaign.invokeCapability).not.toHaveBeenCalled()
})

it('keeps native release setup required even if a host publication is present on the row', () => {
  campaign.enrollments = [{ enrollment_id: Q, machine_id: 'VM-C', state: 'enabled', completed_uses: 2,
    capability: 'campaign.native-release',
    capability_link: { capability: 'campaign.native-release', state: 'completed', effective_catalog_digest: 'a'.repeat(64) } }]
  campaign.submissions = { [Q]: { idempotencyKey: 'host-key' } }
  render(panel())
  expect(screen.getByText(/Native AWS release: Setup required/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Recover submission' })).toBeNull()
  expect(screen.queryByText(/Capability complete/)).toBeNull()
})

describe('campaign panel in the project workspace', () => {
  const digest = 'a'.repeat(64)
  function capabilityFixture() {
    campaign.allowedMachines = ['Native host']
    campaign.capabilities = [{ change_set_id: 'server-choice', label: 'Campaign host capability' }]
    campaign.enrollments = [{ enrollment_id: Q, machine_id: 'Native host', state: 'enabled',
      completed_uses: 0, capability_link: { state: 'pending_link' }, invocations: [] }]
    campaign.bindPublication = vi.fn().mockResolvedValue({ enrollment: {} })
    campaign.invokeCapability = vi.fn().mockResolvedValue({ invocation: {} })
  }

  it('selects a published label without typed IDs and exposes first then second verified use', async () => {
    capabilityFixture()
    const { rerender } = render(panel())
    const selector = screen.getByLabelText('Published tool for Native host')
    expect([...selector.options].map(option => [option.value, option.text])).toEqual([['server-choice', 'Campaign host capability']])
    expect(screen.queryByLabelText(/change set|source|claim|command/i)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Use capability' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Bind published tool' }))
    await screen.findByText('Published tool bound.')
    expect(campaign.bindPublication).toHaveBeenCalledExactlyOnceWith(Q, 'server-choice')
    campaign.enrollments[0].capability_link = { state: 'published', effective_catalog_digest: digest }
    rerender(panel())
    expect(screen.getByText('Verified uses: 0 of 2. Capability not complete.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Use capability' }))
    await screen.findByText('Submission recorded. Awaiting verified receipt.')
    expect(campaign.invokeCapability).toHaveBeenCalledExactlyOnceWith(Q)
    campaign.enrollments[0].completed_uses = 1
    campaign.enrollments[0].capability_link.state = 'invoked_once'
    rerender(panel())
    fireEvent.click(screen.getByRole('button', { name: 'Use again' }))
    await waitFor(() => expect(campaign.invokeCapability).toHaveBeenCalledTimes(2))
    campaign.enrollments[0].completed_uses = 2
    rerender(panel())
    expect(screen.getByText('Verified uses: 2 of 2. Capability not complete.')).toBeTruthy()
    campaign.enrollments[0].capability_link.state = 'completed'
    rerender(panel())
    expect(screen.getByText('Verified uses: 2 of 2. Capability complete.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Use again' })).toBeNull()
  })

  it('shows actual running, held, failed and missing receipt states without counting them', () => {
    capabilityFixture()
    campaign.enrollments[0].capability_link = { state: 'published', effective_catalog_digest: digest }
    campaign.enrollments[0].invocations = [
      { job_id: 'running-job', status: 'running', progress: 'Waiting for host' },
      { job_id: 'held-job', status: 'failed', progress: 'held', reason: 'Lifecycle handoff required' },
      { job_id: 'missing-job', status: 'complete', receipt_available: false, counted: false },
    ]
    const { rerender } = render(panel())
    expect(screen.getByText('running: Waiting for host')).toBeTruthy()
    expect(screen.getByText('failed: held')).toBeTruthy()
    expect(screen.getByText('Lifecycle handoff required')).toBeTruthy()
    expect(screen.getByText(/Verified receipt missing/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Use capability' }).disabled).toBe(true)
    expect(screen.getByText('Verified uses: 0 of 2. Capability not complete.')).toBeTruthy()
    expect(campaign.invokeCapability).not.toHaveBeenCalled()
    delete campaign.enrollments[0].completed_uses
    rerender(panel())
    expect(screen.getByText('Verified use count unavailable. Capability not complete.')).toBeTruthy()
  })

  it('offers explicit recovery and storage disclosure without submitting on mount or reload', async () => {
    capabilityFixture()
    campaign.submissions = { [Q]: { idempotencyKey: 'pending-key', effectiveCatalogDigest: digest } }
    campaign.recoveryUnavailable = true
    render(panel())
    expect(screen.getByText(/reconnect recovery is unavailable/)).toBeTruthy()
    expect(screen.getByText(/Submission outcome unknown/)).toBeTruthy()
    expect(campaign.invokeCapability).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Reload status' }))
    await screen.findByText('Capability status reloaded.')
    expect(campaign.invokeCapability).not.toHaveBeenCalled()
    const recover = screen.getByRole('button', { name: 'Recover submission' })
    fireEvent.click(recover)
    fireEvent.click(recover)
    await screen.findByText('Submission recovered. Reload status for verified progress.')
    expect(campaign.invokeCapability).toHaveBeenCalledExactlyOnceWith(Q)
  })

  it('shows configured hosts, connects once and keeps capability status pending', async () => {
    campaign.allowedMachines = ['VM-C', 'VM-D']
    campaign.enrollments = [{ enrollment_id: Q, machine_id: 'VM-C', state: 'pending', capability_link: { state: 'pending_link' } }]
    campaign.enroll = vi.fn().mockResolvedValue({ enrollment: {} })
    campaign.enableEnrollment = vi.fn().mockResolvedValue({ enrollment: {} })
    campaign.revokeEnrollment = vi.fn().mockResolvedValue({ enrollment: {} })
    render(panel())
    const select = screen.getByLabelText('Campaign machine')
    expect([...select.options].map(option => option.value)).toEqual(['VM-C', 'VM-D'])
    fireEvent.change(select, { target: { value: 'VM-D' } })
    const connect = screen.getByRole('button', { name: 'Connect VM-D to this campaign' })
    fireEvent.click(connect)
    fireEvent.click(connect)
    await screen.findByText('Host enrollment recorded.')
    expect(campaign.enroll).toHaveBeenCalledExactlyOnceWith('VM-D')
    expect(screen.getByText('Capability not yet published')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Enable' }))
    await screen.findByText('Host enrollment enabled.')
    expect(campaign.enableEnrollment).toHaveBeenCalledExactlyOnceWith(Q)
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    await screen.findByText('Host enrollment revoked.')
    expect(campaign.revokeEnrollment).toHaveBeenCalledExactlyOnceWith(Q)
  })

  it('explains unavailable host configuration without a connect action', () => {
    render(panel())
    expect(screen.getByText(/No campaign machines are configured/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Connect VM-C to this campaign' })).toBeNull()
  })
  it('shows the original prompt and recorded execution without internal metadata or inferred completion', () => {
    const hidden = { spec: 'spec secret', worker: 'worker secret', fence: 'fence secret',
      active_attempt: 'attempt secret', dispatch: 'mount-fleet-adapter', artifact_ref: C }
    campaign.execution = {
      tasks: [{ ...hidden, task_id: C, title: 'Publish recipes', current_stage: 'publish', status: 'reconcile_required',
        depends_on: ['design', 'build'], blocked_by_questions: [Q] }],
      questions: [],
      receipts: [{ ...hidden, receipt_id: Q, task_id: C, stage: 'publish', outcome: 'unknown', verified: true, reconciles_receipt_id: C }],
      events: [{ ...hidden, event_id: C, task_id: C, event_type: 'task_created', created_at: '2026-09-05T12:00:00Z' }],
    }
    const { container } = render(panel())
    expect(container.querySelector('.campaign-prompt').textContent).toBe(row.prompt)
    const execution = screen.getByRole('region', { name: 'Execution' })
    expect(within(execution).getByText('Publish recipes')).toBeTruthy()
    expect(within(execution).getByText('Outcome unknown, reconciliation required')).toBeTruthy()
    expect(within(execution).getByText('Waits for: design, build')).toBeTruthy()
    expect(within(execution).getByText('Blocked by an open question')).toBeTruthy()
    expect(execution.textContent).toContain('publish: unknown (verified) (reconciliation)')
    expect(execution.textContent).toContain('task created')
    expect(execution.querySelector('time').textContent).toBe(new Date('2026-09-05T12:00:00Z').toLocaleString())
    expect(execution.textContent).not.toMatch(/spec|worker|fence|attempt|mount-fleet-adapter|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|%|complete/i)
    expect(within(execution).queryAllByRole('button')).toHaveLength(0)
    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Submit campaign', 'Ask'])
  })

  it('shows loading and empty execution, and retains questions during an execution error', () => {
    campaign.executionLoading = true
    const { rerender } = render(panel())
    expect(screen.getByText('Loading execution…')).toBeTruthy()
    expect(screen.queryByText('No tasks recorded yet.')).toBeNull()
    campaign.executionLoading = false
    campaign.execution = { tasks: [], questions: [], receipts: [], events: [] }
    campaign.executionError = new Error('Execution is unavailable.')
    campaign.questions = [{ question_id: Q, prompt: 'Which format?', status: 'open' }]
    rerender(panel())
    expect(screen.getByText('No tasks recorded yet.')).toBeTruthy()
    expect(screen.getByText('Which format?')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('Execution is unavailable.')
    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Submit campaign', 'Try again', 'Record answer', 'Ask'])
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(campaign.refetch).toHaveBeenCalledTimes(1)
  })

  it.each([['claimed', 'In progress'], ['pending', 'Waiting']])('uses plain words for task status %s', (status, words) => {
    campaign.execution = { tasks: [{ task_id: C, title: 'Build recipes', current_stage: 'build', status }], questions: [], receipts: [], events: [] }
    render(panel())
    expect(within(screen.getByRole('region', { name: 'Execution' })).getByText(words)).toBeTruthy()
  })

  it('shows sign-in guidance without mounting a hook or form when signed out', () => {
    const { container, rerender } = render(panel({ signedIn: false }))
    expect(screen.getByRole('status').textContent).toBe('Sign in to submit a campaign.')
    expect(container.querySelector('form')).toBeNull()
    expect(useCampaigns).not.toHaveBeenCalled()
    rerender(panel())
    expect(screen.getByLabelText('Title')).toBeTruthy()
    expect(screen.getByLabelText('Prompt')).toBeTruthy()
  })

  it('renders nothing when disabled or without a project', () => {
    const { container, rerender } = render(panel({ enabled: false }))
    expect(container.textContent).toBe('')
    rerender(panel({ projectId: null }))
    expect(container.textContent).toBe('')
  })

  it('separates accepted status from unavailable fleet without exposing adapter identifiers', () => {
    const { container, rerender } = render(panel())
    const status = container.querySelector('[data-state="accepted"]')
    const lines = within(status).getAllByRole('status')
    expect(lines.map(line => line.textContent)).toEqual(['Accepted, not running', 'The build fleet is not connected yet.'])
    expect(container.textContent).not.toMatch(/mount-fleet-adapter|%|budget/i)
    campaign = { ...campaign, selected: { ...row, status: 'running', dispatch: { available: true } } }
    rerender(panel())
    expect(screen.getByText('Running')).toBeTruthy()
    expect(screen.getByText('Build fleet available')).toBeTruthy()
    expect(screen.queryByText('Dispatched')).toBeNull()
    expect(screen.queryByText('The build fleet is not connected yet.')).toBeNull()
    expect(container.textContent).not.toMatch(/%|budget/i)
  })

  it.each(['succeeded', 'failed', 'cancelled'])('uses the server terminal state %s', state => {
    campaign.selected = { ...row, status: state }
    const { container } = render(panel())
    expect(container.querySelector(`[data-state="${state}"]`)).toBeTruthy()
    expect(screen.getByText(state[0].toUpperCase() + state.slice(1))).toBeTruthy()
    expect(container.textContent).not.toMatch(/%|budget/i)
  })

  it('rejects an oversized title beside its field and focuses the alert', async () => {
    render(panel())
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'x'.repeat(201) } })
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Build documents' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit campaign' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/Title must contain 1 to 200 characters/)
    expect(document.activeElement).toBe(alert)
    expect(campaign.submit).not.toHaveBeenCalled()
  })

  it('submits a valid draft, updates the remaining count and reports the outcome', async () => {
    render(panel())
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'New campaign' } })
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Build' } })
    expect(screen.getByText('32763 characters remaining').getAttribute('aria-live')).toBe('polite')
    fireEvent.click(screen.getByRole('button', { name: 'Submit campaign' }))
    await screen.findByText('Campaign recorded.')
    expect(campaign.submit).toHaveBeenCalledWith({ title: 'New campaign', prompt: 'Build' })
  })

  it('keeps the answer draft on conflict and offers exactly one reload', async () => {
    campaign.questions = [{ question_id: Q, prompt: 'Which format?', status: 'open' }]
    campaign.answer.mockRejectedValue(Object.assign(new Error('Conflict'), { status: 409, code: 'answer_conflict' }))
    render(panel())
    fireEvent.change(screen.getByLabelText('Answer'), { target: { value: 'My draft answer' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record answer' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('This question already has a different recorded answer. Reload to see it.')
    expect(screen.getByLabelText('Answer').value).toBe('My draft answer')
    expect(screen.getAllByRole('button', { name: 'Reload' })).toHaveLength(1)
    expect(campaign.refetch).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }))
    expect(campaign.refetch).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(alert)
  })

  it('shows persisted answers and generates no user-facing question-key field', async () => {
    campaign.questions = [{ question_id: Q, prompt: 'Which format?', status: 'answered' }]
    campaign.answers = { [Q]: { answer: 'Use PDF.' } }
    const { container } = render(panel())
    expect(container.querySelector('[data-state="answered"]').textContent).toContain('Use PDF.')
    expect(screen.queryByLabelText('Answer')).toBeNull()
    expect(screen.queryByLabelText(/key/i)).toBeNull()
    fireEvent.change(screen.getByLabelText('Follow-up question'), { target: { value: 'Which layout?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    await screen.findByText('Question recorded.')
    expect(campaign.ask).toHaveBeenCalledWith({ prompt: 'Which layout?' })
  })

  it('disables pending mutations and retains rows during a failed refresh', () => {
    campaign.pending = { submit: true, ask: true, [`answer:${Q}`]: true }
    campaign.questions = [{ question_id: Q, prompt: 'Which format?', status: 'open' }]
    campaign.error = new Error('Campaigns are unavailable right now.')
    campaign.errorAction = 'load'
    render(panel())
    for (const name of ['Submit campaign', 'Ask', 'Record answer']) {
      const button = screen.getByRole('button', { name })
      expect(button.disabled).toBe(true)
      expect(button.getAttribute('aria-busy')).toBe('true')
    }
    expect(screen.getByText('Which format?')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('Campaigns are unavailable right now.')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(campaign.refetch).toHaveBeenCalledTimes(1)
  })

  it('shows a cold-load skeleton and selects another campaign', () => {
    campaign.status = 'loading'
    const { container, rerender } = render(panel())
    expect(container.querySelector('.skeleton-stack')).toBeTruthy()
    campaign.status = 'ready'
    campaign.campaigns = [row, { ...row, campaign_id: 'other', title: 'Another campaign' }]
    rerender(panel())
    const select = screen.getByRole('combobox', { name: 'Active campaign' })
    expect(select.value).toBe(C)
    fireEvent.change(select, { target: { value: 'other' } })
    expect(campaign.select).toHaveBeenCalledWith('other')
  })

  it('drops late action failures and drafts after project change', async () => {
    let reject
    campaign.submit.mockReturnValue(new Promise((_resolve, no) => { reject = no }))
    const { rerender } = render(panel())
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Old title' } })
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Old prompt' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit campaign' }))
    rerender(panel({ projectId: '22222222-2222-2222-2222-222222222222' }))
    reject(new Error('Old project failed'))
    await waitFor(() => expect(screen.getByLabelText('Title').value).toBe(''))
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
