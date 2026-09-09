import { act, cleanup, fireEvent, render as renderCollapsed, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CampaignPanel from './CampaignPanel.jsx'
import useCampaigns from './useCampaigns.js'
import { uploadProjectInput } from './api.js'

vi.mock('./useCampaigns.js', () => ({ default: vi.fn() }))
vi.mock('./api.js', () => ({ uploadProjectInput: vi.fn() }))

const P = '11111111-1111-1111-1111-111111111111'
const C = '33333333-3333-3333-3333-333333333333'
const Q = '55555555-5555-5555-5555-555555555555'
const row = { campaign_id: C, title: 'Release documents', prompt: 'Organize recipes\nKeep the original text.', status: 'accepted', dispatch: { available: false, action: 'mount-fleet-adapter' } }
let campaign

beforeEach(() => {
  uploadProjectInput.mockReset()
  campaign = {
    status: 'ready', refreshing: false, error: null, errorAction: null,
    execution: null, executionLoading: false, executionError: null,
    campaigns: [row], selectedId: C, selected: row, questions: [], answers: {}, pending: {},
    submit: vi.fn().mockResolvedValue({ campaign: row }),
    ask: vi.fn().mockResolvedValue({ question: { question_id: Q } }),
    answer: vi.fn().mockResolvedValue({ answer: { answer: 'Use PDF.' } }),
    select: vi.fn(), refetch: vi.fn().mockResolvedValue({ campaigns: [row] }),
    createRelease: vi.fn().mockResolvedValue({ ok: true }),
    transitionRelease: vi.fn().mockResolvedValue({ ok: true }),
    retryReleaseStage: vi.fn().mockResolvedValue({ ok: true }),
    downloadReleaseArtifact: vi.fn(),
  }
  useCampaigns.mockImplementation(() => campaign)
})
afterEach(cleanup)

const panel = props => <CampaignPanel projectId={P} projectName="Document studio" signedIn {...props} />

// Existing control/security cases exercise the controls after explicit expansion.
function render(ui) {
  const view = renderCollapsed(ui)
  const expand = () => view.container.querySelectorAll('details:not([open]) > summary').forEach(summary => fireEvent.click(summary))
  expand()
  return { ...view, rerender(next) { view.rerender(next); expand() } }
}

describe('finish request navigation', () => {
  function freshProject() {
    campaign.campaigns = []
    campaign.selected = null
    campaign.selectedId = null
  }
  function navigate() {
    fireEvent.click(screen.getByRole('button', { name: 'Finish this project' }))
  }
  it.each(['fresh', 'release'])('opens finish fields without starting work for a %s project', kind => {
    if (kind === 'fresh') freshProject()
    else campaign.completion = { release: { release_id: Q, status: 'active', contract_version: 1, contract: {} } }
    const { container } = renderCollapsed(panel())
    const header = container.querySelector('.campaign-results-header')
    expect(within(header).getByRole('heading', { name: 'Project results' })).toBeTruthy()
    expect(within(header).getByRole('button', { name: 'Finish this project' })).toBeTruthy()
    if (kind === 'release') expect(screen.getByText('Start a new request').closest('details').open).toBe(false)
    navigate()
    if (kind === 'release') expect(screen.getByText('Start a new request').closest('details').open).toBe(true)
    expect(screen.getByLabelText('Campaign goal').value).toBe('finish')
    expect(document.activeElement).toBe(screen.getByLabelText('Title'))
    expect(screen.getByLabelText('Delivery profile')).toBeTruthy()
    expect(screen.getByLabelText('Input file (optional)')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Request release' })).toBeTruthy()
    expect(campaign.submit).not.toHaveBeenCalled()
    expect(campaign.createRelease).not.toHaveBeenCalled()
    expect(campaign.transitionRelease).not.toHaveBeenCalled()
    expect(uploadProjectInput).not.toHaveBeenCalled()
  })
  it('preserves the same draft and selected input through repeated navigation and uses its ready reference', async () => {
    freshProject()
    const path = `inputs/${'a'.repeat(64)}/records.json`
    uploadProjectInput.mockResolvedValue({ path, name: 'records.json' })
    renderCollapsed(panel())
    const title = screen.getByLabelText('Title')
    const prompt = screen.getByLabelText('Prompt')
    fireEvent.change(title, { target: { value: 'Records export' } })
    fireEvent.change(prompt, { target: { value: 'Download CSV' } })
    navigate()
    const input = screen.getByLabelText('Input file (optional)')
    const file = new File(['[]'], 'records.json', { type: 'application/json' })
    fireEvent.change(input, { target: { files: [file] } })
    navigate()
    navigate()
    expect(screen.getByLabelText('Title')).toBe(title)
    expect(title.value).toBe('Records export')
    expect(prompt.value).toBe('Download CSV')
    expect(screen.getByLabelText('Input file (optional)')).toBe(input)
    expect(input.files[0]).toBe(file)
    expect(uploadProjectInput).not.toHaveBeenCalled()
    expect(campaign.submit).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Add to project' }))
    await screen.findByText('records.json added to this project. Ready for this release.')
    navigate()
    expect(screen.getByRole('button', { name: 'Add to project' }).disabled).toBe(true)
    expect(campaign.submit).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Request release' }))
    await waitFor(() => expect(campaign.submit).toHaveBeenCalledExactlyOnceWith({ title: 'Records export', prompt: 'Download CSV', mode: 'finish',
      finish: { delivery_profile: 'web_tool', intended_user: 'Project owner', workflow: 'Download CSV', artifact_refs: [path] } }))
    expect(uploadProjectInput).toHaveBeenCalledExactlyOnceWith(P, file)
  })
  it('keeps ordinary campaign creation available after finish navigation', async () => {
    freshProject()
    renderCollapsed(panel())
    navigate()
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'New campaign' } })
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Build documents' } })
    fireEvent.change(screen.getByLabelText('Campaign goal'), { target: { value: 'ordinary' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit campaign' }))
    await waitFor(() => expect(campaign.submit).toHaveBeenCalledExactlyOnceWith({ title: 'New campaign', prompt: 'Build documents' }))
    expect(campaign.createRelease).not.toHaveBeenCalled()
  })
  it('isolates finish drafts and ready inputs when the project changes', async () => {
    freshProject()
    uploadProjectInput.mockResolvedValue({ path: 'inputs/old/records.json', name: 'records.json' })
    const { rerender } = renderCollapsed(panel())
    navigate()
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Old title' } })
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Old prompt' } })
    fireEvent.change(screen.getByLabelText('Delivery profile'), { target: { value: 'cad_file' } })
    fireEvent.change(screen.getByLabelText('Input file (optional)'), { target: { files: [new File(['[]'], 'records.json')] } })
    fireEvent.click(screen.getByRole('button', { name: 'Add to project' }))
    await screen.findByText('records.json added to this project. Ready for this release.')
    rerender(panel({ projectId: Q }))
    expect(screen.getByLabelText('Campaign goal').value).toBe('ordinary')
    navigate()
    expect(screen.getByLabelText('Title').value).toBe('')
    expect(screen.getByLabelText('Prompt').value).toBe('')
    expect(screen.getByLabelText('Delivery profile').value).toBe('web_tool')
    expect(screen.getByLabelText('Input file (optional)').files).toHaveLength(0)
    expect(screen.queryByText(/added to this project/)).toBeNull()
    expect(campaign.submit).not.toHaveBeenCalled()
  })
  it.each(['upload', 'submit'])('disables navigation and duplicate submission during %s', async operation => {
    freshProject()
    let resolve
    const pending = new Promise(done => { resolve = done })
    if (operation === 'upload') uploadProjectInput.mockReturnValue(pending)
    else campaign.submit.mockReturnValue(pending)
    renderCollapsed(panel())
    navigate()
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Export' } })
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Download CSV' } })
    const form = screen.getByLabelText('Title').closest('form')
    if (operation === 'upload') {
      fireEvent.change(screen.getByLabelText('Input file (optional)'), { target: { files: [new File(['[]'], 'records.json')] } })
      fireEvent.click(screen.getByRole('button', { name: 'Add to project' }))
    } else fireEvent.submit(form)
    expect(screen.getByRole('button', { name: 'Finish this project' }).disabled).toBe(true)
    navigate()
    fireEvent.submit(form)
    expect(campaign.submit).toHaveBeenCalledTimes(operation === 'submit' ? 1 : 0)
    await act(async () => { resolve(operation === 'upload' ? { path: 'inputs/records.json', name: 'records.json' } : { campaign: row }) })
    expect(screen.getByRole('button', { name: 'Finish this project' }).disabled).toBe(false)
  })
})

describe('stalled release revision', () => {
  beforeEach(() => {
    campaign.completion = { release: { release_id: Q, status: 'needs_approach', contract_version: 1,
      contract: { workflow: 'Old workflow', original_goal: 'Original ambition', required_checks: [], deferred_items: ['Later scope'] } },
      next_action: { reason: 'Change the approach' }, decisions: [] }
    campaign.reviseRelease = vi.fn().mockResolvedValue({ ok: true })
  })
  it('shows the prefilled form visibly and requires both values before recording', async () => {
    renderCollapsed(panel())
    const workflow = screen.getByLabelText('Revised workflow')
    expect(workflow.value).toBe('Old workflow')
    expect(workflow.closest('details')).toBeNull()
    expect(screen.getByText('The original goal and prior evidence are retained. Required checks are updated for the revised workflow.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Record revised approach' }))
    await screen.findByRole('alert')
    expect(campaign.reviseRelease).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Reason for changing approach'), { target: { value: 'Reuse publication' } })
    fireEvent.change(workflow, { target: { value: ' ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record revised approach' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Workflow/))
    expect(campaign.reviseRelease).not.toHaveBeenCalled()
    fireEvent.change(workflow, { target: { value: 'Use the published tool' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record revised approach' }))
    await screen.findByText('Revised approach recorded. Review it before continuing.')
    expect(campaign.reviseRelease).toHaveBeenCalledExactlyOnceWith({ workflow: 'Use the published tool', reason: 'Reuse publication' })
    expect(campaign.transitionRelease).not.toHaveBeenCalled()
  })
  it('requires an explicit continuation after showing the saved workflow', async () => {
    campaign.completion.release = { ...campaign.completion.release, status: 'active', contract_version: 2,
      contract: { ...campaign.completion.release.contract, workflow: 'Use the published tool' } }
    renderCollapsed(panel())
    expect(screen.queryByLabelText('Revised workflow')).toBeNull()
    expect(screen.getByText('Use the published tool')).toBeTruthy()
    expect(campaign.transitionRelease).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Continue release' }))
    await screen.findByText('Release continuation requested.')
    expect(campaign.transitionRelease).toHaveBeenCalledExactlyOnceWith('advance')
  })
  it.each(['waiting', 'paused', 'finished', 'cancelled'])('hides revision for %s', status => {
    campaign.completion.release.status = status
    renderCollapsed(panel())
    expect(screen.queryByLabelText('Revised workflow')).toBeNull()
    expect(campaign.reviseRelease).not.toHaveBeenCalled()
  })
  it.each(['active', 'queued', 'waiting', 'paused'])('offers deliberate revision for %s without acting on disclosure navigation', status => {
    campaign.completion.release.status = status
    renderCollapsed(panel())
    const toggle = screen.getByRole('button', { name: 'Revise release' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    const content = document.getElementById(toggle.getAttribute('aria-controls'))
    expect(content.hidden).toBe(true)
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(content.hidden).toBe(false)
    expect(within(content).getByRole('heading', { name: 'Revise release' })).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Revised workflow'), { target: { value: 'Keep my workflow draft' } })
    fireEvent.change(screen.getByLabelText('Reason for changing approach'), { target: { value: 'Keep my reason draft' } })
    fireEvent.click(toggle)
    expect(screen.queryByLabelText('Revised workflow')).toBeNull()
    fireEvent.click(toggle)
    expect(screen.getByLabelText('Revised workflow').value).toBe('Keep my workflow draft')
    expect(screen.getByLabelText('Reason for changing approach').value).toBe('Keep my reason draft')
    for (const method of ['reviseRelease', 'retryReleaseStage', 'transitionRelease', 'createRelease', 'submit', 'refetch']) {
      expect(campaign[method]).not.toHaveBeenCalled()
    }
  })
  it.each(['waiting', 'paused'])('records one revision from %s and uses the paused hook result until explicit resume', async status => {
    campaign.completion.release.status = status
    campaign.completion.stages = [{ stage: 'implementation', contract_version: 1, status: 'failed' }]
    let resolve
    campaign.reviseRelease.mockImplementation(() => new Promise(done => { resolve = done }))
    const { rerender } = renderCollapsed(panel())
    fireEvent.click(screen.getByRole('button', { name: 'Revise release' }))
    fireEvent.change(screen.getByLabelText('Revised workflow'), { target: { value: 'Use the published tool' } })
    fireEvent.change(screen.getByLabelText('Reason for changing approach'), { target: { value: 'Reuse publication' } })
    const submit = screen.getByRole('button', { name: 'Record revised approach' })
    fireEvent.click(submit)
    fireEvent.submit(submit.closest('form'))
    expect(campaign.reviseRelease).toHaveBeenCalledExactlyOnceWith({ workflow: 'Use the published tool', reason: 'Reuse publication' })
    for (const name of ['Revise release', 'Record revised approach', 'Resume release', 'Cancel release']) {
      expect(screen.getByRole('button', { name }).disabled).toBe(true)
    }
    expect(screen.getByLabelText('Revised workflow').disabled).toBe(true)
    expect(screen.getByLabelText('Reason for changing approach').disabled).toBe(true)
    await act(async () => {
      campaign.completion.release = { ...campaign.completion.release, status: 'paused', contract_version: 2,
        contract: { ...campaign.completion.release.contract, workflow: 'Use the published tool' } }
      resolve({ release: campaign.completion.release })
    })
    rerender(panel())
    expect(screen.getByText('Release paused')).toBeTruthy()
    expect(screen.getByText('Use the published tool')).toBeTruthy()
    expect(screen.queryByLabelText('Revised workflow')).toBeNull()
    expect(campaign.retryReleaseStage).not.toHaveBeenCalled()
    expect(campaign.transitionRelease).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Resume release' }))
    await screen.findByText('Release resumed.')
    expect(campaign.transitionRelease).toHaveBeenCalledExactlyOnceWith('resume')
  })
  it.each(['finished', 'cancelled'])('has no revision entry for %s releases', status => {
    campaign.completion.release.status = status
    renderCollapsed(panel())
    expect(screen.queryByRole('button', { name: 'Revise release' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Record revised approach' })).toBeNull()
  })
  it.each(['waiting', 'needs_approach'])('blocks revision submission while a %s release mutation is pending', status => {
    campaign.completion.release.status = status
    const { rerender } = renderCollapsed(panel())
    if (status === 'waiting') fireEvent.click(screen.getByRole('button', { name: 'Revise release' }))
    else expect(screen.queryByRole('button', { name: 'Revise release' })).toBeNull()
    fireEvent.change(screen.getByLabelText('Reason for changing approach'), { target: { value: 'Reuse publication' } })
    campaign.pending.release = true
    rerender(panel())
    const submit = screen.getByRole('button', { name: 'Record revised approach' })
    expect(submit.disabled).toBe(true)
    expect(screen.getByLabelText('Revised workflow').disabled).toBe(true)
    expect(screen.getByLabelText('Reason for changing approach').disabled).toBe(true)
    fireEvent.submit(submit.closest('form'))
    if (status === 'waiting') {
      const toggle = screen.getByRole('button', { name: 'Revise release' })
      expect(toggle.disabled).toBe(true)
      fireEvent.click(toggle)
      expect(toggle.getAttribute('aria-expanded')).toBe('true')
    }
    expect(campaign.reviseRelease).not.toHaveBeenCalled()
  })
  it('blocks opening and submitting revision while a local release transition is in flight', async () => {
    campaign.completion.release.status = 'waiting'
    let resolve
    campaign.transitionRelease.mockReturnValue(new Promise(done => { resolve = done }))
    renderCollapsed(panel())
    fireEvent.click(screen.getByRole('button', { name: 'Revise release' }))
    fireEvent.change(screen.getByLabelText('Reason for changing approach'), { target: { value: 'Reuse publication' } })
    fireEvent.click(screen.getByRole('button', { name: 'Pause release' }))
    expect(screen.getByRole('button', { name: 'Revise release' }).disabled).toBe(true)
    const submit = screen.getByRole('button', { name: 'Record revised approach' })
    expect(submit.disabled).toBe(true)
    fireEvent.submit(submit.closest('form'))
    expect(campaign.reviseRelease).not.toHaveBeenCalled()
    await act(async () => { resolve({ ok: true }) })
    expect(submit.disabled).toBe(false)
  })
  it.each(['project', 'release', 'contract'])('resets revision drafts when the %s changes', kind => {
    campaign.completion.release.status = 'waiting'
    const { rerender } = renderCollapsed(panel())
    fireEvent.click(screen.getByRole('button', { name: 'Revise release' }))
    fireEvent.change(screen.getByLabelText('Revised workflow'), { target: { value: 'Old draft' } })
    fireEvent.change(screen.getByLabelText('Reason for changing approach'), { target: { value: 'Old reason' } })
    fireEvent.click(screen.getByRole('button', { name: 'Revise release' }))
    campaign.completion.release = { ...campaign.completion.release,
      ...(kind === 'release' ? { release_id: C } : kind === 'contract' ? { contract_version: 2 } : {}),
      contract: { ...campaign.completion.release.contract, workflow: 'Current saved workflow' } }
    rerender(panel(kind === 'project' ? { projectId: Q } : {}))
    expect(screen.getByRole('button', { name: 'Revise release' }).getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: 'Revise release' }))
    expect(screen.getByLabelText('Revised workflow').value).toBe('Current saved workflow')
    expect(screen.getByLabelText('Reason for changing approach').value).toBe('')
    expect(campaign.reviseRelease).not.toHaveBeenCalled()
  })
})

describe('results-first hierarchy', () => {
  it('keeps requests and technical history collapsed without hiding an unanswered decision', () => {
    campaign.questions = [{ question_id: Q, prompt: 'Which format?', status: 'open' }]
    const { container } = renderCollapsed(panel())
    expect(screen.getByRole('heading', { name: 'Project results' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Record answer' }).closest('details')).toBeNull()
    expect(screen.getByText('Which format?').closest('details')).toBeNull()
    for (const name of ['Start a new request', 'Technical execution', 'Enrollment and capability invocation', 'Answered question history']) {
      const disclosure = screen.getByText(name).closest('details')
      expect(disclosure.open).toBe(false)
      fireEvent.click(within(disclosure).getByText(name))
      expect(disclosure.open).toBe(true)
    }
    expect(container.querySelector('.campaign-completion').compareDocumentPosition(screen.getByLabelText('Title').closest('form')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText(/Output unavailable. Define a bounded release/)).toBeTruthy()
  })

  it('makes an empty project request visible immediately', () => {
    campaign.campaigns = []
    campaign.selected = null
    renderCollapsed(panel())
    expect(screen.getByRole('button', { name: 'Submit campaign' }).closest('details')).toBeNull()
  })
})

describe('finish input controls', () => {
  const path = `inputs/${'a'.repeat(64)}/records.json`
  const file = () => new File(['[{"name":"Alice"}]'], 'records.json', { type: 'application/json' })
  function finishForm() {
    fireEvent.change(screen.getByLabelText('Campaign goal'), { target: { value: 'finish' } })
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Records export' } })
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Download CSV' } })
    return screen.getByLabelText('Title').closest('form')
  }
  it('requires an explicit upload, prevents duplicate clicks, then sends the acknowledged reference', async () => {
    let resolve
    uploadProjectInput.mockReturnValue(new Promise(done => { resolve = done }))
    render(panel())
    expect(screen.queryByLabelText('Input file (optional)')).toBeNull()
    const form = finishForm()
    const selected = file()
    fireEvent.change(screen.getByLabelText('Input file (optional)'), { target: { files: [selected] } })
    expect(uploadProjectInput).not.toHaveBeenCalled()
    expect(within(form).getByRole('button', { name: 'Request release' }).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Add to project' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add to project' }))
    expect(uploadProjectInput).toHaveBeenCalledExactlyOnceWith(P, selected)
    expect(screen.getByText('Adding input to project…')).toBeTruthy()
    expect(screen.queryByText(/added to this project/)).toBeNull()
    resolve({ path, name: selected.name })
    await screen.findByText('records.json added to this project. Ready for this release.')
    fireEvent.submit(form)
    await waitFor(() => expect(campaign.submit).toHaveBeenCalledWith({ title: 'Records export', prompt: 'Download CSV', mode: 'finish',
      finish: { delivery_profile: 'web_tool', intended_user: 'Project owner', workflow: 'Download CSV', artifact_refs: [path] } }))
  })
  it('keeps failed input unready, blocks direct submission and permits an explicit retry', async () => {
    uploadProjectInput.mockRejectedValueOnce(new Error('Upload failed')).mockResolvedValueOnce({ path, name: 'records.json' })
    render(panel())
    const form = finishForm()
    const selected = file()
    fireEvent.change(screen.getByLabelText('Input file (optional)'), { target: { files: [selected] } })
    fireEvent.click(screen.getByRole('button', { name: 'Add to project' }))
    await screen.findByText('Upload failed')
    expect(screen.queryByText(/added to this project/)).toBeNull()
    fireEvent.submit(form)
    await waitFor(() => expect(within(form).getAllByRole('alert')).toHaveLength(2))
    expect(campaign.submit).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Add to project' }))
    await screen.findByText('records.json added to this project. Ready for this release.')
    expect(uploadProjectInput.mock.calls).toEqual([[P, selected], [P, selected]])
  })
  it.each(['resolve', 'reject'])('clears project input and ignores an old upload %s after switching projects', async outcome => {
    let resolve, reject
    uploadProjectInput.mockReturnValue(new Promise((yes, no) => { resolve = yes; reject = no }))
    const { rerender } = render(panel())
    finishForm()
    fireEvent.change(screen.getByLabelText('Input file (optional)'), { target: { files: [file()] } })
    fireEvent.click(screen.getByRole('button', { name: 'Add to project' }))
    rerender(panel({ projectId: Q }))
    const form = finishForm()
    if (outcome === 'resolve') resolve({ path, name: 'records.json' })
    else reject(new Error('Old upload failed'))
    fireEvent.submit(form)
    await waitFor(() => expect(campaign.submit).toHaveBeenCalledWith(expect.objectContaining({ finish: expect.objectContaining({ artifact_refs: [] }) })))
    expect(screen.getByLabelText('Input file (optional)').files).toHaveLength(0)
    expect(screen.queryByText(/added to this project|Old upload failed/)).toBeNull()
  })
  it('clears prior readiness when selecting a replacement and leaves ordinary submission intact', async () => {
    uploadProjectInput.mockResolvedValue({ path, name: 'records.json' })
    render(panel())
    const form = finishForm()
    fireEvent.change(screen.getByLabelText('Input file (optional)'), { target: { files: [file()] } })
    fireEvent.click(screen.getByRole('button', { name: 'Add to project' }))
    await screen.findByText('records.json added to this project. Ready for this release.')
    fireEvent.change(screen.getByLabelText('Input file (optional)'), { target: { files: [new File(['0'], 'drawing.dxf')] } })
    expect(screen.queryByText(/added to this project/)).toBeNull()
    expect(within(form).getByRole('button', { name: 'Request release' }).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Campaign goal'), { target: { value: 'ordinary' } })
    fireEvent.submit(form)
    await waitFor(() => expect(campaign.submit).toHaveBeenCalledWith({ title: 'Records export', prompt: 'Download CSV' }))
  })
})

describe('release evidence panel', () => {
  it('passes the project authority provider and labels only authoring continuation', async () => {
    const authorityProvider = vi.fn()
    releaseFixture('waiting')
    campaign.completion.next_action = { wait_kind: 'authority', reason: 'Authoring requires an active project conversation', recommended_action: 'Continue from the project conversation to author the missing tool' }
    const { rerender } = render(panel({ authorityProvider }))
    expect(useCampaigns).toHaveBeenCalledWith(P, { enabled: true, authorityProvider })
    fireEvent.click(screen.getByRole('button', { name: 'Continue authoring' }))
    await waitFor(() => expect(campaign.transitionRelease).toHaveBeenCalledWith('resume'))
    campaign.completion.next_action = { wait_kind: 'authority', reason: 'Execution disabled', recommended_action: 'Resolve the workspace policy' }
    rerender(panel({ authorityProvider }))
    expect(screen.getByRole('button', { name: 'Resume release' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Continue authoring' })).toBeNull()
    expect(screen.getByText(/Resolve the workspace policy/)).toBeTruthy()
  })
  it('offers a deadline only for finish mode and sends it as a declarative field', async () => {
    render(panel())
    expect(screen.queryByLabelText('Release deadline (optional)')).toBeNull()
    fireEvent.change(screen.getByLabelText('Campaign goal'), { target: { value: 'finish' } })
    fireEvent.change(screen.getByLabelText('Release deadline (optional)'), { target: { value: '2026-09-08T09:30' } })
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Export' } })
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Download CSV' } })
    fireEvent.click(within(screen.getByLabelText('Title').closest('form')).getByRole('button', { name: 'Request release' }))
    await waitFor(() => expect(campaign.submit).toHaveBeenCalledWith(expect.objectContaining({ finish: expect.objectContaining({ deadline_at: '2026-09-08T09:30' }) })))
  })
  const stages = ['implementation', 'publication', 'deployment', 'user_verification', 'delivery']
  function releaseFixture(status = 'active') {
    campaign.completion = {
      release: { release_id: Q, status, contract_version: 1, scope_summary: 'Deliver the recipe PDF', deferred_items: ['Mobile app'],
        contract: { original_goal: 'Organize all family recipes', required_checks: [{ check_id: 'workflow', stage: 'user_verification', description: 'Download a readable PDF' }] } },
      stages: [], coverage: [], decisions: [{ payload: { reason: 'PDF first, mobile later' } }], remaining: [], deliverables: [],
      next_action: { message: 'Choose the page size in Questions' },
    }
  }
  it('submits finish mode and can start a release for an existing campaign', async () => {
    render(panel())
    fireEvent.click(screen.getByRole('button', { name: 'Finish this project' }))
    await screen.findByText('Release requested.')
    expect(campaign.createRelease).toHaveBeenCalledWith({ delivery_profile: 'web_tool', intended_user: 'Project owner', workflow: row.prompt, artifact_refs: [] })
    fireEvent.change(screen.getByLabelText('Campaign goal'), { target: { value: 'finish' } })
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Family recipes' } })
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Deliver the PDF' } })
    const form = screen.getByLabelText('Title').closest('form')
    fireEvent.change(within(form).getByLabelText('Delivery profile'), { target: { value: 'cad_file' } })
    fireEvent.submit(form)
    await waitFor(() => expect(campaign.submit).toHaveBeenCalledWith({ title: 'Family recipes', prompt: 'Deliver the PDF', mode: 'finish',
      finish: { delivery_profile: 'cad_file', intended_user: 'Project owner', workflow: 'Deliver the PDF', artifact_refs: [] } }))
  })
  it('renders missing stages and missing check status as unavailable', () => {
    releaseFixture()
    campaign.completion.coverage = [{ check_id: 'workflow' }]
    const { container } = render(panel())
    expect(container.querySelectorAll('.campaign-release-stages li')).toHaveLength(5)
    expect([...container.querySelectorAll('.campaign-release-stages li')].every(item => item.textContent.includes('unavailable'))).toBe(true)
    expect(screen.getByText('Download a readable PDF: unavailable')).toBeTruthy()
    expect(screen.getByText('Verified checks unavailable.')).toBeTruthy()
    expect(screen.queryByRole('link')).toBeNull()
    expect(container.textContent).not.toContain('%')
  })
  it('shows completed bounded release, safe validated outputs, replay and original ambition', () => {
    releaseFixture('finished')
    campaign.completion.stages = stages.map(stage => ({ stage, status: 'passed', contract_version: 1,
      evidence: stage === 'delivery' ? { replay_recipe: ['Open the project', 'Download the recipe PDF'], known_limits: ['Desktop only'] } : {} }))
    campaign.completion.coverage = [{ check_id: 'workflow', status: 'passed' }]
    campaign.completion.deliverables = [
      { artifact_ref: 'recipe-pdf', name: 'Recipe PDF', access_path: '/outputs/recipe.pdf', byte_count: 2048,
        sha256: 'a'.repeat(64), valid: true, retrieved: true },
      { name: 'Unsafe', access_path: 'javascript:alert(1)', byte_count: 100, sha256: 'a'.repeat(64), valid: true, retrieved: true },
      { name: 'Unverified', access_path: 'https://example.test/file', byte_count: 100, sha256: 'a'.repeat(64), valid: false, retrieved: true },
      { name: 'Empty', access_path: '/empty', byte_count: 0, sha256: 'a'.repeat(64), valid: true, retrieved: true },
    ]
    render(panel())
    expect(screen.getByText('Completed release')).toBeTruthy()
    expect(screen.getByText(/does not mean the entire original ambition is done/)).toBeTruthy()
    expect(screen.getByText('Organize all family recipes')).toBeTruthy()
    expect(screen.getByText('Mobile app')).toBeTruthy()
    expect(screen.getByText('PDF first, mobile later')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Download Recipe PDF' })).toBeTruthy()
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Download Unsafe' })).toBeNull()
    expect(screen.getByText('(2 KB)')).toBeTruthy()
    expect(screen.getByText('Download the recipe PDF')).toBeTruthy()
    expect(screen.getByText('Desktop only')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Cancel release' })).toBeNull()
  })
  it.each([
    { valid: undefined }, { valid: false }, { retrieved: undefined }, { retrieved: false },
    { valid: undefined, retrieved: undefined }, { sha256: undefined }, { sha256: 'invalid' },
    { byte_count: undefined }, { byte_count: -1 },
  ])('withholds canonical output links without positive proof: %j', overrides => {
    releaseFixture('finished')
    campaign.completion.stages = [{ stage: 'delivery', status: 'passed' }]
    campaign.completion.deliverables = [{ artifact_ref: 'cad-file', name: 'CAD drawing', access_path: '/outputs/drawing.dwg',
      byte_count: 2048, sha256: 'a'.repeat(64), valid: true, retrieved: true, ...overrides }]
    render(panel())
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByText('CAD drawing: access evidence unavailable')).toBeTruthy()
  })
  it('requires explicit proof for accepted artifact aliases', () => {
    releaseFixture('finished')
    campaign.completion.stages = stages.map(stage => ({ stage, status: 'passed' }))
    campaign.completion.coverage = [{ check_id: 'workflow', status: 'passed' }]
    const artifact = { name: 'Alias output', download_url: 'https://example.test/output.pdf', size_bytes: 2048,
      sha256: 'a'.repeat(64), validated: true, retrieval_validated: true, content_validated: true }
    campaign.completion.deliverables = [artifact]
    const { rerender } = render(panel())
    expect(screen.getByRole('link', { name: 'Alias output' }).getAttribute('href')).toBe(artifact.download_url)
    for (const flag of ['validated', 'retrieval_validated', 'content_validated']) {
      for (const value of [undefined, false]) {
        campaign.completion.deliverables = [{ ...artifact, [flag]: value }]
        rerender(panel())
        expect(screen.queryByRole('link')).toBeNull()
      }
    }
  })
  const toolHtml = '\ufeff<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'"></head><body><h1>Café records</h1><button>Convert</button><script>document.title = "Records converter"</script></body></html>'
  const toolBytes = () => new TextEncoder().encode(toolHtml).buffer
  function readyOutput(name = 'records-to-csv.html', mediaType = 'text/html') {
    releaseFixture('finished')
    campaign.completion.stages = stages.map(stage => ({ stage, status: 'passed' }))
    campaign.completion.coverage = [{ check_id: 'workflow', status: 'passed' }]
    const bytes = mediaType === 'text/html' ? toolBytes() : new Uint8Array([1, 2, 3, 4]).buffer
    const artifact = { name, media_type: mediaType, byte_count: bytes.byteLength, sha256: 'a'.repeat(64), valid: true, retrieved: true }
    campaign.completion.deliverables = [artifact]
    campaign.downloadReleaseArtifact.mockResolvedValue({ name, mediaType, bytes })
    return artifact
  }
  it('places verified output controls before scope, release details and the collapsed request', () => {
    readyOutput()
    campaign.campaigns = [row, { ...row, campaign_id: 'other', title: 'Another release' }]
    renderCollapsed(panel())
    const output = screen.getByRole('button', { name: 'Open tool records-to-csv.html' })
    expect(output.closest('details')).toBeNull()
    expect(output.classList.contains('primary')).toBe(true)
    const picker = screen.getByRole('navigation', { name: 'Project releases and campaigns' })
    expect(output.compareDocumentPosition(picker) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(picker.compareDocumentPosition(screen.getByText('Start a new request')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByText('Accepted, not running')).toBeNull()
    expect(screen.queryByText('Release being delivered')).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Outputs' })).toBeNull()
    fireEvent.click(within(picker).getByRole('button', { name: 'Another release' }))
    expect(campaign.select).toHaveBeenCalledExactlyOnceWith('other')
    for (const label of ['Release details', 'Start a new request']) {
      const details = screen.getByText(label).closest('details')
      expect(details.open).toBe(false)
      expect(output.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
    expect(output.compareDocumentPosition(screen.getByText('Deliver the recipe PDF')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
  it('omits empty action notices while keeping real release decisions and errors visible', () => {
    readyOutput()
    campaign.completion.next_action = null
    const { rerender } = renderCollapsed(panel())
    expect(screen.queryByText('What requires you')).toBeNull()
    expect(screen.queryByText('No user action reported.')).toBeNull()
    expect(screen.queryByText('No other pending action reported.')).toBeNull()
    campaign.completion.next_action = { message: 'Choose the page size in Questions' }
    campaign.completion.remaining = ['Confirm the paper size']
    campaign.questions = [{ question_id: Q, prompt: 'Which format?', status: 'open' }]
    campaign.executionError = new Error('Readback unavailable')
    rerender(panel())
    for (const text of ['Choose the page size in Questions', 'Confirm the paper size', 'Which format?', 'Readback unavailable']) {
      expect(screen.getByText(text).closest('details')).toBeNull()
    }
    expect(screen.getByRole('button', { name: 'Record answer' }).closest('details')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Open tool records-to-csv.html' })).toBeNull()
  })
  it('renders exact UTF-8 HTML with srcdoc and removes it on close, replacement and unmount', async () => {
    const artifact = readyOutput()
    const urlApi = { createObjectURL: vi.fn(), revokeObjectURL: vi.fn() }
    const { unmount } = render(panel({ artifactUrlApi: urlApi }))
    fireEvent.click(screen.getByRole('button', { name: 'Open tool records-to-csv.html' }))
    const frame = await screen.findByTitle('Release tool: records-to-csv.html')
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts allow-downloads')
    expect(frame.getAttribute('srcdoc')).toBe(toolHtml)
    expect(frame.hasAttribute('src')).toBe(false)
    expect(campaign.downloadReleaseArtifact).toHaveBeenCalledWith(artifact)
    let resolveReplacement
    campaign.downloadReleaseArtifact.mockReturnValueOnce(new Promise(done => { resolveReplacement = done }))
    fireEvent.click(screen.getByRole('button', { name: 'Open tool records-to-csv.html' }))
    expect(screen.queryByTitle('Release tool: records-to-csv.html')).toBeNull()
    resolveReplacement({ name: artifact.name, mediaType: 'text/html', bytes: toolBytes() })
    const replacement = await screen.findByTitle('Release tool: records-to-csv.html')
    expect(replacement).not.toBe(frame)
    expect(replacement.getAttribute('srcdoc')).toBe(toolHtml)
    expect(replacement.hasAttribute('src')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Close tool' }))
    expect(screen.queryByTitle('Release tool: records-to-csv.html')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Open tool records-to-csv.html' }))
    await screen.findByTitle('Release tool: records-to-csv.html')
    unmount()
    expect(screen.queryByTitle('Release tool: records-to-csv.html')).toBeNull()
    expect(urlApi.createObjectURL).not.toHaveBeenCalled()
    expect(urlApi.revokeObjectURL).not.toHaveBeenCalled()
  })
  it('refuses malformed UTF-8 and clears a previous preview before reporting the decode failure', async () => {
    readyOutput()
    const urlApi = { createObjectURL: vi.fn(), revokeObjectURL: vi.fn() }
    render(panel({ artifactUrlApi: urlApi }))
    fireEvent.click(screen.getByRole('button', { name: 'Open tool records-to-csv.html' }))
    await screen.findByTitle('Release tool: records-to-csv.html')
    campaign.downloadReleaseArtifact.mockResolvedValueOnce({ name: 'records-to-csv.html', mediaType: 'text/html',
      bytes: new Uint8Array([0xc3, 0x28]).buffer })
    fireEvent.click(screen.getByRole('button', { name: 'Open tool records-to-csv.html' }))
    await screen.findByText('The verified tool could not be opened because its HTML is not valid UTF-8.')
    expect(screen.queryByTitle('Release tool: records-to-csv.html')).toBeNull()
    expect(urlApi.createObjectURL).not.toHaveBeenCalled()
    expect(urlApi.revokeObjectURL).not.toHaveBeenCalled()
  })
  it.each(['load', 'execution'])('removes the open preview when current %s readback fails while retaining history', async source => {
    readyOutput()
    const urlApi = { createObjectURL: vi.fn(), revokeObjectURL: vi.fn() }
    const { rerender } = render(panel({ artifactUrlApi: urlApi }))
    fireEvent.click(screen.getByRole('button', { name: 'Open tool records-to-csv.html' }))
    await screen.findByTitle('Release tool: records-to-csv.html')
    if (source === 'load') Object.assign(campaign, { error: new Error('Readback unavailable'), errorAction: 'load' })
    else campaign.executionError = new Error('Readback unavailable')
    rerender(panel({ artifactUrlApi: urlApi }))
    expect(screen.queryByTitle('Release tool: records-to-csv.html')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Open tool records-to-csv.html' })).toBeNull()
    expect(urlApi.createObjectURL).not.toHaveBeenCalled()
    expect(screen.getByText('Previously completed release. Current verification unavailable.')).toBeTruthy()
    expect(screen.getByText('Organize all family recipes')).toBeTruthy()
  })
  it.each([false, true])('keeps the verified download URL for 1000ms after click and unmount (throwing: %s)', async throws => {
    readyOutput('records.csv', 'text/csv')
    const urlApi = { createObjectURL: vi.fn().mockReturnValue('blob:file'), revokeObjectURL: vi.fn() }
    let anchor
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
      anchor = this
      expect(this.isConnected).toBe(true)
      expect(this.download).toBe('records.csv')
      expect(this.getAttribute('href')).toBe('blob:file')
      expect(urlApi.revokeObjectURL).not.toHaveBeenCalled()
      if (throws) throw new Error('Download click failed')
    })
    vi.useFakeTimers()
    try {
      const { unmount } = render(panel({ artifactUrlApi: urlApi }))
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Download records.csv' }))
      })
      expect(clicked).toHaveBeenCalledOnce()
      expect(anchor.isConnected).toBe(false)
      expect(document.querySelector('a[download]')).toBeNull()
      expect(urlApi.revokeObjectURL).not.toHaveBeenCalled()
      if (throws) expect(screen.getByRole('alert').textContent).toContain('Download click failed')
      else expect(screen.queryByRole('alert')).toBeNull()
      expect(urlApi.createObjectURL).toHaveBeenCalledOnce()
      expect(urlApi.createObjectURL.mock.calls[0][0]).toBeInstanceOf(Blob)
      expect(urlApi.createObjectURL.mock.calls[0][0].size).toBe(4)
      unmount()
      expect(urlApi.revokeObjectURL).not.toHaveBeenCalled()
      vi.advanceTimersByTime(999)
      expect(urlApi.revokeObjectURL).not.toHaveBeenCalled()
      vi.advanceTimersByTime(1)
      expect(urlApi.revokeObjectURL).toHaveBeenCalledExactlyOnceWith('blob:file')
      vi.runAllTimers()
      expect(urlApi.revokeObjectURL).toHaveBeenCalledExactlyOnceWith('blob:file')
      vi.useRealTimers()
      const saved = await new Promise((resolve, reject) => {
        const reader = new FileReader()
        reader.onload = () => resolve(reader.result)
        reader.onerror = reject
        reader.readAsArrayBuffer(urlApi.createObjectURL.mock.calls[0][0])
      })
      expect([...new Uint8Array(saved)]).toEqual([1, 2, 3, 4])
    } finally {
      vi.clearAllTimers()
      vi.useRealTimers()
      clicked.mockRestore()
    }
  })
  it('drops a completed retrieval after the project changes', async () => {
    readyOutput()
    let resolve
    campaign.downloadReleaseArtifact.mockReturnValue(new Promise(done => { resolve = done }))
    const urlApi = { createObjectURL: vi.fn(), revokeObjectURL: vi.fn() }
    const { rerender } = render(panel({ artifactUrlApi: urlApi }))
    fireEvent.click(screen.getByRole('button', { name: 'Open tool records-to-csv.html' }))
    rerender(panel({ projectId: Q, artifactUrlApi: urlApi }))
    resolve({ name: 'records-to-csv.html', mediaType: 'text/html', bytes: toolBytes() })
    await waitFor(() => expect(screen.queryByTitle('Release tool: records-to-csv.html')).toBeNull())
    expect(urlApi.createObjectURL).not.toHaveBeenCalled()
  })
  it('removes an open srcdoc preview when the project changes', async () => {
    readyOutput()
    const { rerender } = render(panel())
    fireEvent.click(screen.getByRole('button', { name: 'Open tool records-to-csv.html' }))
    await screen.findByTitle('Release tool: records-to-csv.html')
    rerender(panel({ projectId: Q }))
    expect(screen.queryByTitle('Release tool: records-to-csv.html')).toBeNull()
  })
  it.each(['failed', 'unavailable'])('retains history but withholds outputs after current verification is %s', status => {
    readyOutput()
    campaign.completion.current_verification = { status, reason: 'The current file could not be verified.' }
    render(panel())
    expect(screen.getByText(`Previously completed release. Current verification ${status}.`)).toBeTruthy()
    expect(screen.getByText('The current file could not be verified.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Open tool|Download records/ })).toBeNull()
  })
  it.each(['required_checks', 'coverage'])('never calls contradictory completion usable without %s', missing => {
    readyOutput()
    if (missing === 'required_checks') campaign.completion.release.contract.required_checks = []
    else campaign.completion.coverage = []
    render(panel())
    expect(screen.queryByText('Completed release')).toBeNull()
    expect(screen.queryByRole('button', { name: /Open tool/ })).toBeNull()
  })
  it('does not replace empty coverage with a successful stage claim', () => {
    readyOutput()
    campaign.completion.coverage = []
    campaign.completion.stages.find(row => row.stage === 'user_verification').evidence = { checks: [{ check_id: 'workflow', status: 'passed' }] }
    render(panel())
    expect(screen.queryByText('Completed release')).toBeNull()
    expect(screen.queryByRole('button', { name: /Open tool/ })).toBeNull()
  })
  it.each([
    [{ action: 'retry_stage', stage: 'user_verification' }, 'Retry user verification using the release controls.'],
    [{ action: 'change_approach' }, 'Choose a different approach before retrying this release.'],
    [{ action: 'unknown' }, 'Review the release and resolve the pending action before continuing.'],
  ])('shows a readable structured next action: %j', (nextAction, message) => {
    releaseFixture()
    campaign.completion.next_action = nextAction
    render(panel())
    expect(screen.getByText(message)).toBeTruthy()
    expect(screen.queryByText('No user action reported.')).toBeNull()
  })
  it('offers pause, resume, cancel and failed-stage retry, retaining progress on errors', async () => {
    releaseFixture()
    campaign.completion.stages = [{ stage: 'implementation', status: 'passed' }, { stage: 'publication', status: 'failed' }]
    campaign.retryReleaseStage.mockRejectedValue(new Error('Retry unavailable'))
    const { rerender } = render(panel())
    fireEvent.click(screen.getByRole('button', { name: 'Retry publication' }))
    await screen.findByText('Retry unavailable')
    expect(campaign.retryReleaseStage).toHaveBeenCalledWith('publication')
    expect(screen.getByText('passed')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Pause release' }))
    await waitFor(() => expect(campaign.transitionRelease).toHaveBeenCalledWith('pause'))
    campaign.completion.release.status = 'paused'
    rerender(panel())
    fireEvent.click(screen.getByRole('button', { name: 'Resume release' }))
    await waitFor(() => expect(campaign.transitionRelease).toHaveBeenCalledWith('resume'))
    await screen.findByText('Release resumed.')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel release' }))
    await waitFor(() => expect(campaign.transitionRelease).toHaveBeenCalledWith('cancel'))
  })
})

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
    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Finish this project', 'Release documents', 'Ask', 'Submit campaign'])
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
    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Finish this project', 'Release documents', 'Try again', 'Record answer', 'Ask', 'Submit campaign'])
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
    expect(screen.getByRole('button', { name: 'Release documents' }).getAttribute('aria-pressed')).toBe('true')
    const select = screen.getByRole('button', { name: 'Another campaign' })
    expect(select.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(select)
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
