import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { listPendingApprovals, openStream, STREAM_EVENT_TYPES } from '../converse.js'
import { getAuthorStageSource, postReview } from '../api.js'
import { track } from '../telemetry.js'
import ConversePanel from './ConversePanel.jsx'

// Exercise the real message, approval, polling, review and source transports.
// Only the delivery of transcript envelopes and unrelated panel reads are stubbed.
vi.mock('../converse.js', async original => ({
  ...await original(), openStream: vi.fn(() => ({ close: vi.fn() })),
  listPendingApprovals: vi.fn(async () => []),
}))
vi.mock('../engineChanges.js', () => ({ listEngineChanges: async () => ({ kind: 'forbidden' }) }))
vi.mock('../telemetry.js', async original => ({ ...await original(), track: vi.fn() }))

const id = '19097a36-1234-4234-8234-123456789abc'
const commit = 'a'.repeat(40)
const digest = 'b'.repeat(64)
const answers = { question_id: 'qc', answers: { q1: { labels: ['Panels'] } } }
const plan = { schema: 'leaf.build-plan.v1', target: 'branch', change_kind: 'create', tool_name: 'count-panels',
  title: 'Count panels', summary: 'Count panels in the selection', behavior: ['Read selected panels'],
  inputs: [], output: 'Panel count', capabilities: ['drawing.read'], undo: 'No changes',
  assumptions: [], acceptance: ['Show the count'], answers: answers.answers }
const proposed = { confirmation_id: 'p1', plan, plan_digest: 'digest', revision: 1 }
const question = { question_id: 'qc', questions: [{ id: 'q1', header: 'Count', question: 'Which objects?',
  multiSelect: false, options: [{ label: 'Panels' }, { label: 'Roofs' }] }] }
const event = (type, turn_id, data = {}) => ({ session_id: 'session', type, turn_id, data })
const push = (...events) => act(async () => { events.forEach(openStream.mock.calls.at(-1)[2].onEvent) })
const click = (name) => act(async () => { fireEvent.click(screen.getByRole('button', { name })) })
const tick = (ms) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })
const response = (status, value) => ({ status, ok: status < 300, json: async () => value })
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.clearAllMocks() })

it.each([true, false])('retires the waiting note after the plan is resolved with approved=%s', async (approved) => {
  render(<ConversePanel sessionId="session" />)
  await push(event('turn_started', 'plan'), event('plan_proposed', 'plan', proposed),
    event('turn_complete', 'plan', { stop_reason: 'awaiting_approval' }))
  expect(screen.getByText('Waiting on your decision above.')).toBeVisible()
  await push(event('confirmation_resolved', 'plan', { confirmation_id: 'p1', approved }))
  expect(screen.queryByText('Waiting on your decision above.')).toBeNull()
})

it('keeps the waiting note until every decision card in the turn is resolved', async () => {
  render(<ConversePanel sessionId="session" />)
  await push(event('turn_started', 'plan'), event('plan_proposed', 'plan', proposed),
    event('plan_proposed', 'plan', { ...proposed, confirmation_id: 'p2' }),
    event('turn_complete', 'plan', { stop_reason: 'awaiting_approval' }))
  await push(event('confirmation_resolved', 'plan', { confirmation_id: 'p1', approved: true }))
  expect(screen.getByText('Waiting on your decision above.')).toBeVisible()
  await push(event('confirmation_resolved', 'plan', { confirmation_id: 'p2', approved: false }))
  expect(screen.queryByText('Waiting on your decision above.')).toBeNull()
})

it('keeps the waiting note when the turn has no decision card', async () => {
  render(<ConversePanel sessionId="session" />)
  await push(event('turn_started', 'plan'), event('turn_complete', 'plan', { stop_reason: 'awaiting_approval' }))
  expect(screen.getByText('Waiting on your decision above.')).toBeVisible()
})

