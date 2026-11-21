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

it('shows the staged files, source, compile and plan risks before a self-approval', async () => {
  postReview.mockResolvedValue({ change_set_id: 'change', decision: 'approve', approval_kind: 'self',
    approver_role: 'owner', decided_at: '2026-11-21T03:14:00Z', status: 'published' })
  await mount()
  for (const text of ['Label every roof', 'Group by roof layer', 'This will be recorded as a self-approval.',
    'Changes your drawing · network.read · Revises the existing tool roof', 'Branch has not reported yet']) {
    expect(screen.getByText(text)).toBeVisible()
  }
  expect(screen.getByText(receipt.package_files.join(', '))).toBeVisible()
  const source = screen.getByText(/main\.csx · 2 lines/)
  fireEvent.click(source)
  expect(source.closest('details')).toHaveAttribute('open')
  expect(screen.getByText(/<img src=x>/)).toBeVisible()
  expect(document.querySelector('img')).toBeNull()
  const approve = button('Approve your own tool')
  approve.focus()
  expect(approve).toHaveFocus()
  await act(async () => approve.click())
  expect(postReview).toHaveBeenCalledWith({ change_set_id: 'change', staged_commit: receipt.staged_commit,
    catalog_digest: receipt.catalog_digest, decision: 'approve' })
  expect(screen.getByText(/Approved by you \(owner\)/)).toHaveTextContent(/\d+:\d{2}/)
  expect(screen.getByText(/Published to the internal dev catalog/)).toBeVisible()
  expect(track).toHaveBeenCalledWith('review.decided', { decision: 'approve', approval_kind: 'self' })
  expect(screen.queryByRole('button')).toBeNull()
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
  expect(button('Approve your own tool')).toBeEnabled()
})
