import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import useCampaigns from './useCampaigns.js'
import * as api from './api.js'

vi.mock('./api.js')
vi.mock('../api.js', () => ({
  config: { apiBase: 'https://campaign.test', tenant: 'test-tenant' },
  authHeaders: () => {
    const token = localStorage.getItem('leaf.jwt')
    return token ? { Authorization: `Bearer ${token}` } : {}
  },
}))

const P = '11111111-1111-1111-1111-111111111111'
const B = '22222222-2222-2222-2222-222222222222'
const C = '33333333-3333-3333-3333-333333333333'
const D = '44444444-4444-4444-4444-444444444444'
const Q = '55555555-5555-5555-5555-555555555555'
const row = { campaign_id: C, title: 'Campaign', status: 'accepted', dispatch: { available: false } }
const open = { question_id: Q, prompt: 'Which format?', status: 'open' }
const receipt = { question_id: Q, answer: 'Use PDF.' }
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

beforeEach(() => {
  vi.resetAllMocks()
  api.listCampaigns.mockResolvedValue({ campaigns: [row] })
  api.getCampaign.mockImplementation(async (_project, id) => ({ campaign: { ...row, campaign_id: id } }))
  api.listQuestions.mockResolvedValue({ questions: [open] })
  api.submitCampaign.mockResolvedValue({ campaign: row })
  api.askQuestion.mockResolvedValue({ question: open })
  api.answerQuestion.mockResolvedValue({ answer: receipt })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear() })

async function ready() {
  const hook = renderHook(() => useCampaigns(P, { enabled: true }))
  await waitFor(() => expect(hook.result.current.status).toBe('ready'))
  return hook
}