it('takes a short prompt through questions, plan approval, two compile attempts and publication inline', async () => {
  vi.useFakeTimers()
  const stagePath = '/api/author/stages/' + id
  const reviewPath = '/api/author/reviews'
  const receipt = { change_set_id: id, staged_commit: commit, catalog_digest: digest,
    package_files: ['branch-tools/count-panels/main.csx'] }
  const status = [
    { status: 'queued' },
    { status: 'running', compile_attempts: [{ attempt: 1, status: 'refused', diagnostics: ['CS1026 main.csx:50'] }] },
    { status: 'staged', receipt, compile_attempts: [{ attempt: 2, status: 'passed' }] },
    { status: 'staged', receipt, publication: { status: 'published' }, retry_after_ms: 3200 },
    { status: 'staged', receipt, publication: { status: 'published' },
      branch: { synced: true, compile: { status: 'passed' }, ran: true, summary: 'Counted 12 panels' } },
  ]
  const requests = []
  let statusCalls = 0
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    const { pathname, search } = new URL(url, 'http://test')
    const path = pathname + search
    const body = init.body ? JSON.parse(init.body) : undefined
    if (path === '/api/tool-envelopes/tenant/status') { statusCalls++; return response(404, {}) }
    requests.push([init.method || 'GET', path, body])
    if (path === '/api/sessions/session/messages') return response(202, { turn_id: body.answers ? 'answer' : 'build' })
    if (path === '/api/agent/approvals/p1') return response(200, { resolved: true })
    if (path === stagePath) return response(200, { change_set_id: id, retry_after_ms: 100, ...status.shift() })
    if (path === reviewPath + '/context?change_set_id=' + id) {
      return response(200, { viewer_may_approve: true, approval_kind_if_approved: 'independent', review: null })
    }
    if (path === stagePath + '?view=source') {
      return { ...response(404, {}), headers: new Headers() }
    }
    if (path === reviewPath) return response(200, { change_set_id: id, status: 'published', decision: 'approve',
      approval_kind: 'independent', approver_role: 'reviewer', decided_at: '2026-11-21T03:14:00Z' })
    throw new Error('Unexpected request ' + path)
  }))
  expect(STREAM_EVENT_TYPES).toEqual(expect.arrayContaining(['questions_required', 'plan_proposed']))
  render(<ConversePanel sessionId="session" />)
  await push(event('turn_started', 'ask', { text: 'Count panels' }), event('questions_required', 'ask', question),
    event('turn_complete', 'ask'))
  await click('Panels')
  await click('Submit')
  await push(event('turn_started', 'answer', { answers }), event('plan_proposed', 'answer', proposed),
    event('turn_complete', 'answer'))
  const questions = screen.getByRole('region', { name: 'Planning questions' })
  expect(within(questions).getByText('Panels')).toBeVisible()
  expect(within(questions).queryByRole('button')).toBeNull()
  expect(screen.getByRole('region', { name: 'Build plan' })).toHaveTextContent('New tool count-panels')
  await click('Approve and build')
  const built = { tool: 'author_tool', ok: true, result: { change_set_id: id } }
  await push(event('confirmation_resolved', 'answer', { confirmation_id: 'p1', approved: true }),
    event('turn_started', 'build', { confirm: { confirmation_id: 'p1', approved: true } }),
    event('tool_call', 'build', { tool: 'author_tool' }), event('tool_result', 'build', built),
    event('tool_result', 'build', built), event('turn_complete', 'build'))
  const build = screen.getByRole('region', { name: 'Tool authoring' })
  expect(build).toHaveTextContent('Waiting to start')
  expect(build).toHaveTextContent('Not deployed yet')
  await tick(1999)
  expect(build).toHaveTextContent('Waiting to start')
  await tick(1)
  expect(build).toHaveTextContent('Attempt 1: refused, CS1026 main.csx:50')
  await tick(2000)
  expect(build).toHaveTextContent('Attempt 2: passed')
  expect(build).toHaveTextContent('Attempt 1: refused')
  expect(build).toHaveTextContent('Read selected panels')
  expect(build).toHaveTextContent('source not available')
  await click('Approve')
  expect(build).toHaveTextContent('Approved by you (reviewer)')
  expect(build).toHaveTextContent('Published to your plugin catalog')
  expect(requests.at(-1)).toEqual(['GET', stagePath, undefined])
  await tick(3199)
  expect(build).toHaveTextContent('Waiting for AutoCAD to pick it up')
  await tick(1)
  expect(build).toHaveTextContent('Synced · Compile · passed · Ran · Counted 12 panels')
  await tick(5000)
  expect(requests).toEqual([
    ['POST', '/api/sessions/session/messages', { answers }],
    ['POST', '/api/agent/approvals/p1', { approved: true }],
    ['POST', '/api/sessions/session/messages', { confirm: { confirmationId: 'p1', approved: true } }],
    ['GET', stagePath, undefined], ['GET', stagePath, undefined], ['GET', stagePath, undefined],
    ['GET', reviewPath + '/context?change_set_id=' + id, undefined], ['GET', stagePath + '?view=source', undefined],
    ['POST', reviewPath, { change_set_id: id, staged_commit: commit, catalog_digest: digest, decision: 'approve' }],
    ['GET', stagePath, undefined], ['GET', stagePath, undefined],
  ])
  expect(statusCalls).toBeGreaterThanOrEqual(1)
  expect(track).toHaveBeenCalledWith('plan.approved', { revision: 1 })
  expect(track).toHaveBeenCalledWith('review.decided', { decision: 'approve', approval_kind: 'independent' })
})

