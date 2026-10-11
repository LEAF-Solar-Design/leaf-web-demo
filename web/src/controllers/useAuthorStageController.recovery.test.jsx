import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, renderHook, screen } from '@testing-library/react'
import { config } from '../api.js'
import { MSG_SERVICE } from '../errorHumanize.js'
import { authorAccountScope, INFLIGHT_AUTHOR_KEY } from '../authorStagePointer.js'
import AuthorPanel from '../components/AuthorPanel.jsx'
import useAuthorStageController, { authorStartFailureOf } from './useAuthorStageController.js'

const R = ['POST', `${config.apiBase}/api/author/stage/recover`]
const S = ['POST', `${config.apiBase}/api/author/stage`]
const G = ['GET', new URL('/api/author/stages/cs-recovered', new URL(config.apiBase || '/', location.origin)).toString()]
const CONTRACT = 'leaf.customization-stage-recovery.v1'
const MESSAGE = 'Could not check the saved request. Try Resume again.'
const WAITING = 'Waiting for the saved request to finish admission.'
const AUTHORITY = { sessionId: 'session-known', turnId: 'turn-known' }
const J = {
  contract: 'leaf.customization-stage-job.v1', status: 'queued',
  change_set_id: 'cs-recovered', poll_url: '/api/author/stages/cs-recovered', retry_after_ms: 250,
}
const RESULT = { receipt: { change_set_id: 'cs-recovered', state: 'staged' }, tool: { name: 'panel_counter' }, preview: 'Counted panels.' }
let fetchSpy
let authorityProvider
let timerSpy
let events

function memoryStorage() {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: vi.fn((key, value) => values.set(key, String(value))),
    removeItem: vi.fn((key) => values.delete(key)),
  }
}

function pointer(storage, extra = {}) {
  const now = Date.now()
  return {
    idempotency_key: 'saved-key', description: 'Count panels', target_tool_name: null,
    change_set_id: null, poll_url: null, retry_after_ms: null,
    account_scope: authorAccountScope(config.tenant, storage), created_at: now, expires_at: now + 60_000,
    ...extra,
  }
}

function response(status, body) {
  return { status, ok: status >= 200 && status < 300, json: vi.fn(async () => body) }
}

const found = (job = J) => response(200, { contract: CONTRACT, status: 'found', job })
const missing = () => response(404, { contract: CONTRACT, status: 'not_found' })
const pending = (retry_after_ms = 1000) => response(202, { contract: CONTRACT, status: 'admission_pending', retry_after_ms })
const accepted = () => response(202, J)
const polled = () => response(200, { status: 'staged', change_set_id: J.change_set_id, result: RESULT })
const unavailable = () => response(503, { error: { message: 'private outage', reason_code: 'customization_stage_failed' } })
const replay = () => response(409, { error: { message: 'already admitted' }, reason_code: 'idempotency_replay' })

function configureFetch(replies, beforeFetch) {
  const queue = [...replies]
  fetchSpy.mockReset().mockImplementation(async (url, init) => {
    const call = [init?.method || 'GET', url]
    events.push(call)
    beforeFetch?.(call, init)
    if (!queue.length) throw new Error('Unexpected fetch')
    const answer = queue.shift()
    if (answer instanceof Error) throw answer
    return typeof answer === 'function' ? answer(url, init) : answer
  })
}

function fixture({ saved = true, replies = [], provider = authorityProvider, storage = memoryStorage(), beforeFetch, recovery, stage } = {}) {
  const P = saved === true ? pointer(storage) : saved || null
  if (P) storage.setItem(INFLIGHT_AUTHOR_KEY, JSON.stringify(P))
  const original = storage.getItem(INFLIGHT_AUTHOR_KEY)
  storage.setItem.mockClear()
  configureFetch(replies, beforeFetch)
  const hook = renderHook(({ enabled }) => useAuthorStageController({
    storage, enabled, authorityProvider: provider,
    ...(recovery ? { recoverAuthorStage: recovery } : {}),
    ...(stage ? { stageAuthorTool: stage } : {}),
  }), { initialProps: { enabled: true } })
  return { ...hook, storage, P, original, provider }
}

async function flush() {
  await act(async () => { for (let step = 0; step < 40; step += 1) await Promise.resolve() })
}

async function advance(ms) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
  await flush()
}

function calls(expected) {
  expect(fetchSpy.mock.calls.map(([url, init]) => [init?.method || 'GET', url])).toEqual(expected)
}

function noAuthorityStored(storage) {
  for (const [key, bytes] of storage.setItem.mock.calls) {
    expect(`${key} ${bytes}`).not.toMatch(/session-known|turn-known|sessionId|turnId|authority|X-Authority/)
  }
}

