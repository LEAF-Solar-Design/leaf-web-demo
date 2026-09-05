import { afterEach, describe, expect, it, vi } from 'vitest'
import { resolveApproval } from './converse.js'

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }
}

function deferredResponse() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

afterEach(() => { vi.unstubAllGlobals() })

describe('approval resume hold across the record wait', () => {
  it('a hold that appears during the approval record stops the resume', async () => {
    const approval = deferredResponse()
    const fetch = vi.fn()
      .mockReturnValueOnce(approval.promise)
      .mockResolvedValue(response(202, { turn_id: 'turn-2', status: 'started' }))
    vi.stubGlobal('fetch', fetch)
    let allowed = true
    const beforeResume = vi.fn(() => allowed)

    const resolution = resolveApproval('c1', 's1', true, false, { beforeResume })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch).toHaveBeenCalledWith(expect.stringMatching(/\/api\/agent\/approvals\/c1$/),
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ approved: true }) }))
    expect(beforeResume).not.toHaveBeenCalled()
    allowed = false
    approval.resolve(response(200, { resolved: true, approved: true }))

    await expect(resolution).resolves.toEqual({ held: true, recorded: true })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(beforeResume).toHaveBeenCalledTimes(1)
  })

  it('a clean hold posts the resume', async () => {
    const approval = deferredResponse()
    const message = { turn_id: 'turn-2', status: 'started' }
    const fetch = vi.fn()
      .mockReturnValueOnce(approval.promise)
      .mockResolvedValue(response(202, message))
    vi.stubGlobal('fetch', fetch)
    const allowed = true
    const beforeResume = vi.fn(() => allowed)

    const resolution = resolveApproval('c1', 's1', true, false, { beforeResume })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(beforeResume).not.toHaveBeenCalled()
    approval.resolve(response(200, { resolved: true, approved: true }))

    await expect(resolution).resolves.toEqual(message)
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(beforeResume).toHaveBeenCalledTimes(1)
    expect(fetch).toHaveBeenNthCalledWith(2, expect.stringMatching(/\/api\/sessions\/s1\/messages$/),
      expect.objectContaining({
        method: 'POST', body: JSON.stringify({ confirm: { confirmationId: 'c1', approved: true } }),
      }))

    fetch.mockClear()
    beforeResume.mockClear()
    await expect(resolveApproval('c1', 's1', true, true, { beforeResume })).resolves.toEqual(message)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(beforeResume).toHaveBeenCalledTimes(1)
    expect(fetch).toHaveBeenCalledWith(expect.stringMatching(/\/api\/sessions\/s1\/messages$/),
      expect.objectContaining({
        method: 'POST', body: JSON.stringify({ confirm: { confirmationId: 'c1', approved: true } }),
      }))
  })

  it('a denial never consults the hook', async () => {
    const approval = deferredResponse()
    const message = { turn_id: 'turn-2', status: 'started' }
    const fetch = vi.fn()
      .mockReturnValueOnce(approval.promise)
      .mockResolvedValue(response(202, message))
    vi.stubGlobal('fetch', fetch)
    const beforeResume = vi.fn(() => { throw new Error('A denial must not check the hold') })

    const resolution = resolveApproval('c1', 's1', false, false, { beforeResume })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch).toHaveBeenCalledWith(expect.stringMatching(/\/api\/agent\/approvals\/c1$/),
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ approved: false }) }))
    approval.resolve(response(200, { resolved: true, approved: false }))

    await expect(resolution).resolves.toEqual(message)
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(beforeResume).not.toHaveBeenCalled()
    expect(fetch).toHaveBeenNthCalledWith(2, expect.stringMatching(/\/api\/sessions\/s1\/messages$/),
      expect.objectContaining({
        method: 'POST', body: JSON.stringify({ confirm: { confirmationId: 'c1', approved: false } }),
      }))
  })
})
