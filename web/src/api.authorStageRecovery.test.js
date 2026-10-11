import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { config, recoverAuthorStage } from './api.js'

const R = `${config.apiBase}/api/author/stage/recover`
const CONTRACT = 'leaf.customization-stage-recovery.v1'
const MESSAGE = 'Could not check the saved request. Try Resume again.'
const J = {
  contract: 'leaf.customization-stage-job.v1', status: 'queued',
  change_set_id: 'cs-recovered', poll_url: '/api/author/stages/cs-recovered', retry_after_ms: 250,
}
let fetchSpy
let writeSpy
let timerSpy

function response(status, body) {
  return { status, ok: status >= 200 && status < 300, json: vi.fn(async () => body) }
}

function found(job = J) {
  return response(200, { contract: CONTRACT, status: 'found', job })
}

async function rejectsRecovery(answer, options = {}) {
  fetchSpy.mockReset().mockResolvedValue(answer)
  let error
  try { await recoverAuthorStage('saved-key', options) } catch (cause) { error = cause }
  expect(error).toBeInstanceOf(Error)
  expect(error).toMatchObject({ message: MESSAGE, code: 'author_recovery_failed', authorRecovery: true, authorTerminal: false })
  for (const key of ['body', 'grantRequired', 'entitlementRequired', 'quotaExceeded']) expect(error).not.toHaveProperty(key)
  expect(fetchSpy.mock.calls.map(([url, init]) => [init?.method || 'GET', url])).toEqual([['POST', R]])
  expect(answer.json).toHaveBeenCalledTimes(1)
  expect(writeSpy).not.toHaveBeenCalled()
}

