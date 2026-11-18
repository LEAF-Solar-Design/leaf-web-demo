import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { config, recoverAuthorStageById, retryAuthorStage } from './api.js'
import useAuthorStageController from './controllers/useAuthorStageController.js'
import AuthorPanel from './components/AuthorPanel.jsx'

const ID = '13a3d502-a207-4be6-8507-60a58ff75e04'
const OTHER = '2409742d-d397-4927-b9bf-4a8e22d5bb5e'
const job = { contract: 'leaf.customization-stage-job.v1', status: 'failed',
  change_set_id: ID, poll_url: `/api/author/stages/${ID}`, attempt: 1, version: 2,
  error: { reason_code: 'author_job_failed', retryable: true } }
const found = (value = job) => ({ status: 'found', job: value })
const response = (value) => ({ status: 200, ok: true,
  json: async () => ({ contract: 'leaf.customization-stage-recovery.v1', ...value }) })

afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('reads a real-format existing ID with one recovery lookup and no author submission or storage write', async () => {
  const fetch = vi.fn().mockResolvedValue(response(found()))
  vi.stubGlobal('fetch', fetch)
  const write = vi.spyOn(Storage.prototype, 'setItem')
  expect(await recoverAuthorStageById(ID)).toEqual(found())
  expect(fetch).toHaveBeenCalledTimes(1)
  const [url, init] = fetch.mock.calls[0]
  expect(url).toBe(`${config.apiBase}/api/author/stage/recover`)
  expect(JSON.parse(init.body)).toEqual({ change_set_id: ID })
  expect(init.headers).not.toHaveProperty('X-Authority-Turn-Id')
  expect(write).not.toHaveBeenCalled()
})

it.each([
  { ...job, change_set_id: OTHER },
  { ...job, poll_url: 'https://foreign.invalid/api/author/stages/' + ID },
  { ...job, receipt: { change_set_id: OTHER } },
])('refuses a mismatched job, poll address or receipt', async (value) => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(found(value))))
  await expect(recoverAuthorStageById(ID)).rejects.toMatchObject({ code: 'author_recovery_failed' })
})

it('rejects an invalid ID before transport', async () => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  await expect(recoverAuthorStageById('not-a-request')).rejects.toBeInstanceOf(Error)
  expect(fetch).not.toHaveBeenCalled()
})

it('only an explicit retry sends CAS fields and fresh authority for the same ID', async () => {
  const fetch = vi.fn().mockResolvedValue(response(found({ ...job, status: 'queued' })))
  vi.stubGlobal('fetch', fetch)
  const authority = { sessionId: 'new-session', turnId: 'new-turn' }
  await retryAuthorStage(job, authority)
  const [, init] = fetch.mock.calls[0]
  expect(JSON.parse(init.body)).toEqual({ change_set_id: ID, retry: true, expected_version: 2, expected_attempt: 1 })
  expect(init.headers['X-Authority-Turn-Id']).toBe('new-turn')
  expect(init.headers['X-Authority-Session-Id']).toBe('new-session')
})

it.each([{ ...job, status: 'staged' }, { ...job, error: { retryable: false } }, { ...job, version: undefined }])(
  'refuses retry transport for an ineligible job', async (value) => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    await expect(retryAuthorStage(value, { sessionId: 'new', turnId: 'new' })).rejects.toBeInstanceOf(Error)
    expect(fetch).not.toHaveBeenCalled()
  })