describe('project campaign hook', () => {
  it('selects a submitted replay and refetches before resolving, blocking duplicate submit', async () => {
    const hook = await ready()
    const pending = deferred()
    api.submitCampaign.mockReturnValue(pending.promise)
    let first
    act(() => { first = hook.result.current.submit({ title: 'New', prompt: 'Build it' }) })
    expect(hook.result.current.pending.submit).toBe(true)
    await act(async () => { await hook.result.current.submit({ title: 'New', prompt: 'Build it' }) })
    expect(api.submitCampaign).toHaveBeenCalledTimes(1)
    const key = api.submitCampaign.mock.calls[0][0].idempotencyKey
    expect(key).toBeTruthy()
    expect(api.submitCampaign).toHaveBeenCalledWith({ projectId: P, title: 'New', prompt: 'Build it', idempotencyKey: key })
    api.listCampaigns.mockResolvedValue({ campaigns: [row, { ...row, campaign_id: D }] })
    await act(async () => { pending.resolve({ campaign: { ...row, campaign_id: D, replayed: true } }); await first })
    expect(hook.result.current.selectedId).toBe(D)
    expect(api.listCampaigns).toHaveBeenCalledTimes(2)
    expect(hook.result.current.pending.submit).toBeFalsy()
  })

  it('keeps a failed draft key for retry and replaces it for changed drafts and success', async () => {
    const hook = await ready()
    const failure = Object.assign(new Error('Network unavailable'), { status: 0 })
    api.submitCampaign.mockRejectedValue(failure)
    for (const prompt of ['Same', 'Same', 'Changed']) {
      await act(async () => { await expect(hook.result.current.submit({ title: 'Title', prompt })).rejects.toBe(failure) })
    }
    const keys = api.submitCampaign.mock.calls.map(([arg]) => arg.idempotencyKey)
    expect(keys[0]).toBe(keys[1])
    expect(keys[2]).not.toBe(keys[1])
    api.submitCampaign.mockResolvedValue({ campaign: row })
    await act(async () => { await hook.result.current.submit({ title: 'Title', prompt: 'Changed' }) })
    await act(async () => { await hook.result.current.submit({ title: 'Title', prompt: 'Changed' }) })
    expect(api.submitCampaign.mock.calls[4][0].idempotencyKey).not.toBe(keys[2])
  })

  it('restores authoritative answer text on a fresh mount without a POST', async () => {
    api.listQuestions.mockResolvedValue({ questions: [{ ...open, status: 'answered', answer: receipt }] })
    const hook = await ready()
    expect(hook.result.current.questions[0].status).toBe('answered')
    expect(hook.result.current.answers[Q].answer).toBe('Use PDF.')
    expect(api.answerQuestion).not.toHaveBeenCalled()
  })

  it.each([false, true])('refetches an answer receipt (replayed: %s) and blocks duplicate answers', async replayed => {
    const hook = await ready()
    const pending = deferred()
    api.answerQuestion.mockReturnValue(pending.promise)
    let first
    act(() => { first = hook.result.current.answer(Q, 'Use PDF.') })
    await act(async () => { await hook.result.current.answer(Q, 'Use PDF.') })
    expect(api.answerQuestion).toHaveBeenCalledTimes(1)
    api.listQuestions.mockResolvedValue({ questions: [{ ...open, status: 'answered', answer: { ...receipt, answer: 'Persisted answer' } }] })
    await act(async () => { pending.resolve({ answer: { ...receipt, replayed } }); await first })
    expect(hook.result.current.questions[0].status).toBe('answered')
    expect(hook.result.current.answers[Q].answer).toBe('Persisted answer')
    expect(hook.result.current.pending[`answer:${Q}`]).toBeFalsy()
  })

  it('preserves an open row and untouched error on conflict without forcing a refresh', async () => {
    const hook = await ready()
    const failure = Object.assign(new Error('Answer conflict'), { status: 409, code: 'answer_conflict' })
    api.answerQuestion.mockRejectedValue(failure)
    await act(async () => { await expect(hook.result.current.answer(Q, 'Other')).rejects.toBe(failure) })
    expect(hook.result.current.questions).toEqual([open])
    expect(hook.result.current.answers).toEqual({})
    expect(hook.result.current.error).toBe(failure)
    expect(api.listCampaigns).toHaveBeenCalledTimes(1)
  })

  it('drops a delayed project A list after switching to B', async () => {
    const pending = deferred()
    api.listCampaigns.mockImplementation(project => project === P ? pending.promise : Promise.resolve({ campaigns: [{ ...row, campaign_id: D }] }))
    const seen = []
    const hook = renderHook(({ project }) => {
      const value = useCampaigns(project, { enabled: true })
      seen.push({ project, ids: value.campaigns.map(item => item.campaign_id) })
      return value
    }, { initialProps: { project: P } })
    hook.rerender({ project: B })
    expect(hook.result.current.campaigns).toEqual([])
    await waitFor(() => expect(hook.result.current.selectedId).toBe(D))
    await act(async () => { pending.resolve({ campaigns: [row] }); await pending.promise })
    expect(hook.result.current.selectedId).toBe(D)
    expect(seen.filter(item => item.project === B).every(item => !item.ids.includes(C))).toBe(true)
  })

  it('drops old questions and mutation responses after a selection change', async () => {
    const hook = await ready()
    api.listCampaigns.mockResolvedValue({ campaigns: [row, { ...row, campaign_id: D }] })
    const pending = deferred()
    api.answerQuestion.mockReturnValue(pending.promise)
    let answer
    act(() => { answer = hook.result.current.answer(Q, 'Old draft') })
    api.listQuestions.mockImplementation(async (_project, id) => ({ questions: id === D ? [] : [open] }))
    await act(async () => { await hook.result.current.select(D) })
    const calls = api.listCampaigns.mock.calls.length
    await act(async () => { pending.resolve({ answer: receipt }); await answer })
    expect(hook.result.current.selectedId).toBe(D)
    expect(hook.result.current.questions).toEqual([])
    expect(hook.result.current.answers).toEqual({})
    expect(api.listCampaigns).toHaveBeenCalledTimes(calls)
  })

  it('drops delayed selection reads and responses after unmount', async () => {
    const hook = await ready()
    api.listCampaigns.mockResolvedValue({ campaigns: [row, { ...row, campaign_id: D }] })
    const old = deferred()
    api.listQuestions.mockImplementation((_project, id) => id === D ? old.promise : Promise.resolve({ questions: [open] }))
    let selection
    act(() => { selection = hook.result.current.select(D) })
    await waitFor(() => expect(api.listQuestions).toHaveBeenCalledWith(P, D))
    await act(async () => { await hook.result.current.select(C) })
    await act(async () => { old.resolve({ questions: [] }); await selection })
    expect(hook.result.current.questions).toEqual([open])
    const mutation = deferred()
    api.submitCampaign.mockReturnValue(mutation.promise)
    let submitting
    act(() => { submitting = hook.result.current.submit({ title: 'Title', prompt: 'Prompt' }) })
    const calls = api.listCampaigns.mock.calls.length
    hook.unmount()
    await act(async () => { mutation.resolve({ campaign: row }); await submitting })
    expect(api.listCampaigns).toHaveBeenCalledTimes(calls)
  })

  it('retains the last snapshot after a failed refresh and retries generated question keys', async () => {
    const hook = await ready()
    const failure = new Error('Unavailable')
    api.askQuestion.mockRejectedValueOnce(failure)
    await act(async () => { await expect(hook.result.current.ask({ prompt: 'Why?' })).rejects.toBe(failure) })
    await act(async () => { await hook.result.current.ask({ prompt: 'Why?' }) })
    expect(api.askQuestion.mock.calls[0][2].questionKey).toBe(api.askQuestion.mock.calls[1][2].questionKey)
    api.listCampaigns.mockRejectedValue(failure)
    await act(async () => { await hook.result.current.refetch() })
    expect(hook.result.current.campaigns).toEqual([row])
    expect(hook.result.current.status).toBe('ready')
    expect(hook.result.current.errorAction).toBe('load')
  })
})

