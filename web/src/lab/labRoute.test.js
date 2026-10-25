import { afterEach, describe, expect, it, vi } from 'vitest'
import { config } from '../api.js'
import { fetchLabStatus, formatLabRoute, isLabEmbedded, parseLabRoute } from './labRoute.js'
import golden from './__fixtures__/lab-route.v1.golden.json'

afterEach(() => {
  vi.useRealTimers()
  localStorage.removeItem('leaf.jwt')
})

describe('Lab route golden', () => {
  it('round-trips the inert embed string on direct and surface routes', () => {
    for (const [path, search] of [['/app/lab', '?embed=1'], ['/app', '?surface=lab&embed=1']]) {
      const route = parseLabRoute(path, search)
      expect(route).toEqual({ view: 'graph', params: { embed: '1' } })
      expect(formatLabRoute(route)).toBe('/app/lab?embed=1')
    }
    const top = {}
    expect(isLabEmbedded(parseLabRoute('/app/lab', '?embed=1'), { self: top, top })).toBe(false)
    expect(isLabEmbedded(parseLabRoute('/app/lab', '?embed=1'), { self: {}, top })).toBe(true)
    for (const search of ['', '?embed=0', '?embed=true']) {
      expect(isLabEmbedded(parseLabRoute('/app/lab', search), { self: {}, top })).toBe(false)
    }
    expect(parseLabRoute('/app/lab', '?embed=1&embed=1')).toBeNull()
  })
  for (const entry of golden.accepted) {
    it(`parses and round-trips ${entry.path}${entry.search}`, () => {
      const route = parseLabRoute(entry.path, entry.search)
      expect(route).toEqual(entry.route)
      expect(formatLabRoute(route)).toBe(entry.canonical)
      const url = new URL(formatLabRoute(route), 'https://example.com')
      expect(parseLabRoute(url.pathname, url.search)).toEqual(route)
    })
  }
  for (const entry of golden.rejected) {
    it(`rejects ${entry.path}${entry.search}`, () => {
      expect(parseLabRoute(entry.path, entry.search)).toBeNull()
    })
  }
  it('rejects invalid formatter input and non-string parser input', () => {
    for (const route of [null, {}, { view: 'unknown' }, { view: 'graph', params: [] },
      { view: 'graph', params: { view: 'runs' } }, { view: 'graph', params: { surface: 'lab' } },
      { view: 'graph', params: { project: 1 } }]) {
      expect(formatLabRoute(route)).toBeNull()
    }
    expect(parseLabRoute(null)).toBeNull()
    expect(parseLabRoute('/app/lab', null)).toBeNull()
  })
})

const body = { ok: true, schema: 'leaf.lab-status.v1', rail: 'on', posture: 'staging', tenant: 't1' }
const response = (status, data = body) => ({ status, ok: status >= 200 && status < 300, json: async () => data })

describe('fetchLabStatus', () => {
  it('reads the status endpoint with current tenant and auth, exposing only status and posture', async () => {
    const fetchImpl = vi.fn(async () => response(200))
    localStorage.setItem('leaf.jwt', 'first-token')
    expect(await fetchLabStatus({ fetchImpl })).toEqual({ state: 'on', posture: 'staging' })
    expect(fetchImpl).toHaveBeenLastCalledWith(`${config.apiBase}/api/lab/status`, expect.objectContaining({
      headers: { accept: 'application/json', 'X-Tenant-Id': config.tenant, Authorization: 'Bearer first-token' },
      signal: expect.any(AbortSignal),
    }))
    localStorage.setItem('leaf.jwt', 'second-token')
    await fetchLabStatus({ fetchImpl })
    expect(fetchImpl.mock.calls[1][1].headers.Authorization).toBe('Bearer second-token')
  })

  it.each([[404, 'off'], [401, 'signed-out'], [403, 'signed-out'],
    [500, 'unavailable']])('maps HTTP %s to %s without reading its body', async (code, state) => {
    const result = response(code)
    result.json = vi.fn(() => { throw new Error('must not parse an error body') })
    expect(await fetchLabStatus({ fetchImpl: async () => result })).toEqual({ state, posture: null })
    expect(result.json).not.toHaveBeenCalled()
  })

  it.each([
    ["{'ok': False, 'status': 'refused', 'reason': 'lab_auth_live_required'}", 'auth-off'],
    ['lab_store_unavailable', 'unavailable'],
  ])('maps HTTP 503 reason %s to %s', async (message, state) => {
    const fetchImpl = async () => response(503, { ok: false, error: { error_code: 'INTERNAL', message, retryable: true } })
    expect(await fetchLabStatus({ fetchImpl })).toEqual({ state, posture: null })
  })

  it('maps HTTP 503 with a non-JSON body to unavailable', async () => {
    const fetchImpl = async () => ({ ...response(503), json: async () => { throw new SyntaxError('bad JSON') } })
    expect(await fetchLabStatus({ fetchImpl })).toEqual({ state: 'unavailable', posture: null })
  })

  it.each(['local', 'staging', 'production'])('accepts the %s posture', async (posture) => {
    expect(await fetchLabStatus({ fetchImpl: async () => response(200, { ...body, posture }) }))
      .toEqual({ state: 'on', posture })
  })

  it.each([null, {}, { ...body, ok: false }, { ...body, schema: 'wrong' }, { ...body, rail: 'off' },
    { ...body, posture: 'unknown' }, { ...body, tenant: null }])('fails closed for invalid status %#', async (data) => {
    expect(await fetchLabStatus({ fetchImpl: async () => response(200, data) }))
      .toEqual({ state: 'unavailable', posture: null })
  })

  it('never throws for network errors or bad JSON', async () => {
    for (const fetchImpl of [async () => { throw new Error('offline') },
      async () => ({ ...response(200), json: async () => { throw new Error('bad JSON') } })]) {
      await expect(fetchLabStatus({ fetchImpl })).resolves.toEqual({ state: 'unavailable', posture: null })
    }
  })

  it('ends a stalled request with the shared five second budget', async () => {
    vi.useFakeTimers()
    const fetchImpl = vi.fn((url, { signal }) => new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
    }))
    const pending = fetchLabStatus({ fetchImpl })
    await vi.advanceTimersByTimeAsync(5000)
    await expect(pending).resolves.toEqual({ state: 'unavailable', posture: null })
    expect(fetchImpl.mock.calls[0][1].signal.aborted).toBe(true)
  })
})