it('replays submitted answers and supersession with only the current inline decision available', async () => {
  listPendingApprovals.mockResolvedValueOnce([{ kind: 'propose_build_plan', confirmation_id: 'p2' }])
  render(<ConversePanel sessionId="session" />)
  await push(event('questions_required', 'first', question), event('turn_started', 'second', { answers }),
    event('plan_proposed', 'second', proposed), event('plan_proposed', 'second', {
      ...proposed, revision: 2, confirmation_id: 'p2', supersedes: 'p1',
    }), event('turn_complete', 'second'))
  expect(screen.getByText('Replaced by revision 2')).toBeVisible()
  expect(screen.getAllByRole('button', { name: 'Approve and build' })).toHaveLength(1)
  expect(screen.queryByRole('region', { name: 'Pending approvals' })).toBeNull()
  expect(within(screen.getByRole('region', { name: 'Planning questions' })).getByText('Panels')).toBeVisible()
})

it('keeps a pending plan outside replay history actionable when the poll returns after replay', async () => {
  let finishPoll
  listPendingApprovals.mockReturnValueOnce(new Promise(resolve => { finishPoll = resolve }))
  render(<ConversePanel sessionId="session" />)
  await push(event('plan_proposed', 'first', proposed), event('turn_complete', 'first'))
  const pending = { kind: 'propose_build_plan', session_id: 'session', tool: 'propose_plan' }
  await act(async () => finishPoll([
    { ...pending, confirmation_id: 'p1', rationale: 'Represented inline' },
    { ...pending, confirmation_id: 'outside', rationale: 'Older plan awaiting approval' },
  ]))
  const list = screen.getByRole('region', { name: 'Pending approvals' })
  expect(list).not.toHaveTextContent('Represented inline')
  expect(list).toHaveTextContent('Older plan awaiting approval')
  expect(within(list).getByRole('button', { name: 'Approve' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Approve and build' })).toBeEnabled()
})

it('replays the plan bound to this change set even when a newer plan is in the transcript', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (url.includes('/reviews/context')) return response(200, { viewer_may_approve: true })
    if (url.includes('?view=source')) return { ...response(404, {}), headers: new Headers() }
    return response(200, { change_set_id: id, status: 'staged',
      receipt: { change_set_id: id, staged_commit: commit, catalog_digest: digest } })
  }))
  render(<ConversePanel sessionId="session" />)
  await push(event('plan_proposed', 'first', { ...proposed, change_set_id: id }),
    event('confirmation_resolved', 'first', { confirmation_id: 'p1', approved: true }),
    event('plan_proposed', 'second', { ...proposed, confirmation_id: 'p2', change_set_id: 'different',
      plan: { ...plan, title: 'Draw roof labels' } }),
    event('tool_result', 'build', { tool: 'author_tool', result: { change_set_id: id } }))
  const build = screen.getByRole('region', { name: 'Tool authoring' })
  expect(build).toHaveTextContent('Count panels in the selection')
  expect(build).not.toHaveTextContent('Draw roof labels')
  expect(within(build).getByRole('button', { name: 'Approve' })).toBeEnabled()
})