beforeEach(() => {
  localStorage.clear()
  fetchSpy = vi.fn()
  vi.stubGlobal('fetch', fetchSpy)
  writeSpy = vi.spyOn(Storage.prototype, 'setItem')
  timerSpy = vi.spyOn(globalThis, 'setTimeout')
})

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('author stage recovery transport', () => {
  it('C8C-01 recovery sends only the stored key with normal authentication', async () => {
    localStorage.setItem('leaf.jwt', 'account-token')
    localStorage.setItem('leaf.guest_session', 'guest-token')
    writeSpy.mockClear()
    // jsdom's Storage.setItem schedules one timer per write; only the call's own timers count.
    timerSpy.mockClear()
    const signal = new AbortController().signal
    const answer = found()
    fetchSpy.mockResolvedValue(answer)
    expect(await recoverAuthorStage('saved-key', { signal })).toEqual({ status: 'found', job: J })
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    expect(fetchSpy).toHaveBeenCalledWith(R, {
      method: 'POST', signal,
      headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': config.tenant, Authorization: 'Bearer account-token' },
      body: JSON.stringify({ idempotency_key: 'saved-key' }),
    })
    expect(answer.json).toHaveBeenCalledTimes(1)
    expect(writeSpy).not.toHaveBeenCalled()
    expect(timerSpy).not.toHaveBeenCalled()
  })

  it('C8C-02 only the contracted 404 means not found', async () => {
    fetchSpy.mockResolvedValue(response(404, { contract: CONTRACT, status: 'not_found' }))
    expect(await recoverAuthorStage('saved-key')).toEqual({ status: 'not_found' })
    expect(fetchSpy.mock.calls.map(([url, init]) => [init.method, url])).toEqual([['POST', R]])
    expect(writeSpy).not.toHaveBeenCalled()
    // Any other 404 is a failed check, never an absence that permits a new submission.
    const unreadable = response(404, null)
    unreadable.json.mockRejectedValue(new SyntaxError('private invalid JSON'))
    for (const answer of [
      unreadable, response(404, {}), response(404, { status: 'not_found' }),
      response(404, { contract: 'wrong', status: 'not_found' }),
      response(404, { contract: CONTRACT, status: 'found', job: J }),
      response(404, { contract: CONTRACT, status: 'not_found', error: { message: 'private' } }),
    ]) await rejectsRecovery(answer)
  })

  it('C8C-03 pending is a single transport result rather than a transport retry', async () => {
    fetchSpy.mockResolvedValue(response(202, { contract: CONTRACT, status: 'admission_pending', retry_after_ms: 1000 }))
    expect(await recoverAuthorStage('saved-key')).toEqual({ status: 'admission_pending', retry_after_ms: 1000 })
    expect(fetchSpy.mock.calls.map(([url, init]) => [init.method, url])).toEqual([['POST', R]])
    expect(timerSpy).not.toHaveBeenCalled()
    expect(writeSpy).not.toHaveBeenCalled()
  })

  it('C8C-04 recovery rejects wrong envelopes and HTTP status combinations', async () => {
    const envelope = (status, extra = {}) => ({ contract: CONTRACT, status, ...extra })
    const malformed = response(200, null)
    malformed.json.mockRejectedValue(new SyntaxError('private invalid JSON'))
    const answers = [
      malformed, ...[null, [], 1, 'found', {}, { contract: 'wrong', status: 'found', job: J }].map((body) => response(200, body)),
      response(200, envelope('not_found')), response(404, envelope('found', { job: J })),
      response(202, envelope('found', { job: J })), response(200, envelope('unknown')),
      response(202, envelope('admission_pending', { job: J })),
      response(404, envelope('not_found', { job: null })),
      response(200, envelope('found', { job: J, error: { message: 'private' } })),
      ...[-1, Infinity, NaN, '1000', null].map((retry_after_ms) => response(202, envelope('admission_pending', { retry_after_ms }))),
    ]
    for (const answer of answers) await rejectsRecovery(answer)
  })

  it('C8C-05 recovery validates the found job identity before returning it', async () => {
    const fixtures = [
      [undefined], [[]], [{ ...J, contract: 'wrong' }], [{ ...J, status: 'accepted' }],
      [{ ...J, change_set_id: '' }], [{ ...J, change_set_id: '   ' }], [{ ...J, change_set_id: 4 }],
      [J, { changeSetId: 'other-id' }], [J, { targetToolName: 'other-tool' }],
      [{ ...J, receipt: { change_set_id: 'other-id' } }],
      [{ ...J, result: { receipt: { change_set_id: 'other-id' } } }],
      [{ ...J, staged: { receipt: { change_set_id: 'other-id' } } }],
      [{ ...J, receipt: [] }], [{ ...J, receipt: null }],
    ]
    for (const [job, options] of fixtures) {
      await rejectsRecovery(response(200, { contract: CONTRACT, status: 'found', job }), options)
    }
  })

  it('C8C-06 recovery rejects unsafe or mismatched poll addresses', async () => {
    for (const poll_url of [undefined, '', 3, 'https://foreign.invalid/api/author/stages/cs-recovered',
      '/api/author/jobs/cs-recovered', '/api/author/stages/other-id',
      '/api/author/stages/cs-recovered?x=1', '/api/author/stages/cs-recovered#fragment']) {
      await rejectsRecovery(found({ ...J, poll_url }))
    }
  })

  it('C8C-07 recovery accepts queued running staged and failed jobs as found', async () => {
    for (const status of ['queued', 'running', 'staged', 'failed']) {
      const job = { ...J, status, ...(status === 'staged' ? { result: { receipt: { change_set_id: J.change_set_id } } } : {}) }
      fetchSpy.mockReset().mockResolvedValue(found(job))
      expect(await recoverAuthorStage(' saved-key ', { changeSetId: J.change_set_id })).toEqual({ status: 'found', job })
      expect(fetchSpy.mock.calls.map(([url, init]) => [init.method, url])).toEqual([['POST', R]])
      expect(JSON.parse(fetchSpy.mock.calls[0][1].body)).toEqual({ idempotency_key: ' saved-key ' })
    }
    expect(writeSpy).not.toHaveBeenCalled()
    expect(timerSpy).not.toHaveBeenCalled()
  })

  it('C8C-08 recovery preserves cancellation and rejects invalid keys before fetch', async () => {
    const controller = new AbortController()
    const cancellation = new DOMException('Cancelled', 'AbortError')
    controller.abort(cancellation)
    await expect(recoverAuthorStage('saved-key', { signal: controller.signal })).rejects.toBe(cancellation)
    for (const key of [null, '', '   ', 'x'.repeat(201)]) {
      await expect(recoverAuthorStage(key)).rejects.toMatchObject({ message: MESSAGE, authorRecovery: true, authorTerminal: false })
    }
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(timerSpy).not.toHaveBeenCalled()
    expect(writeSpy).not.toHaveBeenCalled()
  })
})
