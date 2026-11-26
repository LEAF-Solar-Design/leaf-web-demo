import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { getAuthorStage, getAuthorStageSource, postReview, reviewContext } from '../../api.js'
import { postMessage } from '../../converse.js'
import { track } from '../../telemetry.js'
import AuthoringCard from './AuthoringCard.jsx'

vi.mock('../../api.js', () => ({
  getAuthorStage: vi.fn(), getAuthorStageSource: vi.fn(), postReview: vi.fn(), reviewContext: vi.fn(),
}))
vi.mock('../../converse.js', () => ({ postMessage: vi.fn(), resolveApproval: vi.fn() }))
vi.mock('../../telemetry.js', () => ({ track: vi.fn() }))
const receipt = { change_set_id: 'change', staged_commit: 'a'.repeat(40), catalog_digest: 'b'.repeat(64),
  package_files: ['branch-tools/roof/main.csx', 'branch-tools/roof/tool.json'] }
const staged = { change_set_id: 'change', status: 'staged', receipt, compile_attempts: [{ attempt: 2, status: 'passed' }] }
const proposal = { confirmation_id: 'plan', plan: { title: 'Roof labels', summary: 'Label every roof',
  behavior: ['Group by roof layer'], capabilities: ['drawing.write', 'network.read'], change_kind: 'revise', target_tool_name: 'roof' } }
const mount = async (props = {}) => {
  let view
  await act(async () => { view = render(<AuthoringCard sessionId="session" changeSetId="change" proposal={proposal} {...props} />) })
  return view
}
const button = name => screen.getByRole('button', { name })
const advance = async ms => act(async () => { await vi.advanceTimersByTimeAsync(ms) })
beforeEach(() => {
  getAuthorStage.mockResolvedValue(staged)
  getAuthorStageSource.mockResolvedValue({ path: 'branch-tools/roof/main.csx', text: '<img src=x>\nreturn;\n' })
  reviewContext.mockResolvedValue({ viewer_may_approve: true, approval_kind_if_approved: 'self', review: null })
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks(); vi.unstubAllGlobals() })

const openDeployDetails = () => fireEvent.click(screen.getByText('What will be deployed'))

it('shows the staged files, source, compile and plan risks behind a self deploy', async () => {
  postReview.mockResolvedValue({ change_set_id: 'change', decision: 'approve', approval_kind: 'self',
    approver_role: 'owner', decided_at: '2026-11-21T03:14:00Z', status: 'published' })
  await mount()
  openDeployDetails()
  for (const text of ['Label every roof', 'Group by roof layer',
    'Changes your drawing · network.read · Revises the existing tool roof', 'Branch has not reported yet']) {
    expect(screen.getByText(text)).toBeVisible()
  }
  expect(screen.getByText(receipt.package_files.join(', '))).toBeVisible()
  const source = screen.getByText(/main\.csx · 2 lines/)
  fireEvent.click(source)
  expect(source.closest('details')).toHaveAttribute('open')
  expect(screen.getByText(/<img src=x>/)).toBeVisible()
  expect(document.querySelector('img')).toBeNull()
  const approve = button('Deploy to AutoCAD')
  approve.focus()
  expect(approve).toHaveFocus()
  await act(async () => approve.click())
  expect(postReview).toHaveBeenCalledWith({ change_set_id: 'change', staged_commit: receipt.staged_commit,
    catalog_digest: receipt.catalog_digest, decision: 'approve' })
  expect(screen.getByText(/Deployed to AutoCAD/)).toHaveTextContent(/\d+:\d{2}/)
  expect(screen.getByText(/Published to your plugin catalog/)).toBeVisible()
  expect(track).toHaveBeenCalledWith('review.decided', { decision: 'approve', approval_kind: 'self' })
  expect(track).toHaveBeenCalledWith('deploy.clicked')
  expect(screen.queryByRole('button')).toBeNull()
})