function successful(f) {
  expect(f.result.current).toMatchObject({ phase: 'succeeded', error: null, progress: 'staged for review', result: RESULT })
  expect(JSON.parse(f.storage.getItem(INFLIGHT_AUTHOR_KEY))).toEqual({
    ...f.P, change_set_id: J.change_set_id, poll_url: J.poll_url, retry_after_ms: J.retry_after_ms,
    terminal_failed: false, terminal_staged: true, staged_result: RESULT,
  })
  noAuthorityStored(f.storage)
}

function interrupted(f, waiting = false) {
  expect(f.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBe(f.original)
  expect(f.result.current).toMatchObject({
    phase: 'interrupted', progress: waiting ? WAITING : 'connection interrupted',
    active: false, resumable: true, failedRequest: null,
  })
  expect(f.result.current.error.message).toBe(waiting ? WAITING : MESSAGE)
  expect(JSON.parse(f.original)).not.toHaveProperty('terminal_failed')
  if (!waiting) {
    expect(f.result.current.error).toMatchObject({ code: 'author_recovery_failed', authorRecovery: true, authorTerminal: false })
    expect(authorStartFailureOf(f.result.current.error)).toBeNull()
    for (const key of ['body', 'grantRequired', 'entitlementRequired', 'quotaExceeded']) expect(f.result.current.error).not.toHaveProperty(key)
  }
  expect(f.storage.setItem).not.toHaveBeenCalled()
  noAuthorityStored(f.storage)
}

function captureFresh(f) {
  const write = f.storage.setItem.mock.calls.find(([key]) => key === INFLIGHT_AUTHOR_KEY)
  f.original = write[1]
  f.P = JSON.parse(f.original)
}

function snapshot(f) {
  const { pointer: current, phase, progress, error, result } = f.result.current
  return { pointer: current, phase, progress, error, result }
}

function switchAccount(f) {
  f.storage.setItem('leaf.org_id', 'account-B')
  const next = pointer(f.storage, { idempotency_key: 'account-B-key', description: 'Account B request' })
  f.storage.setItem(INFLIGHT_AUTHOR_KEY, JSON.stringify(next))
  f.rerender({ enabled: true })
  return f.storage.getItem(INFLIGHT_AUTHOR_KEY)
}

beforeEach(() => {
  localStorage.clear()
  vi.useFakeTimers()
  events = []
  fetchSpy = vi.fn()
  vi.stubGlobal('fetch', fetchSpy)
  authorityProvider = vi.fn(async () => { events.push('mint'); return { ...AUTHORITY } })
  timerSpy = vi.spyOn(globalThis, 'setTimeout')
})

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('saved author stage recovery', () => {
  it('C8C-09 a saved poll address resumes without recovery or minting', async () => {
    const storage = memoryStorage()
    const f = fixture({ storage, saved: pointer(storage, { change_set_id: J.change_set_id, poll_url: J.poll_url, retry_after_ms: 250 }), replies: [polled()] })
    await flush(); await advance(250)
    calls([G]); expect(authorityProvider).not.toHaveBeenCalled(); successful(f)
  })

  it('C8C-10 mount recovers a lost acceptance before polling', async () => {
    const storage = memoryStorage()
    const f = fixture({ storage, replies: [found(), polled()], beforeFetch: (call) => {
      if (call[0] === 'GET') expect(JSON.parse(storage.getItem(INFLIGHT_AUTHOR_KEY))).toMatchObject({ change_set_id: J.change_set_id, poll_url: J.poll_url })
    } })
    await flush(); await advance(250)
    calls([R, G]); expect(authorityProvider).not.toHaveBeenCalled(); successful(f)
  })

  it('C8C-11 explicit Resume recovers before polling', async () => {
    const f = fixture({ replies: [unavailable()] })
    await flush(); interrupted(f)
    configureFetch([found(), polled()])
    act(() => { void f.result.current.resume() })
    await flush(); await advance(250)
    calls([R, G]); expect(authorityProvider).not.toHaveBeenCalled(); successful(f)
  })

  it('C8C-12 retry recovers the stored request instead of submitting new text', async () => {
    const f = fixture({ replies: [unavailable()] })
    await flush(); interrupted(f)
    configureFetch([found(), polled()])
    act(() => { void f.result.current.stage('Different text') })
    await flush(); await advance(250)
    calls([R, G]); expect(authorityProvider).not.toHaveBeenCalled(); successful(f)
    expect(f.result.current.pointer.description).toBe('Count panels')
    expect(f.result.current.pointer.idempotency_key).toBe('saved-key')
  })

  it('C8C-13 contracted not found permits the existing mint and stage path', async () => {
    const answer = missing()
    const provider = vi.fn(async () => {
      expect(answer.json).toHaveBeenCalledTimes(1)
      events.push('mint')
      return { ...AUTHORITY }
    })
    const f = fixture({ provider, replies: [answer, accepted(), polled()] })
    await flush(); await advance(250)
    calls([R, S, G]); expect(provider).toHaveBeenCalledTimes(1)
    expect(events).toEqual([R, 'mint', S, G])
    expect(JSON.parse(fetchSpy.mock.calls[1][1].body)).toEqual({ description: f.P.description, mode: 'build', idempotency_key: f.P.idempotency_key })
    expect(fetchSpy.mock.calls[1][1].headers).toMatchObject({ 'X-Authority-Session-Id': AUTHORITY.sessionId, 'X-Authority-Turn-Id': AUTHORITY.turnId })
    for (const index of [0, 2]) expect(fetchSpy.mock.calls[index][1].headers).not.toHaveProperty('X-Authority-Session-Id')
    successful(f)
  })

  it('C8C-14 fresh submission stages without recovery', async () => {
    const f = fixture({ saved: false, replies: [accepted(), polled()] })
    act(() => { void f.result.current.stage('Count panels') })
    await flush(); captureFresh(f); await advance(250)
    calls([S, G]); expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(JSON.parse(fetchSpy.mock.calls[0][1].body).idempotency_key).toBe(f.P.idempotency_key)
    successful(f)
  })

  it('C8C-15 pending waits and recovers again without minting', async () => {
    const f = fixture({ replies: [pending(), found(), polled()] })
    await flush(); await advance(999)
    calls([R]); expect(authorityProvider).not.toHaveBeenCalled()
    expect(f.result.current).toMatchObject({ phase: 'reconnecting', progress: WAITING, error: null })
    expect(f.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBe(f.original)
    await advance(1); await advance(250)
    calls([R, R, G]); expect(authorityProvider).not.toHaveBeenCalled(); successful(f)
  })

  it('C8C-16 three pending answers stop in a resumable waiting state', async () => {
    const f = fixture({ replies: [pending(), pending(), pending()] })
    await flush(); await advance(1000); await advance(1000)
    calls([R, R, R]); expect(timerSpy.mock.calls.map((call) => call[1])).toEqual([1000, 1000])
    interrupted(f, true); expect(authorityProvider).not.toHaveBeenCalled()
    await advance(10_000); calls([R, R, R])
  })

  it('C8C-17 Resume starts a new recovery budget after pending exhaustion', async () => {
    const f = fixture({ replies: [pending(), pending(), pending()] })
    await flush(); await advance(2000); interrupted(f, true)
    configureFetch([found(), polled()])
    act(() => { void f.result.current.resume() })
    await flush(); await advance(250)
    calls([R, G]); expect(authorityProvider).not.toHaveBeenCalled(); successful(f)
  })

  it('C8C-18 recovery waits use the existing retry clamp', async () => {
    const f = fixture({ replies: [pending(1), pending(9000), found(), polled()] })
    await flush(); await advance(249); calls([R])
    await advance(1); calls([R, R])
    await advance(4999); calls([R, R])
    await advance(1); calls([R, R, R])
    await advance(250); calls([R, R, R, G])
    expect(timerSpy.mock.calls.map((call) => call[1])).toEqual([250, 5000, 250])
    expect(authorityProvider).not.toHaveBeenCalled(); successful(f)
  })

  it('C8C-19 unmount aborts a pending recovery wait', async () => {
    const f = fixture({ replies: [pending()] })
    await flush()
    const last = snapshot(f)
    expect(last).toMatchObject({ phase: 'reconnecting', progress: WAITING, error: null })
    const signal = fetchSpy.mock.calls[0][1].signal
    f.unmount(); await advance(10_000)
    calls([R]); expect(signal.aborted).toBe(true)
    expect(authorityProvider).not.toHaveBeenCalled()
    expect(f.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBe(f.original)
    expect(f.storage.setItem).not.toHaveBeenCalled()
    expect(snapshot(f)).toEqual(last)
  })

  it('C8C-20 recovery 503 preserves the request and never mints', async () => {
    const f = fixture({ replies: [unavailable()] })
    await flush(); calls([R]); interrupted(f); expect(authorityProvider).not.toHaveBeenCalled()
  })

  it('C8C-21 a bare recovery 404 never permits submission', async () => {
    const f = fixture({ replies: [response(404, { reason_code: 'customization_stage_disabled' })] })
    await flush(); calls([R]); interrupted(f); expect(authorityProvider).not.toHaveBeenCalled()
  })

  it('C8C-22 other recovery refusals preserve the unresolved request', async () => {
    for (const status of [401, 403, 409, 422, 429, 500, 502]) {
      const f = fixture({ replies: [response(status, { error: { message: 'private refusal', grant_required: true }, quota_kind: 'daily_author' })] })
      await flush(); calls([R]); interrupted(f); expect(authorityProvider).not.toHaveBeenCalled()
      f.unmount()
    }
  })

  it('C8C-23 recovery network and malformed-response failures never mint', async () => {
    const malformed = response(200, null)
    malformed.json.mockRejectedValue(new SyntaxError('private invalid JSON'))
    for (const answer of [new TypeError('Failed to fetch'), malformed, response(200, { contract: 'wrong', status: 'found', job: J })]) {
      const f = fixture({ replies: [answer] })
      await flush(); calls([R]); interrupted(f); expect(authorityProvider).not.toHaveBeenCalled()
      f.unmount()
    }
  })

  it('C8C-24 an invalid recovered identity or URL is never persisted', async () => {
    const fixtures = [
      { savedId: 'expected-id', job: J },
      { job: { ...J, receipt: { change_set_id: 'rejected-id' } } },
      { job: { ...J, poll_url: 'https://foreign.invalid/api/author/stages/cs-recovered' } },
      { job: { ...J, poll_url: '/api/author/jobs/cs-recovered' } },
    ]
    for (const { savedId, job } of fixtures) {
      const storage = memoryStorage()
      const f = fixture({ storage, saved: pointer(storage, { change_set_id: savedId || null }), replies: [found(job)] })
      await flush(); calls([R]); interrupted(f); expect(authorityProvider).not.toHaveBeenCalled()
      expect(storage.setItem).not.toHaveBeenCalled()
      f.unmount()
    }
  })

  it('C8C-25 account changes make recovery completions inert', async () => {
    for (const outcome of ['found', 'not_found', 'rejection']) {
      let release
      const delayed = new Promise((resolve, reject) => { release = outcome === 'rejection' ? reject : resolve })
      const f = fixture({ replies: [() => delayed] })
      await flush(); calls([R])
      const bytes = switchAccount(f)
      await flush()
      const before = snapshot(f)
      const writes = f.storage.setItem.mock.calls.length
      await act(async () => { release(outcome === 'rejection' ? new TypeError('Failed to fetch') : outcome === 'found' ? found() : missing()) })
      await flush(); await advance(10_000)
      calls([R]); expect(authorityProvider).not.toHaveBeenCalled()
      expect(f.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBe(bytes)
      expect(f.storage.setItem).toHaveBeenCalledTimes(writes)
      expect(snapshot(f)).toEqual(before)
      f.unmount()
    }
  })

  it('C8C-26 a stale pending loop sends no recovery request in the new account', async () => {
    const f = fixture({ replies: [pending()] })
    await flush()
    const bytes = switchAccount(f)
    const before = snapshot(f)
    const writes = f.storage.setItem.mock.calls.length
    await advance(10_000)
    calls([R]); expect(authorityProvider).not.toHaveBeenCalled()
    expect(f.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBe(bytes)
    expect(f.storage.setItem).toHaveBeenCalledTimes(writes)
    expect(snapshot(f)).toEqual(before)
  })

  it('C8C-27 not found reuses the same-key C8B authority hold', async () => {
    const f = fixture({ saved: false, replies: [new Error('Connection lost'), missing(), accepted(), polled()] })
    act(() => { void f.result.current.stage('Count panels') })
    await flush(); captureFresh(f)
    expect(f.result.current.phase).toBe('interrupted')
    act(() => { void f.result.current.resume() })
    await flush(); await advance(250)
    calls([S, R, S, G]); expect(authorityProvider).toHaveBeenCalledTimes(1)
    const first = fetchSpy.mock.calls[0][1]
    const repeat = fetchSpy.mock.calls[2][1]
    expect(repeat.body).toBe(first.body)
    expect(repeat.headers).toEqual(first.headers)
    expect(first.headers).toMatchObject({ 'X-Authority-Session-Id': AUTHORITY.sessionId, 'X-Authority-Turn-Id': AUTHORITY.turnId })
    expect(JSON.parse(fetchSpy.mock.calls[1][1].body)).toEqual({ idempotency_key: f.P.idempotency_key })
    successful(f)
  })

  it('C8C-28 found recovery bypasses an existing authority hold', async () => {
    const f = fixture({ saved: false, replies: [new Error('Connection lost'), found(), polled()] })
    act(() => { void f.result.current.stage('Count panels') })
    await flush(); captureFresh(f)
    act(() => { void f.result.current.resume() })
    await flush(); await advance(250)
    calls([S, R, G]); expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(JSON.parse(fetchSpy.mock.calls[1][1].body)).toEqual({ idempotency_key: f.P.idempotency_key })
    successful(f)
  })

  it('C8C-29 a racing stage replay recovers and polls once', async () => {
    const f = fixture({ saved: false, replies: [replay(), found(), polled()] })
    act(() => { void f.result.current.stage('Count panels') })
    await flush(); captureFresh(f); await advance(250)
    calls([S, R, G]); expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(JSON.parse(fetchSpy.mock.calls[1][1].body)).toEqual({ idempotency_key: JSON.parse(fetchSpy.mock.calls[0][1].body).idempotency_key })
    successful(f)
  })

  it('C8C-30 replay recovery never resubmits after not found or failure', async () => {
    for (const answer of [missing(), unavailable(), response(404, {}), found({ ...J, poll_url: '/api/author/jobs/cs-recovered' })]) {
      authorityProvider.mockClear()
      const f = fixture({ saved: false, replies: [replay(), answer] })
      act(() => { void f.result.current.stage('Count panels') })
      await flush(); captureFresh(f)
      expect(f.storage.setItem).toHaveBeenCalledTimes(1)
      f.storage.setItem.mockClear()
      calls([S, R]); expect(authorityProvider).toHaveBeenCalledTimes(1); interrupted(f)
      await advance(10_000); calls([S, R])
      f.unmount()
    }
  })

  it('C8C-31 replay recovery pending stops after one lookup', async () => {
    const f = fixture({ saved: false, replies: [replay(), pending()] })
    act(() => { void f.result.current.stage('Count panels') })
    await flush(); captureFresh(f)
    expect(f.storage.setItem).toHaveBeenCalledTimes(1)
    f.storage.setItem.mockClear()
    calls([S, R]); expect(authorityProvider).toHaveBeenCalledTimes(1); interrupted(f, true)
    expect(timerSpy).not.toHaveBeenCalled()
    await advance(10_000); calls([S, R])
  })

  it('C8C-32 generic stage 409 retains its existing refusal behavior', async () => {
    const body = { error: { message: 'The stage authority was refused.' }, reason_code: 'stage_authority_invalid' }
    const f = fixture({ saved: false, replies: [response(409, body)] })
    act(() => { void f.result.current.stage('Count panels') })
    await flush()
    calls([S]); expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(f.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBeNull()
    expect(f.result.current.pointer).toBeNull()
    expect(f.result.current.phase).toBe('failed')
    expect(f.result.current.error).toMatchObject({ status: 409, authorTerminal: true, body, message: 'The stage authority was refused.' })
  })

  it('C8C-33 recovered staged jobs still pass through acceptance and polling', async () => {
    const job = { ...J, status: 'staged', result: { receipt: RESULT.receipt, tool: { name: 'recovery_metadata' } } }
    const storage = memoryStorage()
    const f = fixture({ storage, replies: [found(job), polled()], beforeFetch: (call) => {
      if (call[0] === 'GET') expect(JSON.parse(storage.getItem(INFLIGHT_AUTHOR_KEY))).toMatchObject({ poll_url: J.poll_url, change_set_id: J.change_set_id })
    } })
    await flush()
    expect(f.result.current.result).toBeNull()
    expect(JSON.parse(storage.getItem(INFLIGHT_AUTHOR_KEY))).not.toHaveProperty('terminal_staged')
    await advance(250)
    calls([R, G]); expect(authorityProvider).not.toHaveBeenCalled(); successful(f)
  })

  it('C8C-34 recovered failed jobs become terminal only through the existing poll path', async () => {
    const storage = memoryStorage()
    const f = fixture({ storage, replies: [found({ ...J, status: 'failed' }), response(200, {
      contract: J.contract, status: 'failed', change_set_id: J.change_set_id,
      error: { message: 'The saved authoring job failed.', reason_code: 'customization_author_job_failed' },
    })], beforeFetch: (call) => {
      if (call[0] === 'GET') expect(JSON.parse(storage.getItem(INFLIGHT_AUTHOR_KEY))).toMatchObject({ poll_url: J.poll_url, change_set_id: J.change_set_id })
    } })
    await flush()
    expect(f.result.current.phase).toBe('reconnecting')
    expect(f.result.current.error).toBeNull()
    expect(JSON.parse(storage.getItem(INFLIGHT_AUTHOR_KEY))).not.toHaveProperty('terminal_failed')
    await advance(250)
    calls([R, G]); expect(authorityProvider).not.toHaveBeenCalled()
    expect(f.result.current.phase).toBe('failed')
    // The existing poll path words a failed job through customizationError(..., 500, body).
    expect(f.result.current.error.message).toBe(MSG_SERVICE)
    expect(JSON.parse(storage.getItem(INFLIGHT_AUTHOR_KEY))).toEqual({
      ...f.P, change_set_id: J.change_set_id, poll_url: J.poll_url, retry_after_ms: J.retry_after_ms,
      terminal_failed: true, failed_at: expect.any(Number),
      failure: { message: 'Authoring failed. Your request is saved for recovery.', status: 500, reason_code: 'customization_author_job_failed' },
    })
    noAuthorityStored(storage)
  })

  it('C8C-35 polling interruption retains the recovered address for the next Resume', async () => {
    const f = fixture({ replies: [found(), new Error('Connection lost'), polled()] })
    await flush(); await advance(250)
    calls([R, G])
    expect(f.result.current.phase).toBe('interrupted')
    expect(f.result.current.error.message).toBe('Connection lost')
    expect(JSON.parse(f.storage.getItem(INFLIGHT_AUTHOR_KEY))).toEqual({ ...f.P, change_set_id: J.change_set_id, poll_url: J.poll_url, retry_after_ms: J.retry_after_ms })
    act(() => { void f.result.current.resume() })
    await flush(); await advance(250)
    calls([R, G, G]); expect(authorityProvider).not.toHaveBeenCalled(); successful(f)
  })

  it('C8C-36 existing panel seams show recovery failure and waiting sentences', async () => {
    for (const waiting of [false, true]) {
      const storage = memoryStorage()
      const P = pointer(storage)
      storage.setItem(INFLIGHT_AUTHOR_KEY, JSON.stringify(P))
      const f = { storage, P, original: storage.getItem(INFLIGHT_AUTHOR_KEY), result: { current: null } }
      storage.setItem.mockClear()
      configureFetch(waiting ? [pending(), pending(), pending()] : [unavailable()])
      function Panel() {
        const controller = useAuthorStageController({ storage, authorityProvider })
        f.result.current = controller
        return <AuthorPanel onAuthor={controller.stage} stageActivity={controller} onResumeAuthor={controller.resume} />
      }
      const view = render(<Panel />)
      await flush()
      if (waiting) await advance(2000)
      interrupted(f, waiting)
      // The panel renders the sentence inside its failure line, beside a prefix and the reason code.
      expect(screen.getByText(waiting ? WAITING : MESSAGE, { exact: false })).toBeTruthy()
      expect(screen.getByRole('button', { name: 'Resume authoring' })).toBeTruthy()
      expect(screen.queryByRole('button', { name: 'Start new attempt' })).toBeNull()
      calls(waiting ? [R, R, R] : [R]); expect(authorityProvider).not.toHaveBeenCalled()
      view.unmount()
    }
  })

  it('C8C-37 recovery also gates a stored request when no provider is supplied', async () => {
    for (const isFound of [false, true]) {
      const f = fixture({ provider: null, replies: isFound ? [found(), polled()] : [missing(), accepted(), polled()] })
      await flush(); await advance(250)
      calls(isFound ? [R, G] : [R, S, G]); expect(authorityProvider).not.toHaveBeenCalled()
      for (const [, init] of fetchSpy.mock.calls) {
        expect(init.headers).not.toHaveProperty('X-Authority-Session-Id')
        expect(init.headers).not.toHaveProperty('X-Authority-Turn-Id')
      }
      successful(f)
      f.unmount()
    }
  })

  it('C8C-38 a late mint after recovery cannot submit into a changed account', async () => {
    let release
    const provider = vi.fn(() => new Promise((resolve) => { release = resolve }))
    const f = fixture({ provider, replies: [missing()] })
    await flush(); calls([R]); expect(provider).toHaveBeenCalledTimes(1)
    const bytes = switchAccount(f)
    await flush()
    const before = snapshot(f)
    const writes = f.storage.setItem.mock.calls.length
    await act(async () => { release({ ...AUTHORITY }) })
    await flush(); await advance(10_000)
    calls([R]); expect(provider).toHaveBeenCalledTimes(1)
    expect(f.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBe(bytes)
    expect(f.storage.setItem).toHaveBeenCalledTimes(writes)
    expect(snapshot(f)).toEqual(before)
    noAuthorityStored(f.storage)
  })

  it('C8C-39 a stage failure whose fields cannot be read is rethrown unchanged and never recovered', async () => {
    const unreadable = (field, status) => {
      const error = new Error('bad')
      if (status !== undefined) error.status = status
      Object.defineProperty(error, field, { get() { throw new Error(`${field} cannot be read`) } })
      return error
    }
    for (const [label, make] of [
      ['a status that throws', () => unreadable('status')],
      ['a replay status whose body throws', () => unreadable('body', 409)],
    ]) {
      const staged = make()
      const stage = vi.fn(async () => { throw staged })
      const recovery = vi.fn(async () => ({ status: 'not_found' }))
      const f = fixture({ saved: false, stage, recovery })
      act(() => { void f.result.current.stage('Count panels') })
      await flush()
      // Identity is compared outside expect, so a failing row never prints an object whose fields throw.
      expect(f.result.current.error === staged, label).toBe(true)
      expect(f.result.current.phase, label).toBe('interrupted')
      expect(stage, label).toHaveBeenCalledTimes(1)
      expect(recovery, label).not.toHaveBeenCalled()
      calls([])
      f.unmount()
    }
  })

  it('C8C-40 a recovery failure whose status cannot be read settles as the recovery failure', async () => {
    const hostile = new Error('lookup failed')
    Object.defineProperty(hostile, 'status', { get() { throw new Error('status cannot be read') } })
    const recovery = vi.fn(async () => { throw hostile })
    const f = fixture({ recovery })
    await flush()
    expect(recovery).toHaveBeenCalledTimes(1)
    calls([]); expect(authorityProvider).not.toHaveBeenCalled(); interrupted(f)
    expect(f.result.current.error).not.toHaveProperty('status')
  })

  it('C8C-41 recovered polling sends no status read once the account changes', async () => {
    const running = () => response(200, { status: 'running', change_set_id: J.change_set_id })
    for (const [label, replies, wait, expected] of [
      ['during the first wait', [found(), polled()], 0, [R]],
      ['after a running answer', [found(), running(), polled()], 250, [R, G]],
    ]) {
      const f = fixture({ replies })
      await flush()
      if (wait) await advance(wait)
      calls(expected)
      const bytes = switchAccount(f)
      await flush()
      const before = snapshot(f)
      const writes = f.storage.setItem.mock.calls.length
      await advance(10_000)
      calls(expected); expect(authorityProvider, label).not.toHaveBeenCalled()
      expect(f.storage.getItem(INFLIGHT_AUTHOR_KEY), label).toBe(bytes)
      expect(f.storage.setItem, label).toHaveBeenCalledTimes(writes)
      expect(snapshot(f), label).toEqual(before)
      f.unmount()
    }
  })

  it('C8C-42 a recovery answered after the panel is disabled mints, stages and writes nothing', async () => {
    for (const answer of [{ status: 'not_found' }, { status: 'found', job: J }]) {
      let release
      const recovery = vi.fn(() => new Promise((resolve) => { release = resolve }))
      const f = fixture({ recovery })
      await flush()
      expect(recovery).toHaveBeenCalledTimes(1)
      f.rerender({ enabled: false })
      await flush()
      const before = snapshot(f)
      const writes = f.storage.setItem.mock.calls.length
      await act(async () => { release(answer) })
      await flush(); await advance(10_000)
      expect(authorityProvider, answer.status).not.toHaveBeenCalled()
      calls([])
      expect(f.storage.getItem(INFLIGHT_AUTHOR_KEY), answer.status).toBe(f.original)
      expect(f.storage.setItem, answer.status).toHaveBeenCalledTimes(writes)
      expect(snapshot(f), answer.status).toEqual(before)
      f.unmount()
    }
  })

  it('C8C-43 a poll failure whose fields cannot be read settles and keeps what it cannot disprove', async () => {
    const FAILED = 'Authoring failed. Your request is saved for recovery.'
    const unreadable = (field, extra) => {
      const error = new Error('poll failed')
      Object.assign(error, extra)
      Object.defineProperty(error, field, { get() { throw new Error(`${field} cannot be read`) } })
      return error
    }
    for (const [label, make, phase] of [
      ['a terminal failure whose body cannot be read', () => unreadable('body', { authorTerminal: true }), 'interrupted'],
      ['a failure whose terminal flag cannot be read', () => unreadable('authorTerminal', {}), 'interrupted'],
      ['a failed job whose status cannot be read', () => unreadable('status', {
        authorTerminal: true, body: { change_set_id: J.change_set_id, status: 'failed' },
      }), 'failed'],
    ]) {
      const thrown = make()
      const stage = vi.fn(async () => { throw thrown })
      const recovery = vi.fn()
        .mockRejectedValueOnce(new TypeError('Failed to fetch'))
        .mockResolvedValue({ status: 'found', job: J })
      const f = fixture({ stage, recovery })
      await flush()
      expect(f.result.current.phase, label).toBe('interrupted')
      let settled = 'pending'
      await act(async () => { settled = await f.result.current.resume() })
      await flush()
      expect(settled, label).toBeNull()
      expect(recovery, label).toHaveBeenCalledTimes(2)
      expect(stage, label).toHaveBeenCalledTimes(1)
      // Identity is compared outside expect, so a failing row never prints an object whose fields throw.
      expect(f.result.current.error === thrown, label).toBe(true)
      expect(f.result.current.phase, label).toBe(phase)
      const accepted = { ...f.P, change_set_id: J.change_set_id, poll_url: J.poll_url, retry_after_ms: J.retry_after_ms }
      expect(JSON.parse(f.storage.getItem(INFLIGHT_AUTHOR_KEY)), label).toEqual(phase === 'failed'
        ? { ...accepted, terminal_failed: true, failed_at: expect.any(Number), failure: { message: FAILED, status: null, reason_code: null } }
        : accepted)
      calls([]); expect(authorityProvider, label).not.toHaveBeenCalled()
      f.unmount()
    }
  })
  it('C8C-44 a recovery stops once another request is saved in its place', async () => {
    const running = () => response(200, { status: 'running', change_set_id: J.change_set_id })
    const replace = (f) => {
      const next = pointer(f.storage, { idempotency_key: 'replacement-key', description: 'Another tab request' })
      f.storage.setItem(INFLIGHT_AUTHOR_KEY, JSON.stringify(next))
      f.rerender({ enabled: true })
      return f.storage.getItem(INFLIGHT_AUTHOR_KEY)
    }
    // Each case pauses at one point, saves another request in its place, then lets the pause end.
    for (const [label, replies, held, wait, after, minted] of [
      ['while recovery is answering', [missing(), accepted(), polled()], 0, 0, [R], 0],
      ['while the stage request is in flight', [missing(), accepted(), polled()], 1, 0, [R, S], 1],
      ['while the last status read is in flight', [found(), polled()], 1, 250, [R, G], 0],
      ['between status reads', [found(), running(), polled()], null, 250, [R, G], 0],
    ]) {
      authorityProvider.mockClear()
      let release
      const queued = replies.map((answer, index) => index === held
        ? () => new Promise((resolve) => { release = () => resolve(answer) })
        : answer)
      const f = fixture({ replies: queued })
      await flush()
      if (wait) await advance(wait)
      calls(after)
      const bytes = replace(f)
      await flush()
      const snap = snapshot(f)
      const writes = f.storage.setItem.mock.calls.length
      if (held !== null) await act(async () => { release() })
      await flush(); await advance(10_000)
      calls(after)
      expect(authorityProvider.mock.calls.length, label).toBe(minted)
      expect(f.storage.getItem(INFLIGHT_AUTHOR_KEY), label).toBe(bytes)
      expect(f.storage.setItem, label).toHaveBeenCalledTimes(writes)
      expect(snapshot(f), label).toEqual(snap)
      f.unmount()
    }
  })

  it('C8C-45 a recovery continues while the same request stays saved, and stops once nothing is saved', async () => {
    let release
    const hold = (answer) => () => new Promise((resolve) => { release = () => resolve(answer) })
    const same = fixture({ replies: [hold(missing()), accepted(), polled()] })
    await flush(); calls([R])
    // Another tab recovering the same request saves it again under the same key.
    same.storage.setItem(INFLIGHT_AUTHOR_KEY, JSON.stringify({ ...same.P, description: 'Count panels again' }))
    await act(async () => { release() })
    await flush(); await advance(250)
    calls([R, S, G]); expect(authorityProvider).toHaveBeenCalledTimes(1)
    successful(same)
    same.unmount()

    authorityProvider.mockClear()
    const gone = fixture({ replies: [hold(missing())] })
    await flush(); calls([R])
    gone.storage.removeItem(INFLIGHT_AUTHOR_KEY)
    const snap = snapshot(gone)
    const writes = gone.storage.setItem.mock.calls.length
    await act(async () => { release() })
    await flush(); await advance(10_000)
    calls([R]); expect(authorityProvider).not.toHaveBeenCalled()
    expect(gone.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBeNull()
    expect(gone.storage.setItem).toHaveBeenCalledTimes(writes)
    expect(snapshot(gone)).toEqual(snap)
    gone.unmount()
  })

  it('C8C-46 a recovery stops when the saved key differs from its own only by case or white space', async () => {
    // Another tab saves a request whose key differs from the one being recovered only by letter case or
    // surrounding white space. Those are different requests, so the recovery mints, sends and writes nothing.
    for (const key of ['SAVED-KEY', 'Saved-Key', 'saved-KEY', ' saved-key', 'saved-key ', 'saved-key\n', '\tsaved-key']) {
      authorityProvider.mockClear()
      let release
      const held = () => new Promise((resolve) => { release = () => resolve(missing()) })
      const f = fixture({ replies: [held, accepted(), polled()] })
      await flush(); calls([R])
      f.storage.setItem(INFLIGHT_AUTHOR_KEY, JSON.stringify(pointer(f.storage, { idempotency_key: key, description: 'Another tab request' })))
      const bytes = f.storage.getItem(INFLIGHT_AUTHOR_KEY)
      const snap = snapshot(f)
      const writes = f.storage.setItem.mock.calls.length
      await act(async () => { release() })
      await flush(); await advance(10_000)
      calls([R])
      expect(authorityProvider, JSON.stringify(key)).not.toHaveBeenCalled()
      expect(f.storage.getItem(INFLIGHT_AUTHOR_KEY), JSON.stringify(key)).toBe(bytes)
      expect(f.storage.setItem, JSON.stringify(key)).toHaveBeenCalledTimes(writes)
      expect(snapshot(f), JSON.stringify(key)).toEqual(snap)
      f.unmount()
    }
  })
})
