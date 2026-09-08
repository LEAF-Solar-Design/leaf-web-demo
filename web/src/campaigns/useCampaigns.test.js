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
  api.listCapabilities.mockResolvedValue({ capabilities: [] })
  api.listEnrollments.mockResolvedValue({ enrollment: { enrollments: [], allowed_machines: ['VM-C'] } })
  api.requestEnrollment.mockResolvedValue({ enrollment: { enrollment_id: Q, state: 'pending' } })
  api.enableEnrollment.mockResolvedValue({ enrollment: { enrollment_id: Q, state: 'enabled' } })
  api.revokeEnrollment.mockResolvedValue({ enrollment: { enrollment_id: Q, state: 'revoked' } })
  api.getExecution.mockResolvedValue({ execution: { tasks: [], questions: [], receipts: [], events: [] } })
  api.submitCampaign.mockResolvedValue({ campaign: row })
  api.askQuestion.mockResolvedValue({ question: open })
  api.answerQuestion.mockResolvedValue({ answer: receipt })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })

async function ready() {
  const hook = renderHook(() => useCampaigns(P, { enabled: true }))
  await waitFor(() => expect(hook.result.current.status).toBe('ready'))
  return hook
}

it('registers native release in the selected campaign and reloads setup readiness without execution', async () => {
  const native = { enrollment_id: Q, machine_id: 'VM-C', capability: 'campaign.native-release',
    state: 'pending', readiness: 'setup_required', readiness_message: 'The release executor is not connected.',
    capability_link: { capability: 'campaign.native-release', state: 'pending_link' } }
  const hook = await ready()
  api.requestEnrollment.mockResolvedValue({ enrollment: native })
  api.listEnrollments.mockResolvedValue({ enrollment: { enrollments: [native], allowed_machines: ['VM-C'] } })
  await act(async () => { await hook.result.current.enroll('VM-C', 'campaign.native-release') })
  expect(api.requestEnrollment).toHaveBeenCalledExactlyOnceWith(P, C, 'VM-C', 'campaign.native-release')
  expect(hook.result.current.enrollments).toEqual([native])
  expect(api.enableEnrollment).not.toHaveBeenCalled()
  expect(api.bindPublication).not.toHaveBeenCalled()
  expect(api.invokeCapability).not.toHaveBeenCalled()
})