it('settles the step on a foreign result and mounts the failed build only for the current result', async () => {
  const fetch = vi.fn(async () => response(200, { change_set_id: id, status: 'failed',
    error: { reason_code: 'compile_failed', message: 'Review the missing delimiter' },
    compile_attempts: [{ attempt: 1, status: 'refused', diagnostics: ['CS1026 main.csx:50'] }] }))
  vi.stubGlobal('fetch', fetch)
  const { container } = render(<ConversePanel sessionId="session" />)
  const result = event('tool_result', 'build', {
    tool: 'author_tool', ok: true, summary: 'Private build summary', result: { change_set_id: id },
  })
  await push(event('turn_started', 'build'), event('tool_call', 'build', { tool: 'author_tool' }),
    { ...result, session_id: 'another-session' })
  expect(container.querySelector('.converse-step')).toHaveAttribute('data-state', 'done')
  expect(container).not.toHaveTextContent('Private build summary')
  expect(screen.queryByRole('region', { name: 'Tool authoring' })).toBeNull()
  expect(fetch).not.toHaveBeenCalled()
  await push({ ...result, data: { ...result.data, ok: false, summary: 'Current build summary' } }, event('turn_complete', 'build'))
  expect(container.querySelectorAll('.converse-step')).toHaveLength(1)
  expect(container.querySelector('.converse-step')).toHaveAttribute('data-state', 'failed')
  expect(container.querySelector('.converse-step')).toHaveTextContent('Current build summary')
  const build = screen.getByRole('region', { name: 'Tool authoring' })
  expect(build).toHaveTextContent('Review the missing delimiter')
  expect(build).toHaveTextContent('Attempt 1: refused, CS1026 main.csx:50')
  expect(within(build).getByRole('button', { name: 'Fix with these errors' })).toBeEnabled()
  expect(fetch).toHaveBeenCalledTimes(1)
})

it('reads only source bound to the staged commit and guards review comments at the wire', async () => {
  const path = 'tools/count-panels/tool.py'
  const source = { ok: true, headers: new Headers({ 'X-Leaf-Source-Path': path, 'X-Leaf-Source-Commit': commit }),
    text: vi.fn(async () => 'return count') }
  const fetch = vi.fn().mockResolvedValue(source)
  vi.stubGlobal('fetch', fetch)
  await expect(getAuthorStageSource(id, 'c'.repeat(40))).resolves.toBeNull()
  expect(source.text).not.toHaveBeenCalled()
  await expect(getAuthorStageSource(id, commit)).resolves.toEqual({ path, text: 'return count' })
  expect(fetch.mock.calls[1][0]).toMatch(/\?view=source$/)
  await expect(postReview({ change_set_id: id, staged_commit: commit, catalog_digest: digest,
    decision: 'request_changes', comment: 'api_key: ' + 'x'.repeat(24) })).rejects.toMatchObject({ secretRefused: true })
  expect(fetch).toHaveBeenCalledTimes(2)
})
