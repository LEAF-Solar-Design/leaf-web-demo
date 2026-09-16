// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearRequestFailures, getSession, getTools, recentRequestFailures } from './api.js'

vi.mock('./telemetry.js', () => ({ track: vi.fn(), trackErrorShown: vi.fn() }))

function failWith(status, body, nonJson = false) {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: false,
    status,
    clone() { return this },
    async json() {
      if (nonJson) throw new SyntaxError('not JSON')
      return body
    },
  })))
}

describe('request failure diagnostics', () => {
  beforeEach(() => {
    clearRequestFailures()
    localStorage.clear()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  it('records a frozen, minimal 401 record with an ISO timestamp', async () => {
    failWith(401, { error: { error_code: 'UNAUTHENTICATED', message: 'no' } })
    await expect(getTools(false)).rejects.toMatchObject({ status: 401 })
    const records = recentRequestFailures()
    expect(records).toHaveLength(1)
    expect(records[0]).toEqual({
      at: expect.any(String), method: 'GET', endpointClass: '/api/tools',
      status: 401, errorCode: 'UNAUTHENTICATED', errorId: null,
    })
    expect(new Date(records[0].at).toISOString()).toBe(records[0].at)
    expect(Object.isFrozen(records[0])).toBe(true)
    records.length = 0
    expect(recentRequestFailures()).toHaveLength(1)
  })

  it.each([
    'internal server error (error_id: 0123456789abcdef)',
    'failed, error_id: 0123456789abcdef',
  ])('extracts the server error token without retaining the message: %s', async (message) => {
    failWith(500, { error: { message } })
    await expect(getTools(false)).rejects.toMatchObject({ status: 500 })
    expect(recentRequestFailures()[0]).toMatchObject({ errorCode: null, errorId: '0123456789abcdef' })
    expect(recentRequestFailures()[0]).not.toHaveProperty('message')
  })

  it.each([
    'internal server error (error_id: 0123456789abcdef0123456789abcdef)',
    'error_id: 0123456789abcdefABC',
  ])('does not extract a prefix of an alphanumeric error token: %s', async (message) => {
    failWith(500, { error: { message } })
    await expect(getTools(false)).rejects.toMatchObject({ status: 500 })
    expect(recentRequestFailures()[0].errorId).toBeNull()
  })

  it('records null error fields for a non-JSON response', async () => {
    failWith(502, null, true)
    await expect(getTools(false)).rejects.toMatchObject({ status: 502 })
    expect(recentRequestFailures()[0]).toMatchObject({ status: 502, errorCode: null, errorId: null })
  })

  it('keeps the newest eight failures in order', async () => {
    for (let index = 0; index < 10; index += 1) {
      failWith(500, { error: { error_code: `FAIL_${index}` } })
      await expect(getTools(false)).rejects.toMatchObject({ status: 500 })
    }
    expect(recentRequestFailures().map((record) => record.errorCode)).toEqual([
      'FAIL_2', 'FAIL_3', 'FAIL_4', 'FAIL_5', 'FAIL_6', 'FAIL_7', 'FAIL_8', 'FAIL_9',
    ])
  })

  it('clears the ring', async () => {
    failWith(500, {})
    await expect(getTools(false)).rejects.toMatchObject({ status: 500 })
    expect(recentRequestFailures()).toHaveLength(1)
    clearRequestFailures()
    expect(recentRequestFailures()).toEqual([])
  })

  it('never retains the query string in the endpoint class', async () => {
    failWith(401, {})
    await expect(getSession(false, 'private-drawing')).rejects.toMatchObject({ status: 401 })
    expect(fetch.mock.calls[0][0]).toContain('/api/session?dwg=private-drawing')
    expect(recentRequestFailures()[0].endpointClass).toBe('/api/session')
    expect(JSON.stringify(recentRequestFailures())).not.toContain('private-drawing')
  })
})