describe('project campaign hook', () => {
  const digest = 'a'.repeat(64)
  const host = { enrollment_id: Q, machine_id: 'Host', state: 'enabled', completed_uses: 0,
    capability_link: { state: 'published', effective_catalog_digest: digest }, invocations: [] }
  const job = { job_id: D, status: 'running', progress: 'Waiting for host' }
  function capabilityFixture() {
    api.listEnrollments.mockResolvedValue({ enrollment: { enrollments: [host], allowed_machines: ['Host'] } })
    api.listCapabilities.mockResolvedValue({ capabilities: [{ change_set_id: 'listed', label: 'Host capability' }] })
    api.invokeCapability.mockResolvedValue({ invocation: job })
    api.bindPublication.mockResolvedValue({ enrollment: host })
  }

  const submissionKey = (project = P, campaign = C, enrollment = Q) => `leaf.campaign.invocation:${project}:${campaign}:${enrollment}`
  const drift = () => Object.assign(new Error('Catalog mismatch'), { status: 409, code: 'catalog_drift', retryable: false })
  const guidance = 'The published tool changed. No job was submitted. Review its publication binding before using it again.'

  it('clears only a definitive catalog_drift pending submission and reloads current choices', async () => {
    capabilityFixture()
    const other = { idempotencyKey: 'other-key', effectiveCatalogDigest: digest }
    sessionStorage.setItem(submissionKey(P, C, B), JSON.stringify(other))
    sessionStorage.setItem(submissionKey(B), JSON.stringify(other))
    const hosts = [{ ...host, completed_uses: 2 }, { ...host, enrollment_id: B }]
    api.listEnrollments.mockResolvedValue({ enrollment: { enrollments: hosts, allowed_machines: ['Host'] } })
    const hook = await ready()
    await act(async () => { await hook.result.current.invokeCapability(Q) })
    expect(hook.result.current.invocationResults[Q]).toEqual(job)
    const capabilityLoads = api.listCapabilities.mock.calls.length
    const enrollmentLoads = api.listEnrollments.mock.calls.length
    const failure = drift()
    api.invokeCapability.mockImplementationOnce((_p, _c, _e, submission) => {
      expect(JSON.parse(sessionStorage.getItem(submissionKey()))).toEqual(submission)
      return Promise.reject(failure)
    })
    const nextDigest = 'b'.repeat(64)
    const choices = [{ change_set_id: 'current', label: 'Current tool' }]
    api.listCapabilities.mockResolvedValue({ capabilities: choices })
    api.listEnrollments.mockResolvedValue({ enrollment: { enrollments: [
      { ...hosts[0], capability_link: { ...host.capability_link, effective_catalog_digest: nextDigest } }, hosts[1],
    ], allowed_machines: ['Host'] } })
    await act(async () => { await expect(hook.result.current.invokeCapability(Q)).rejects.toMatchObject({
      message: guidance, status: 409, code: 'catalog_drift', retryable: false,
    }) })
    const rejected = api.invokeCapability.mock.calls[1][3]
    expect(sessionStorage.getItem(submissionKey())).toBeNull()
    expect(JSON.parse(sessionStorage.getItem(submissionKey(P, C, B)))).toEqual(other)
    expect(JSON.parse(sessionStorage.getItem(submissionKey(B)))).toEqual(other)
    expect(hook.result.current.submissions).toEqual({ [B]: other })
    expect(hook.result.current.invocationResults[Q]).toEqual(job)
    expect(hook.result.current.enrollments[0].completed_uses).toBe(2)
    expect(hook.result.current.capabilities).toEqual(choices)
    expect(hook.result.current.error.message).toBe(guidance)
    expect(api.listCapabilities).toHaveBeenCalledTimes(capabilityLoads + 1)
    expect(api.listEnrollments).toHaveBeenCalledTimes(enrollmentLoads + 1)
    expect(api.invokeCapability).toHaveBeenCalledTimes(2)
    expect(api.bindPublication).not.toHaveBeenCalled()
    expect(api.requestEnrollment).not.toHaveBeenCalled()
    expect(api.revokeEnrollment).not.toHaveBeenCalled()
    await act(async () => { await hook.result.current.invokeCapability(Q) })
    expect(api.invokeCapability.mock.calls[2][3]).toEqual({
      idempotencyKey: expect.any(String), effectiveCatalogDigest: nextDigest,
    })
    expect(api.invokeCapability.mock.calls[2][3].idempotencyKey).not.toBe(rejected.idempotencyKey)
  })

  it.each([
    [0, undefined, true], [503, 'invocation_unknown', true],
    [409, 'idempotency_conflict', false], [409, 'invocation_pending', true],
    [409, undefined, false], [503, 'catalog_drift', false],
    [undefined, 'catalog_drift', false], ['409', 'catalog_drift', false],
    [400, 'catalog_drift', false],
  ])('retains unknown outcomes across remount for status %s and code %s', async (status, code, retryable) => {
    capabilityFixture()
    const failure = Object.assign(new Error('catalog_drift'), { status, code, retryable })
    api.invokeCapability.mockRejectedValue(failure)
    const hook = await ready()
    await act(async () => { await expect(hook.result.current.invokeCapability(Q)).rejects.toBe(failure) })
    const submission = api.invokeCapability.mock.calls[0][3]
    expect(JSON.parse(sessionStorage.getItem(submissionKey()))).toEqual(submission)
    expect(api.listCapabilities).toHaveBeenCalledTimes(1)
    hook.unmount()
    api.listEnrollments.mockResolvedValue({ enrollment: { enrollments: [{ ...host,
      capability_link: { ...host.capability_link, effective_catalog_digest: 'b'.repeat(64) },
    }], allowed_machines: ['Host'] } })
    const reconnected = await ready()
    await act(async () => { await reconnected.result.current.refetch() })
    expect(reconnected.result.current.submissions[Q]).toEqual(submission)
    await act(async () => { await expect(reconnected.result.current.invokeCapability(Q)).rejects.toBe(failure) })
    expect(api.invokeCapability.mock.calls[1][3]).toEqual(submission)
    expect(submission.effectiveCatalogDigest).toBe(digest)
    expect(JSON.parse(sessionStorage.getItem(submissionKey()))).toEqual(submission)
  })

  it.each(['campaigns', 'capabilities', 'enrollments'])('keeps catalog rejection truthful when refreshing %s fails', async resource => {
    capabilityFixture()
    const hook = await ready()
    const failure = new Error('Choices unavailable')
    const method = { campaigns: 'listCampaigns', capabilities: 'listCapabilities', enrollments: 'listEnrollments' }[resource]
    api[method].mockRejectedValue(failure)
    api.invokeCapability.mockRejectedValue(drift())
    await act(async () => { await expect(hook.result.current.invokeCapability(Q)).rejects.toMatchObject({
      status: 409, code: 'catalog_drift', message: `${guidance} Current choices could not be refreshed.`,
    }) })
    expect(sessionStorage.getItem(submissionKey())).toBeNull()
    expect(hook.result.current.submissions).toEqual({})
    const field = { campaigns: 'error', capabilities: 'capabilityError', enrollments: 'enrollmentError' }[resource]
    expect(hook.result.current[field]).toBe(failure)
    expect(api.invokeCapability).toHaveBeenCalledTimes(1)
    expect(api.bindPublication).not.toHaveBeenCalled()
  })

  it('does not revive rejected storage within the view when removal fails', async () => {
    capabilityFixture()
    const storage = sessionStorage
    vi.stubGlobal('sessionStorage', {
      getItem: key => storage.getItem(key), setItem: (key, value) => storage.setItem(key, value),
      removeItem: () => { throw new Error('Removal disabled') },
    })
    const hook = await ready()
    api.invokeCapability.mockRejectedValueOnce(drift())
    await act(async () => { await expect(hook.result.current.invokeCapability(Q)).rejects.toThrow(guidance) })
    const rejected = api.invokeCapability.mock.calls[0][3]
    expect(JSON.parse(storage.getItem(submissionKey()))).toEqual(rejected)
    expect(hook.result.current.submissions).toEqual({})
    expect(hook.result.current.recoveryUnavailable).toBe(true)
    await act(async () => { await hook.result.current.refetch() })
    expect(hook.result.current.submissions).toEqual({})
    await act(async () => { await hook.result.current.invokeCapability(Q) })
    expect(api.invokeCapability.mock.calls[1][3].idempotencyKey).not.toBe(rejected.idempotencyKey)
  })

  it.each(['project', 'campaign'])('clears only originating storage on delayed drift after a %s switch', async kind => {
    capabilityFixture()
    const hook = renderHook(({ project }) => useCampaigns(project), { initialProps: { project: P } })
    await waitFor(() => expect(hook.result.current.status).toBe('ready'))
    const pending = deferred()
    api.invokeCapability.mockReturnValueOnce(pending.promise)
    let first
    act(() => { first = hook.result.current.invokeCapability(Q) })
    const nextKey = kind === 'project' ? submissionKey(B) : submissionKey(P, D)
    const other = { idempotencyKey: 'new-view-key', effectiveCatalogDigest: 'b'.repeat(64) }
    sessionStorage.setItem(nextKey, JSON.stringify(other))
    if (kind === 'project') hook.rerender({ project: B })
    else {
      api.listCampaigns.mockResolvedValue({ campaigns: [row, { ...row, campaign_id: D }] })
      await act(async () => { await hook.result.current.select(D) })
    }
    await waitFor(() => expect(hook.result.current.submissions[Q]).toEqual(other))
    const loads = api.listCampaigns.mock.calls.length
    await act(async () => { pending.reject(drift()); await expect(first).rejects.toThrow(guidance) })
    expect(sessionStorage.getItem(submissionKey())).toBeNull()
    expect(JSON.parse(sessionStorage.getItem(nextKey))).toEqual(other)
    expect(hook.result.current.submissions[Q]).toEqual(other)
    expect(hook.result.current.error).toBeNull()
    expect(hook.result.current.selectedId).toBe(kind === 'project' ? C : D)
    expect(api.listCampaigns).toHaveBeenCalledTimes(loads)
    expect(api.invokeCapability).toHaveBeenCalledTimes(1)
  })

  it('preserves a newer stored submission when a rejected response arrives late', async () => {
    capabilityFixture()
    const hook = await ready()
    const pending = deferred()
    api.invokeCapability.mockReturnValueOnce(pending.promise)
    let first
    act(() => { first = hook.result.current.invokeCapability(Q) })
    const newer = { idempotencyKey: 'newer-key', effectiveCatalogDigest: 'b'.repeat(64) }
    sessionStorage.setItem(submissionKey(), JSON.stringify(newer))
    await act(async () => { pending.reject(drift()); await expect(first).rejects.toThrow(guidance) })
    expect(JSON.parse(sessionStorage.getItem(submissionKey()))).toEqual(newer)
  })

  it('does not clear a newer in-memory submission after leaving and returning to a campaign', async () => {
    capabilityFixture()
    api.listCampaigns.mockResolvedValue({ campaigns: [row, { ...row, campaign_id: D }] })
    const hook = await ready()
    const pending = deferred()
    api.invokeCapability.mockReturnValueOnce(pending.promise)
    let first
    act(() => { first = hook.result.current.invokeCapability(Q) })
    await act(async () => { await hook.result.current.select(D) })
    await act(async () => { await hook.result.current.select(C) })
    await act(async () => { await hook.result.current.invokeCapability(Q) })
    api.invokeCapability.mockRejectedValueOnce(new Error('Response lost'))
    await act(async () => { await expect(hook.result.current.invokeCapability(Q)).rejects.toThrow('Response lost') })
    const newer = api.invokeCapability.mock.calls[2][3]
    expect(newer.idempotencyKey).not.toBe(api.invokeCapability.mock.calls[0][3].idempotencyKey)
    const loads = api.listCampaigns.mock.calls.length
    await act(async () => { pending.reject(drift()); await expect(first).rejects.toThrow(guidance) })
    expect(hook.result.current.submissions[Q]).toEqual(newer)
    expect(JSON.parse(sessionStorage.getItem(submissionKey()))).toEqual(newer)
    expect(hook.result.current.invocationResults[Q]).toEqual(job)
    expect(api.listCampaigns).toHaveBeenCalledTimes(loads)
  })

  it.each([undefined, 'not-a-uuid'])('retains a submission across remount after job identity %s', async jobId => {
    capabilityFixture()
    api.invokeCapability.mockResolvedValue({ invocation: { job_id: jobId, status: 'running' } })
    const hook = await ready()
    await act(async () => { await expect(hook.result.current.invokeCapability(Q)).rejects.toThrow('Submission outcome unknown') })
    const submission = api.invokeCapability.mock.calls[0][3]
    hook.unmount()
    api.listEnrollments.mockResolvedValue({ enrollment: { enrollments: [{ ...host,
      capability_link: { ...host.capability_link, effective_catalog_digest: 'b'.repeat(64) },
    }], allowed_machines: ['Host'] } })
    const reconnected = await ready()
    await act(async () => { await expect(reconnected.result.current.invokeCapability(Q)).rejects.toThrow('Submission outcome unknown') })
    expect(api.invokeCapability.mock.calls[1][3]).toEqual(submission)
    expect(JSON.parse(sessionStorage.getItem(submissionKey()))).toEqual(submission)
    expect(reconnected.result.current.submissions[Q]).toEqual(submission)
  })

  it('binds only server-listed choices and reloads candidates', async () => {
    capabilityFixture()
    const hook = await ready()
    await act(async () => { await hook.result.current.bindPublication(Q, 'typed-id') })
    expect(api.bindPublication).not.toHaveBeenCalled()
    await act(async () => { await hook.result.current.bindPublication(Q, 'listed') })
    expect(api.bindPublication).toHaveBeenCalledExactlyOnceWith(P, C, Q, 'listed')
    expect(api.listCapabilities).toHaveBeenCalledTimes(2)
  })

  it('persists before send, locks duplicate clicks and gives an intentional second use a new key', async () => {
    capabilityFixture()
    const hook = await ready()
    const pending = deferred()
    api.invokeCapability.mockImplementation((_p, _c, _e, submission) => {
      expect(JSON.parse(sessionStorage.getItem(`leaf.campaign.invocation:${P}:${C}:${Q}`))).toEqual(submission)
      return pending.promise
    })
    let first
    act(() => { first = hook.result.current.invokeCapability(Q); hook.result.current.invokeCapability(Q) })
    expect(api.invokeCapability).toHaveBeenCalledTimes(1)
    const submission = api.invokeCapability.mock.calls[0][3]
    expect(submission.effectiveCatalogDigest).toBe(digest)
    await act(async () => { pending.resolve({ invocation: job }); await first })
    expect(sessionStorage.getItem(`leaf.campaign.invocation:${P}:${C}:${Q}`)).toBeNull()
    expect(hook.result.current.submissions).toEqual({})
    api.invokeCapability.mockResolvedValue({ invocation: { ...job, job_id: B } })
    await act(async () => { await hook.result.current.invokeCapability(Q) })
    expect(api.invokeCapability.mock.calls[1][3].idempotencyKey).not.toBe(submission.idempotencyKey)
  })

  it('recovers response loss across remount with the same key and original digest, never on mount or refresh', async () => {
    capabilityFixture()
    const hook = await ready()
    api.invokeCapability.mockRejectedValue(new Error('Response lost'))
    await act(async () => { await expect(hook.result.current.invokeCapability(Q)).rejects.toThrow('Response lost') })
    const submission = api.invokeCapability.mock.calls[0][3]
    await act(async () => { await expect(hook.result.current.invokeCapability(Q)).rejects.toThrow() })
    expect(api.invokeCapability.mock.calls[1][3]).toEqual(submission)
    hook.unmount()
    api.listEnrollments.mockResolvedValue({ enrollment: { enrollments: [{ ...host,
      capability_link: { ...host.capability_link, effective_catalog_digest: 'b'.repeat(64) } }], allowed_machines: ['Host'] } })
    const reconnected = await ready()
    await act(async () => { await reconnected.result.current.refetch() })
    expect(api.invokeCapability).toHaveBeenCalledTimes(2)
    expect(reconnected.result.current.submissions[Q]).toEqual(submission)
    api.invokeCapability.mockResolvedValue({ invocation: job })
    await act(async () => { await reconnected.result.current.invokeCapability(Q) })
    expect(api.invokeCapability.mock.calls[2][3]).toEqual(submission)
    expect(reconnected.result.current.submissions).toEqual({})
  })

  it('keeps an in-memory retry stable and discloses disabled storage', async () => {
    capabilityFixture()
    vi.stubGlobal('sessionStorage', { getItem: () => { throw new Error('Disabled') },
      setItem: () => { throw new Error('Disabled') }, removeItem: () => { throw new Error('Disabled') } })
    const hook = await ready()
    api.invokeCapability.mockRejectedValue(new Error('Response lost'))
    await act(async () => { await expect(hook.result.current.invokeCapability(Q)).rejects.toThrow() })
    await act(async () => { await expect(hook.result.current.invokeCapability(Q)).rejects.toThrow() })
    expect(api.invokeCapability.mock.calls[1][3]).toEqual(api.invokeCapability.mock.calls[0][3])
    expect(hook.result.current.recoveryUnavailable).toBe(true)
  })

  it('retains the submission when a response has no canonical job identity', async () => {
    capabilityFixture()
    const hook = await ready()
    api.invokeCapability.mockResolvedValue({ invocation: { status: 'running' } })
    await act(async () => { await expect(hook.result.current.invokeCapability(Q)).rejects.toThrow('Submission outcome unknown') })
    const submission = api.invokeCapability.mock.calls[0][3]
    expect(hook.result.current.submissions[Q]).toEqual(submission)
    api.invokeCapability.mockResolvedValue({ invocation: job })
    await act(async () => { await hook.result.current.invokeCapability(Q) })
    expect(api.invokeCapability.mock.calls[1][3]).toEqual(submission)
  })

  it('keeps recovered job identity visible even when the following refresh fails', async () => {
    capabilityFixture()
    const hook = await ready()
    api.listCampaigns.mockRejectedValue(new Error('Readback unavailable'))
    await act(async () => { await hook.result.current.invokeCapability(Q) })
    expect(hook.result.current.submissions).toEqual({})
    expect(hook.result.current.invocationResults[Q]).toEqual(job)
    expect(sessionStorage.getItem(`leaf.campaign.invocation:${P}:${C}:${Q}`)).toBeNull()
  })

  it.each(['project', 'campaign'])('ignores a late invocation response after a %s switch', async kind => {
    capabilityFixture()
    const hook = renderHook(({ project }) => useCampaigns(project), { initialProps: { project: P } })
    await waitFor(() => expect(hook.result.current.status).toBe('ready'))
    const pending = deferred()
    api.invokeCapability.mockReturnValue(pending.promise)
    let first
    act(() => { first = hook.result.current.invokeCapability(Q) })
    if (kind === 'project') hook.rerender({ project: B })
    else {
      api.listCampaigns.mockResolvedValue({ campaigns: [row, { ...row, campaign_id: D }] })
      await act(async () => { await hook.result.current.select(D) })
    }
    await waitFor(() => expect(hook.result.current.status).toBe('ready'))
    await act(async () => { pending.resolve({ invocation: job }); await first })
    expect(hook.result.current.invocationResults).toEqual({})
    expect(hook.result.current.submissions).toEqual({})
  })

  it('drops stale published choices after a campaign switch', async () => {
    capabilityFixture()
    const hook = await ready()
    const pending = deferred()
    api.listCapabilities.mockReturnValueOnce(pending.promise)
    let refresh
    act(() => { refresh = hook.result.current.refetch() })
    await waitFor(() => expect(api.listCapabilities).toHaveBeenCalledTimes(2))
    api.listCampaigns.mockResolvedValue({ campaigns: [row, { ...row, campaign_id: D }] })
    api.listCapabilities.mockResolvedValue({ capabilities: [] })
    await act(async () => { await hook.result.current.select(D) })
    await act(async () => { pending.resolve({ capabilities: [{ change_set_id: 'old' }] }); await refresh })
    expect(hook.result.current.capabilities).toEqual([])
  })

  it('loads configured machines and serializes enrollment mutations', async () => {
    const hook = await ready()
    expect(hook.result.current.allowedMachines).toEqual(['VM-C'])
    const pending = deferred()
    api.requestEnrollment.mockReturnValue(pending.promise)
    let first
    act(() => { first = hook.result.current.enroll('VM-C'); hook.result.current.enroll('VM-C') })
    expect(api.requestEnrollment).toHaveBeenCalledExactlyOnceWith(P, C, 'VM-C')
    await act(async () => { pending.resolve({ enrollment: { enrollment_id: Q } }); await first })
    await act(async () => { await hook.result.current.enableEnrollment(Q) })
    expect(api.enableEnrollment).toHaveBeenCalledExactlyOnceWith(P, C, Q)
    await act(async () => { await hook.result.current.revokeEnrollment(Q) })
    expect(api.revokeEnrollment).toHaveBeenCalledExactlyOnceWith(P, C, Q)
  })

  it('keeps campaign and questions available when enrollment loading fails', async () => {
    api.listEnrollments.mockRejectedValue(new Error('Hosts unavailable'))
    const hook = await ready()
    expect(hook.result.current.questions).toEqual([open])
    expect(hook.result.current.enrollmentError.message).toBe('Hosts unavailable')
    expect(hook.result.current.allowedMachines).toEqual([])
  })
  it('reads execution after campaign details are ready and preserves an empty snapshot', async () => {
    const pending = deferred()
    api.getExecution.mockReturnValue(pending.promise)
    const hook = await ready()
    expect(api.getExecution).toHaveBeenCalledExactlyOnceWith(P, C)
    expect(hook.result.current.selected).toEqual(row)
    expect(hook.result.current.questions).toEqual([open])
    expect(hook.result.current.executionLoading).toBe(true)
    const execution = { tasks: [], questions: [], receipts: [], events: [] }
    await act(async () => { pending.resolve({ execution }); await pending.promise })
    expect(hook.result.current.execution).toEqual(execution)
    expect(hook.result.current.executionLoading).toBe(false)
    expect(hook.result.current.executionError).toBeNull()
  })

  it('clears execution synchronously on select and drops a delayed old snapshot', async () => {
    const initial = { tasks: [{ title: 'Original' }], questions: [], receipts: [], events: [] }
    api.getExecution.mockResolvedValue({ execution: initial })
    const hook = await ready()
    await waitFor(() => expect(hook.result.current.execution).toEqual(initial))
    const old = deferred()
    api.getExecution.mockReturnValueOnce(old.promise)
    let refreshing
    act(() => { refreshing = hook.result.current.refetch() })
    await waitFor(() => expect(api.getExecution).toHaveBeenCalledTimes(2))
    api.listCampaigns.mockResolvedValue({ campaigns: [row, { ...row, campaign_id: D }] })
    const next = { tasks: [{ title: 'New selection' }], questions: [], receipts: [], events: [] }
    api.getExecution.mockResolvedValue({ execution: next })
    let selecting
    act(() => { selecting = hook.result.current.select(D) })
    expect(hook.result.current.execution).toBeNull()
    expect(hook.result.current.executionError).toBeNull()
    await act(async () => { await selecting })
    expect(api.getExecution).toHaveBeenLastCalledWith(P, D)
    await act(async () => { old.resolve({ execution: initial }); await refreshing })
    expect(hook.result.current.execution).toEqual(next)
  })

  it('resets execution on project change and drops the prior project response', async () => {
    const old = deferred()
    api.getExecution.mockImplementation(project => project === P ? old.promise : Promise.resolve({
      execution: { tasks: [], questions: [], receipts: [], events: [] },
    }))
    const hook = renderHook(({ project }) => useCampaigns(project), { initialProps: { project: P } })
    await waitFor(() => expect(api.getExecution).toHaveBeenCalledWith(P, C))
    hook.rerender({ project: B })
    expect(hook.result.current.execution).toBeNull()
    await waitFor(() => expect(hook.result.current.execution).toEqual({ tasks: [], questions: [], receipts: [], events: [] }))
    await act(async () => { old.resolve({ execution: { tasks: [{ title: 'Old project' }] } }); await old.promise })
    expect(hook.result.current.execution.tasks).toEqual([])
  })

  it('keeps the snapshot, campaigns and questions when execution refresh fails', async () => {
    const execution = { tasks: [{ title: 'Recorded task' }], questions: [], receipts: [], events: [] }
    api.getExecution.mockResolvedValue({ execution })
    const hook = await ready()
    await waitFor(() => expect(hook.result.current.execution).toEqual(execution))
    const failure = new Error('Execution is unavailable')
    api.getExecution.mockRejectedValue(failure)
    let result
    await act(async () => { result = await hook.result.current.refetch() })
    expect(result).toEqual({ campaigns: [row], selected: row, questions: [open] })
    expect(hook.result.current.execution).toEqual(execution)
    expect(hook.result.current.executionError).toBe(failure)
    expect(hook.result.current.executionLoading).toBe(false)
    expect(hook.result.current.error).toBeNull()
    expect(hook.result.current.campaigns).toEqual([row])
    expect(hook.result.current.questions).toEqual([open])
    api.getExecution.mockResolvedValue({ execution })
    await act(async () => { await hook.result.current.refetch() })
    expect(hook.result.current.executionError).toBeNull()
    api.getExecution.mockRejectedValue(failure)
    await act(async () => { await hook.result.current.refetch() })
    expect(hook.result.current.executionError).toBe(failure)
    api.listCampaigns.mockReturnValue(new Promise(() => {}))
    act(() => { hook.result.current.select(D) })
    expect(hook.result.current.execution).toBeNull()
    expect(hook.result.current.executionError).toBeNull()
  })

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

  it('projects a real catalog drift envelope into numeric status and code', async () => {
    const body = { error: { error_code: 'catalog_drift', retryable: false, message: 'The catalog changed.' } }
    fetcher.mockResolvedValue({ ok: false, status: 409, json: async () => body })
    await expect(client.invokeCapability(P, C, Q, {
      idempotencyKey: 'persisted-key', effectiveCatalogDigest: 'a'.repeat(64),
    })).rejects.toMatchObject({ status: 409, code: 'catalog_drift', retryable: false, body })
    expect(fetcher).toHaveBeenCalledExactlyOnceWith(
      `https://campaign.test/api/campaigns/${C}/enrollments/${Q}/invoke`,
      expect.objectContaining({
        headers: expect.objectContaining({ 'Idempotency-Key': 'persisted-key' }),
        body: JSON.stringify({ project_id: P, effective_catalog_digest: 'a'.repeat(64) }),
      }),
    )
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

  it('sends the other project-scoped wires without dispatch options', async () => {
    await client.getCampaign(P, C)
    await client.askQuestion(P, C, { questionKey: 'follow-up.1', prompt: 'Why?' })
    await client.listQuestions(P, C)
    await client.answerQuestion(P, C, Q, 'Use PDF.')
    await client.getExecution(P, C)
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      `https://campaign.test/api/campaigns/${C}?project_id=${P}`,
      `https://campaign.test/api/campaigns/${C}/questions`,
      `https://campaign.test/api/campaigns/${C}/questions?project_id=${P}`,
      `https://campaign.test/api/campaigns/${C}/questions/${Q}/answer`,
      `https://campaign.test/api/campaigns/${C}/execution?project_id=${P}&limit=50`,
    ])
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ project_id: P, question_key: 'follow-up.1', prompt: 'Why?' })
    expect(JSON.parse(fetcher.mock.calls[3][1].body)).toEqual({ project_id: P, answer: 'Use PDF.' })
  })

  it('refuses missing bearer before fetch', async () => {
    localStorage.removeItem('leaf.jwt')
    await expect(client.listCampaigns(P)).rejects.toMatchObject({ status: 0, message: 'Sign in to submit a campaign.' })
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('sends enrollment actions without subject or publication fields', async () => {
    await client.listEnrollments(P, C)
    await client.requestEnrollment(P, C, 'VM-C')
    await client.enableEnrollment(P, C, Q)
    await client.revokeEnrollment(P, C, Q)
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      `https://campaign.test/api/campaigns/${C}/enrollments?project_id=${P}`,
      `https://campaign.test/api/campaigns/${C}/enrollments`,
      `https://campaign.test/api/campaigns/${C}/enrollments/${Q}/enable`,
      `https://campaign.test/api/campaigns/${C}/enrollments/${Q}/revoke`,
    ])
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ project_id: P, machine_id: 'VM-C' })
    expect(JSON.parse(fetcher.mock.calls[2][1].body)).toEqual({ project_id: P })
  })

  it.each([[999, 200], [0, 1], [-20, 1], [2.9, 2], ['bad', 50]])('clamps execution limit %s to %s', async (limit, count) => {
    await client.getExecution(P, C, limit)
    expect(fetcher.mock.calls[0][0]).toBe(`https://campaign.test/api/campaigns/${C}/execution?project_id=${P}&limit=${count}`)
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