describe('real campaign HTTP transport', () => {
  let client, fetcher
  beforeEach(async () => {
    client = await vi.importActual('./api.js')
    fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) })
    vi.stubGlobal('fetch', fetcher)
    localStorage.setItem('leaf.jwt', 'test-bearer')
    localStorage.setItem('leaf.org_id', 'test-org')
  })

  it('sends exact submit headers/body and rereads bearer identity each call', async () => {
    await client.submitCampaign({ projectId: P, title: 'Title', prompt: 'Prompt', idempotencyKey: 'retry-key' })
    expect(fetcher).toHaveBeenCalledWith('https://campaign.test/api/campaigns', expect.objectContaining({
      method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'retry-key', Authorization: 'Bearer test-bearer', 'X-Tenant-Id': 'test-tenant', 'X-Org-Id': 'test-org' },
      body: JSON.stringify({ project_id: P, title: 'Title', prompt: 'Prompt' }),
    }))
    localStorage.setItem('leaf.jwt', 'replacement-bearer')
    await client.listCampaigns(P)
    expect(fetcher.mock.calls[1][1].headers.Authorization).toBe('Bearer replacement-bearer')
    expect(fetcher.mock.calls[1][0]).toBe(`https://campaign.test/api/campaigns?project_id=${P}&limit=50`)
  })

  it('sends the other four project-scoped wires without dispatch options', async () => {
    await client.getCampaign(P, C)
    await client.askQuestion(P, C, { questionKey: 'follow-up.1', prompt: 'Why?' })
    await client.listQuestions(P, C)
    await client.answerQuestion(P, C, Q, 'Use PDF.')
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      `https://campaign.test/api/campaigns/${C}?project_id=${P}`,
      `https://campaign.test/api/campaigns/${C}/questions`,
      `https://campaign.test/api/campaigns/${C}/questions?project_id=${P}`,
      `https://campaign.test/api/campaigns/${C}/questions/${Q}/answer`,
    ])
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ project_id: P, question_key: 'follow-up.1', prompt: 'Why?' })
    expect(JSON.parse(fetcher.mock.calls[3][1].body)).toEqual({ project_id: P, answer: 'Use PDF.' })
  })

  it('refuses missing bearer before fetch', async () => {
    localStorage.removeItem('leaf.jwt')
    await expect(client.listCampaigns(P)).rejects.toMatchObject({ status: 0, message: 'Sign in to submit a campaign.' })
    expect(fetcher).not.toHaveBeenCalled()
  })

  it.each([
    ['title', c => c.submitCampaign({ projectId: P, title: 'x'.repeat(201), prompt: 'Prompt', idempotencyKey: 'K' })],
    ['prompt', c => c.submitCampaign({ projectId: P, title: 'Title', prompt: 'x'.repeat(32769), idempotencyKey: 'K' })],
    ['title', c => c.submitCampaign({ projectId: P, title: '', prompt: 'Prompt', idempotencyKey: 'K' })],
    ['question key', c => c.askQuestion(P, C, { questionKey: 'bad/key', prompt: 'Why?' })],
    ['question key', c => c.askQuestion(P, C, { questionKey: 'x'.repeat(129), prompt: 'Why?' })],
    ['question', c => c.askQuestion(P, C, { questionKey: 'valid', prompt: 'x'.repeat(4097) })],
    ['answer', c => c.answerQuestion(P, C, Q, 'x'.repeat(8193))],
    ['campaign', c => c.getCampaign(P, '../bad')],
    ['question', c => c.answerQuestion(P, C, '../bad', 'Text')],
  ])('refuses invalid %s before fetch', async (invalidField, operation) => {
    await expect(operation(client)).rejects.toMatchObject({ status: 0, invalidField })
    expect(fetcher).not.toHaveBeenCalled()
  })

  it.each([
    [401, 'unauthorized', 'Your session is not signed in for this project any more.'],
    [403, 'forbidden', 'You do not have permission to do that in this project.'],
    [404, 'project_unavailable', 'That project is no longer available to you.'],
    [409, 'answer_conflict', 'This question already has a different recorded answer. Reload to see it.'],
    [503, 'campaigns_unavailable', 'Campaigns are unavailable right now; retry in a moment.'],
  ])('preserves the %s envelope and gives a readable fallback', async (status, code, message) => {
    const body = { error: { error_code: code, retryable: status === 503, message: '/api/internal -> 500' } }
    fetcher.mockResolvedValue({ ok: false, status, json: async () => body })
    await expect(client.listCampaigns(P)).rejects.toMatchObject({ status, code, message, body, retryable: status === 503 })
  })

  it('uses a plain envelope message and maps network failures to retryable status zero', async () => {
    fetcher.mockResolvedValueOnce({ ok: false, status: 400, json: async () => ({ error: { message: 'Please shorten the question.' } }) })
    await expect(client.listCampaigns(P)).rejects.toThrow('Please shorten the question.')
    fetcher.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(client.listCampaigns(P)).rejects.toMatchObject({ status: 0, retryable: true })
  })
})