describe('fresh-context lookup controller', () => {
  it('coalesces explicit retry clicks, polls the same ID and returns its staged receipt without a new pointer', async () => {
    const read = vi.fn().mockResolvedValue(found())
    let admit
    const authority = vi.fn(() => new Promise((resolve) => { admit = resolve }))
    const retry = vi.fn().mockResolvedValue(found({ ...job, status: 'queued' }))
    const staged = { tool: { name: 'example' }, receipt: { change_set_id: ID } }
    const poll = vi.fn().mockResolvedValue(staged)
    const { result } = renderHook(() => useAuthorStageController({
      recoverAuthorStageById: read, retryAuthorStage: retry, authorityProvider: authority, stageAuthorTool: poll,
    }))
    await act(async () => { await result.current.lookupRequest(ID) })
    let first, second
    act(() => { first = result.current.retryRequest(); second = result.current.retryRequest() })
    expect(first).toBe(second)
    expect(authority).toHaveBeenCalledTimes(1)
    await act(async () => { admit({ sessionId: 'new', turnId: 'new' }); await first })
    expect(retry).toHaveBeenCalledTimes(1)
    expect(poll).toHaveBeenCalledTimes(1)
    expect(poll.mock.calls[0][3]).toMatchObject({ pollUrl: job.poll_url, changeSetId: ID })
    expect(result.current.result).toEqual(staged)
    expect(result.current.pointer).toBeNull()
  })

  it('never exposes a mismatched staged receipt from retry', async () => {
    const { result } = renderHook(() => useAuthorStageController({
      recoverAuthorStageById: vi.fn().mockResolvedValue(found()),
      retryAuthorStage: vi.fn().mockResolvedValue(found({ ...job, status: 'queued' })),
      authorityProvider: vi.fn().mockResolvedValue({ sessionId: 'new', turnId: 'new' }),
      stageAuthorTool: vi.fn().mockResolvedValue({ tool: { name: 'foreign' }, receipt: { change_set_id: OTHER } }),
    }))
    await act(async () => { await result.current.lookupRequest(ID) })
    await act(async () => { await result.current.retryRequest() })
    expect(result.current.result).toBeNull()
    expect(result.current.recoveredRequest.message).toMatch(/Check the existing request again/)
  })
  it('shows the failed existing job without creating a pointer, minting authority or staging', async () => {
    const read = vi.fn().mockResolvedValue(found())
    const stage = vi.fn(), authority = vi.fn()
    const { result } = renderHook(() => useAuthorStageController({
      recoverAuthorStageById: read, stageAuthorTool: stage, authorityProvider: authority,
    }))
    const write = vi.spyOn(Storage.prototype, 'setItem')
    await act(async () => { await result.current.lookupRequest(ID) })
    expect(result.current.recoveredRequest.job).toEqual(job)
    expect(result.current.pointer).toBeNull()
    expect(stage).not.toHaveBeenCalled()
    expect(authority).not.toHaveBeenCalled()
    expect(write).not.toHaveBeenCalled()
  })

  it('ignores a late response after the account changes', async () => {
    const values = new Map()
    const storage = { getItem: (key) => values.get(key) || null,
      setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) }
    let resolve
    const read = vi.fn(() => new Promise((done) => { resolve = done }))
    const { result, rerender } = renderHook(() => useAuthorStageController({ storage, recoverAuthorStageById: read }))
    let pending
    act(() => { pending = result.current.lookupRequest(ID) })
    values.set('leaf.org_id', 'different-synthetic-org')
    rerender()
    await act(async () => { resolve(found()); await pending })
    expect(result.current.recoveredRequest).toBeNull()
    expect(result.current.pointer).toBeNull()
  })

  it('keeps a failed lookup separate from generation', async () => {
    const read = vi.fn().mockRejectedValue(new Error('transport failed'))
    const stage = vi.fn()
    const { result } = renderHook(() => useAuthorStageController({ recoverAuthorStageById: read, stageAuthorTool: stage }))
    await act(async () => { await result.current.lookupRequest(ID) })
    expect(result.current.recoveredRequest.message).toBe('Could not check this request. Try again.')
    expect(result.current.pointer).toBeNull()
    expect(stage).not.toHaveBeenCalled()
  })
})

it('offers a normal existing-request check while preserving the supplied ID on failure', () => {
  const lookupRequest = vi.fn()
  render(<AuthorPanel stageActivity={{ lookupRequest, enabled: true, pointer: null,
    recoveredRequest: { busy: false, message: 'Could not check this request. Try again.' } }} />)
  const input = screen.getByLabelText('Existing authoring request ID')
  fireEvent.change(input, { target: { value: ID } })
  fireEvent.click(screen.getByRole('button', { name: 'Check existing request' }))
  expect(lookupRequest).toHaveBeenCalledWith(ID)
  expect(input.value).toBe(ID)
})