it('deploys a self-built tool that passed its checks with one inline button', async () => {
  let finishDeploy
  postReview.mockReturnValue(new Promise(resolve => { finishDeploy = resolve }))
  await mount()
  expect(screen.getAllByRole('button', { name: 'Deploy to AutoCAD' })).toHaveLength(1)
  expect(screen.getByText('Deploy')).toBeVisible()
  expect(screen.queryByText('Review')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Request changes' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(screen.queryByText(/self-approval/)).toBeNull()
  expect(screen.getByText('What will be deployed').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByText('Label every roof')).not.toBeVisible()
  await act(async () => fireEvent.click(button('Deploy to AutoCAD')))
  expect(track).toHaveBeenCalledWith('deploy.clicked')
  expect(button('Deploying...')).toBeDisabled()
  expect(postReview).toHaveBeenCalledTimes(1)
  expect(postReview).toHaveBeenCalledWith({ change_set_id: 'change', staged_commit: receipt.staged_commit,
    catalog_digest: receipt.catalog_digest, decision: 'approve' })
  let finishStage
  getAuthorStage.mockReturnValue(new Promise(resolve => { finishStage = resolve }))
  await act(async () => finishDeploy({ change_set_id: 'change', decision: 'approve', approval_kind: 'self',
    approver_role: 'owner', decided_at: '2026-11-21T03:14:00Z', status: 'approved' }))
  expect(screen.getByText('Deploying to AutoCAD...')).toBeVisible()
  expect(screen.queryByText(/Published to your plugin catalog/)).toBeNull()
  await act(async () => finishStage({ ...staged,
    publication: { state: 'published', published_at: '2026-11-21T03:15:00Z' } }))
  expect(screen.getByText(/Deployed to AutoCAD/)).toHaveTextContent(/\d+:\d{2}/)
  expect(screen.getByText(/Published to your plugin catalog/)).toBeVisible()
  expect(screen.queryByText('Deploying to AutoCAD...')).toBeNull()
})

const selfReview = { decision: 'approve', approval_kind: 'self', approver_role: 'owner',
  decided_at: '2026-11-21T03:14:00Z' }

it('shows the deployed publication and its time after a fresh reload of the server stage shape', async () => {
  const publishedAt = '2026-11-21T03:15:00Z'
  getAuthorStage.mockResolvedValue({ ...staged, status: 'staged', review: selfReview,
    publication: { state: 'published', published_at: publishedAt } })
  await mount()
  for (const label of [/Deployed to AutoCAD/, /Published to your plugin catalog/]) {
    const row = screen.getByText(label)
    expect(row).toHaveTextContent(/\d+:\d{2}/)
    expect(row.querySelector('time')).toHaveAttribute('datetime', new Date(publishedAt).toISOString())
  }
  expect(screen.queryByText('Deploying to AutoCAD...')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Deploy to AutoCAD' })).toBeNull()
  expect(getAuthorStageSource).not.toHaveBeenCalled()
})

it.each([
  ['rolled_back', 'Rolled back', 'Rolled back from your plugin catalog'],
  ['superseded', 'Not deployed', 'Not published: another change was published first'],
  ['denied', 'Not deployed', 'Not published: changes were requested'],
  ['publishing', 'Deploying to AutoCAD...', 'Publishing...'],
  ['awaiting_approval', 'Deploying to AutoCAD...', 'Waiting for approval'],
])('shows the reported %s publication after a reload', async (state, deploy, publish) => {
  getAuthorStage.mockResolvedValue({ ...staged, review: selfReview, publication: { state } })
  await mount()
  expect(screen.getByText(deploy)).toBeVisible()
  expect(screen.getByText(publish)).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Deploy to AutoCAD' })).toBeNull()
  expect(getAuthorStageSource).not.toHaveBeenCalled()
})

it.each(['published', 'denied', 'superseded', 'rolled_back', 'publishing'])(
  'keeps source and approval closed for publication %s even without a reported review', async state => {
    getAuthorStage.mockResolvedValue({ ...staged, publication: { state } })
    await mount()
    expect(getAuthorStageSource).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Deploy to AutoCAD' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })

it.each([
  ['rolled_back', 'Rolled back from your plugin catalog'],
  ['superseded', 'Not published: another change was published first'],
  ['denied', 'Not published: changes were requested'],
  ['awaiting_approval', 'Published to your plugin catalog'],
  ['publishing', 'Published to your plugin catalog'],
  [undefined, 'Published to your plugin catalog'],
])('reconciles a published decision with stage publication %s', async (state, label) => {
  postReview.mockResolvedValue({ change_set_id: 'change', ...selfReview, status: 'published' })
  await mount()
  getAuthorStage.mockResolvedValue({ ...staged, review: selfReview,
    ...(state ? { publication: { state } } : {}) })
  await act(async () => fireEvent.click(button('Deploy to AutoCAD')))
  expect(screen.getByText(new RegExp(label))).toBeVisible()
  if (['rolled_back', 'superseded', 'denied'].includes(state)) {
    expect(screen.queryByText(/Deployed to AutoCAD/)).toBeNull()
    expect(screen.queryByText(/Published to your plugin catalog/)).toBeNull()
  } else expect(screen.getByText(/Deployed to AutoCAD/)).toBeVisible()
})

it.each(['rolled_back', 'superseded', 'denied'])('stops polling for a %s publication without Branch evidence', async state => {
  vi.useFakeTimers()
  getAuthorStage.mockResolvedValue({ ...staged, review: selfReview, publication: { state } })
  await mount()
  await advance(10000)
  expect(getAuthorStage).toHaveBeenCalledTimes(1)
})

it('keeps polling a published stage until Branch reports', async () => {
  vi.useFakeTimers()
  const published = { ...staged, review: selfReview, publication: { state: 'published' } }
  getAuthorStage.mockResolvedValueOnce(published)
    .mockResolvedValue({ ...published, branch: { last_event: 'synced' } })
  await mount()
  await advance(2000)
  expect(screen.getByText('Synced')).toBeVisible()
  await advance(10000)
  expect(getAuthorStage).toHaveBeenCalledTimes(2)
})

it.each(['rolled_back', 'superseded', 'denied', 'publishing'])(
  'keeps the independent Review row unchanged for %s', async state => {
    getAuthorStage.mockResolvedValue({ ...staged, publication: { state },
      review: { ...selfReview, approval_kind: 'independent', approver_role: 'reviewer' } })
    await mount()
    expect(screen.getByText('Review')).toBeVisible()
    expect(screen.getByText(/Approved by reviewer/)).toBeVisible()
    expect(screen.queryByText('Deploy')).toBeNull()
  })

it('offers no deploy for a self-built tool whose compile failed, only the retry', async () => {
  getAuthorStage.mockResolvedValue({ ...staged, status: 'failed',
    error: { reason_code: 'compile_failed', message: 'Unexpected delimiter' },
    compile_attempts: [{ attempt: 1, status: 'refused', diagnostics: [{ code: 'CS1026', file: 'main.csx', line: 50 }] }] })
  await mount()
  expect(screen.getByText('Deploy is available once the checks pass.')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Deploy to AutoCAD' })).toBeNull()
  expect(button('Fix with these errors')).toBeEnabled()
  expect(postReview).not.toHaveBeenCalled()
})

it('keeps the independent review buttons for a reviewer who is not the author', async () => {
  reviewContext.mockResolvedValue({ viewer_may_approve: true, approval_kind_if_approved: 'independent', review: null })
  await mount()
  expect(button('Approve')).toBeEnabled()
  expect(button('Request changes')).toBeEnabled()
  expect(screen.getByText('Review')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Deploy to AutoCAD' })).toBeNull()
  expect(screen.queryByText('What will be deployed')).toBeNull()
})

it('sends a bound change request and shows an unavailable export honestly', async () => {
  reviewContext.mockResolvedValue({ viewer_may_approve: true, approval_kind_if_approved: 'independent' })
  getAuthorStageSource.mockResolvedValue(null)
  postReview.mockResolvedValue({ change_set_id: 'change', decision: 'request_changes',
    approval_kind: 'independent', approver_role: 'reviewer', status: 'denied' })
  await mount()
  expect(screen.getByText('source not available')).toBeVisible()
  expect(button('Approve')).toBeEnabled()
  fireEvent.click(button('Request changes'))
  expect(postReview).not.toHaveBeenCalled()
  expect(button('Send change request')).toBeDisabled()
  const box = screen.getByRole('textbox', { name: /Review comment/ })
  expect(box.maxLength).toBe(500)
  fireEvent.change(box, { target: { value: 'Handle an empty drawing' } })
  await act(async () => fireEvent.click(button('Send change request')))
  expect(postReview).toHaveBeenCalledWith({ change_set_id: 'change', staged_commit: receipt.staged_commit,
    catalog_digest: receipt.catalog_digest, decision: 'request_changes', comment: 'Handle an empty drawing' })
  expect(screen.getByText('Changes requested by you (reviewer)')).toBeVisible()
  expect(screen.queryByText(/Published to the/)).toBeNull()
  expect(track).toHaveBeenCalledWith('review.decided', { decision: 'request_changes', approval_kind: 'independent' })
})

it('uses the stage-bound plan and waits for its staged source before offering deploy', async () => {
  let finishSource
  getAuthorStageSource.mockReturnValue(new Promise(resolve => { finishSource = resolve }))
  getAuthorStage.mockResolvedValue({ ...staged, plan: { ...proposal.plan, summary: 'Place labels on ridgelines' } })
  await mount({ proposal: undefined })
  openDeployDetails()
  expect(screen.getByText('Place labels on ridgelines')).toBeVisible()
  expect(screen.getByText('Group by roof layer')).toBeVisible()
  expect(screen.getByText('Reading source…')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Deploy to AutoCAD' })).toBeNull()
  expect(screen.getByText('Deploy is available once the checks pass.')).toBeVisible()
  expect(postReview).not.toHaveBeenCalled()
  await act(async () => finishSource(null))
  expect(screen.getByText('source not available')).toBeVisible()
  expect(button('Deploy to AutoCAD')).toBeEnabled()
})

it.each(['branch_package', 'tool'])('uses the staged %s manifest in the absence of a plan', async field => {
  const tool = { name: 'roof-label', version: '1.2.0', description: 'Labels <roof> faces', capabilities: ['drawing.write'] }
  getAuthorStage.mockResolvedValue({ ...staged, change_kind: 'revise', result: { [field]: tool } })
  await mount({ proposal: undefined })
  openDeployDetails()
  expect(screen.getByRole('heading', { name: 'roof-label · 1.2.0' })).toBeVisible()
  expect(screen.getByText('Labels <roof> faces')).toBeVisible()
  expect(document.querySelector('roof')).toBeNull()
  expect(screen.getByText('Changes your drawing · Revises the existing tool roof-label')).toBeVisible()
  expect(button('Deploy to AutoCAD')).toBeEnabled()
})

it('withholds deploy without a bound plan or staged manifest, even after reading source', async () => {
  await mount({ proposal: undefined })
  openDeployDetails()
  expect(screen.getByText(/main\.csx · 2 lines/)).toBeVisible()
  expect(screen.getByText('Build context not reported')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Deploy to AutoCAD' })).toBeNull()
})

it('keeps the staged polling deadline across a hidden interval', async () => {
  vi.useFakeTimers()
  getAuthorStage.mockResolvedValue({ ...staged, retry_after_ms: 10000, updated_at: Date.now() - 29 * 60000 })
  const view = await mount()
  await advance(10000)
  expect(getAuthorStage).toHaveBeenCalledTimes(2)
  const card = active => <AuthoringCard sessionId="session" changeSetId="change" proposal={proposal} active={active} />
  view.rerender(card(false))
  await advance(60000)
  view.rerender(card(true))
  await advance(10000)
  expect(getAuthorStage).toHaveBeenCalledTimes(2)
})

it('renders a historical review by role, a Studio test run and Branch evidence', async () => {
  getAuthorStage.mockResolvedValue({ ...staged, compile_attempts: [], result: { test_run: { status: 'failed', summary: 'No result' } },
    branch: { synced: true, compile: { status: 'passed' }, ran: true, summary: 'Drew 3 labels' } })
  reviewContext.mockResolvedValue({ viewer_may_approve: false,
    review: { decision: 'approve', approver_role: 'owner', decided_at: '2026-11-21T03:14:00Z' } })
  await mount()
  expect(screen.getByText(/Approved by owner/)).toBeVisible()
  expect(screen.getByText('Test run · failed · No result')).toBeVisible()
  expect(screen.getByText('Synced · Compile · passed · Ran · Drew 3 labels')).toBeVisible()
  expect(screen.queryByRole('button')).toBeNull()
  expect(getAuthorStageSource).not.toHaveBeenCalled()
})

it('pauses while offscreen, respects retry_after_ms, and stops after a compile failure', async () => {
  vi.useFakeTimers()
  let visibility
  vi.stubGlobal('IntersectionObserver', class {
    constructor(callback) { visibility = callback }
    observe() {}
    disconnect() {}
  })
  getAuthorStage.mockResolvedValueOnce({ change_set_id: 'change', status: 'queued', retry_after_ms: 3500 })
    .mockResolvedValueOnce({ change_set_id: 'change', status: 'running', retry_after_ms: 3500 })
    .mockResolvedValueOnce({ change_set_id: 'change', status: 'failed', error: { message: 'Unexpected delimiter' },
      compile_attempts: [{ attempt: 1, status: 'refused', diagnostics: [{ code: 'CS1026', file: 'main.csx', line: 50 }] }] })
  await mount()
  expect(getAuthorStage).not.toHaveBeenCalled()
  await act(async () => visibility([{ isIntersecting: true }]))
  expect(screen.getByText('queued')).toBeVisible()
  act(() => visibility([{ isIntersecting: false }]))
  await advance(7000)
  expect(getAuthorStage).toHaveBeenCalledTimes(1)
  await act(async () => visibility([{ isIntersecting: true }]))
  await advance(3499)
  expect(getAuthorStage).toHaveBeenCalledTimes(2)
  await advance(1)
  expect(screen.getByText('Unexpected delimiter')).toBeVisible()
  expect(screen.getByText('Attempt 1: refused, CS1026 main.csx:50')).toBeVisible()
  expect(screen.getByText(/uses one author run/)).toBeVisible()
  await act(async () => fireEvent.click(button('Fix with these errors')))
  expect(postMessage).toHaveBeenCalledWith('session', { text: expect.stringContaining('"retry_of_change_set_id":"change"') })
  expect(postMessage.mock.calls[0][1].text).toContain('CS1026 main.csx:50')
  expect(button('Retry requested')).toBeDisabled()
  await advance(10000)
  expect(getAuthorStage).toHaveBeenCalledTimes(3)
  expect(reviewContext).not.toHaveBeenCalled()
})

it('withholds review for a mismatched receipt and can re-read the stage', async () => {
  getAuthorStage.mockResolvedValueOnce({ ...staged, receipt: { ...receipt, change_set_id: 'another' } })
  await mount()
  expect(screen.getByRole('alert')).toHaveTextContent('did not match')
  expect(reviewContext).not.toHaveBeenCalled()
  await act(async () => fireEvent.click(button('Check again')))
  expect(button('Deploy to AutoCAD')).toBeEnabled()
})

it('reads the stage contract: compile attempts by n and the Branch receipt by last_event', async () => {
  getAuthorStage.mockResolvedValue({ ...staged, compile_attempts: undefined,
    compile: { attempts: [
      { n: 1, decision: 'compile_failed', errors: 1, diagnostics: [{ code: 'CS1026', file: 'main.csx', line: 50 }] },
      { n: 2, decision: 'accept', errors: 0, diagnostics: [] },
    ] },
    status: 'published', publication: { state: 'published' },
    branch: { last_event: 'synced', at: '2026-11-21T14:46:00Z', plugin_version: '1.4.52.0' } })
  reviewContext.mockResolvedValue({ viewer_may_approve: false, review: { decision: 'approve' } })
  await mount()
  expect(screen.getByText('Attempt 1: refused, CS1026 main.csx:50')).toBeVisible()
  expect(screen.getByText('Attempt 2: passed')).toBeVisible()
  expect(screen.getByText('Synced')).toBeVisible()
  expect(screen.queryByText('Branch has not reported yet')).toBeNull()
})
