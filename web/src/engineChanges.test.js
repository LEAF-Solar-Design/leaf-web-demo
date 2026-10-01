import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./api.js', () => ({
  config: { apiBase: 'https://engine.test', tenant: 'test-tenant' },
  authHeaders: vi.fn(() => ({ Authorization: 'Bearer fixture-auth' })),
  noteUnauthorized: vi.fn(),
}))

import { noteUnauthorized } from './api.js'
import {
  listEngineChanges, getEngineChange, markEngineChangeRead,
  requestEngineChangeHold, engineChangeDiscussText,
} from './engineChanges.js'

const card = {
  card_id: 'change-1', title: 'Fix panel selection', state: 'live', feature_id: 'selection',
  summary: 'Selection lost the focused panel. The change restores focus after refresh.',
  change: { pr_number: 123, head_sha: 'a'.repeat(40) },
  evidence: { regression_spec: 'web/e2e/selection.spec.mjs' },
}
const reply = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data })
const calls = [
  ['list', () => listEngineChanges(), { cards: [card], unread_count: 1, next_before: null }],
  ['detail', () => getEngineChange(card.card_id), card],
  ['read', () => markEngineChangeRead(card.card_id), { ...card, unread: false }],
  ['hold', () => requestEngineChangeHold(card.card_id), { ...card, hold_requested_at: '2026-10-01T12:00:00Z' }],
]

beforeEach(() => { vi.stubGlobal('fetch', vi.fn()); vi.clearAllMocks() })
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('engine changes transport', () => {
  for (const [name, call, body] of calls) {
    it(`${name} returns an ok result for 200`, async () => {
      fetch.mockResolvedValue(reply(200, body))
      const result = await call()
      expect(result.kind).toBe('ok')
      expect(name === 'list' ? result.cards[0] : result.card).toEqual(name === 'list' ? card : body)
      expect(fetch.mock.calls[0][1].headers).toMatchObject({ 'X-Tenant-Id': 'test-tenant', Authorization: 'Bearer fixture-auth' })
      expect(noteUnauthorized).toHaveBeenCalled()
    })
    it(`${name} returns forbidden for 403`, async () => {
      fetch.mockResolvedValue(reply(403, {}))
      expect(await call()).toEqual({ kind: 'forbidden' })
    })
    it(`${name} returns unavailable for 503`, async () => {
      fetch.mockResolvedValue(reply(503, {}))
      expect(await call()).toEqual({ kind: 'unavailable', reason: 'store' })
    })
    it(`${name} times out and aborts even if fetch never settles`, async () => {
      vi.useFakeTimers()
      fetch.mockImplementation(() => new Promise(() => {}))
      const pending = call()
      const signal = fetch.mock.calls[0][1].signal
      await vi.advanceTimersByTimeAsync(10_000)
      expect(await pending).toEqual({ kind: 'unavailable', reason: 'timeout' })
      expect(signal.aborted).toBe(true)
      expect(vi.getTimerCount()).toBe(0)
    })
  }

  it('bounds the whole response, including a stalled JSON body', async () => {
    vi.useFakeTimers()
    fetch.mockResolvedValue({ status: 200, ok: true, json: () => new Promise(() => {}) })
    const pending = listEngineChanges({ timeoutMs: 20 })
    await vi.advanceTimersByTimeAsync(20)
    expect(await pending).toMatchObject({ kind: 'unavailable', reason: 'timeout' })
  })
  it('returns typed errors for malformed responses and HTTP errors', async () => {
    fetch.mockResolvedValueOnce(reply(200, { cards: [] })).mockResolvedValueOnce(reply(500, {}))
      .mockResolvedValueOnce({ status: 200, ok: true, json: async () => { throw new Error('bad JSON') } })
    expect(await listEngineChanges()).toMatchObject({ kind: 'error' })
    expect(await getEngineChange('change-1')).toMatchObject({ kind: 'error', httpStatus: 500 })
    expect(await getEngineChange('change-1')).toMatchObject({ kind: 'error' })
  })
  it('returns unavailable for a network failure without exposing the exception', async () => {
    fetch.mockRejectedValue(new Error('a private diagnostic'))
    expect(await listEngineChanges()).toEqual({ kind: 'unavailable', reason: 'network' })
  })
  it('encodes cursors and IDs, bounds the limit, and posts to the two action paths', async () => {
    fetch.mockResolvedValue(reply(200, { cards: [], unread_count: 0 }))
    await listEngineChanges({ limit: 900, before: '2026-10-01T12:00:00+00:00' })
    const url = new URL(fetch.mock.calls[0][0])
    expect(url.searchParams.get('limit')).toBe('100')
    expect(url.searchParams.get('before')).toBe('2026-10-01T12:00:00+00:00')
    await markEngineChangeRead('change /1')
    expect(fetch.mock.calls[1][0]).toBe('https://engine.test/api/engine-changes/change%20%2F1/read')
    await requestEngineChangeHold('change-1')
    expect(fetch.mock.calls[2][0]).toBe('https://engine.test/api/engine-changes/change-1/hold-request')
    expect(fetch.mock.calls[2][1]).toMatchObject({ method: 'POST', body: '{}' })
  })
  it('accepts wrapped cards and an empty successful read receipt', async () => {
    fetch.mockResolvedValueOnce(reply(200, { card })).mockResolvedValueOnce(reply(204, null))
    expect(await getEngineChange('change-1')).toEqual({ kind: 'ok', card })
    expect(await markEngineChangeRead('change-1')).toEqual({ kind: 'ok', card: {} })
  })
  it('does not fetch an invalid card identity', async () => {
    expect(await getEngineChange(null)).toMatchObject({ kind: 'error' })
    expect(await getEngineChange('\ud800')).toMatchObject({ kind: 'error' })
    expect(fetch).not.toHaveBeenCalled()
  })
})

describe('discussion seed', () => {
  it('contains the useful card facts as plain text', () => {
    const seed = engineChangeDiscussText(card)
    for (const value of [card.title, card.state, card.feature_id, card.summary, '123', card.change.head_sha, card.evidence.regression_spec]) {
      expect(seed).toContain(value)
    }
    expect(seed).not.toContain('"card_id"')
  })
  it('bounds every field so the regression path survives a very long summary', () => {
    const seed = engineChangeDiscussText({ ...card, title: 'x'.repeat(5000), summary: 'y'.repeat(5000) })
    expect(seed.length).toBeLessThanOrEqual(1500)
    expect(seed).toContain(card.evidence.regression_spec)
  })
  it('copies neither unlisted auth fields, structured values, nor credential-shaped text', () => {
    // The transport seam is the authority for pattern-library tokens.
    const token = 'unlisted-auth-value'
    const seed = engineChangeDiscussText({
      ...card, token, Authorization: 'Bearer private-token',
      summary: 'What broke: secret=private-secret', title: { token }, feature_id: 'Bearer private-token',
    })
    expect(seed).not.toContain(token)
    expect(seed).not.toContain('private-token')
    expect(seed).not.toContain('private-secret')
    expect(seed).not.toContain('[object Object]')
    expect(seed).toContain('[credential omitted]')
    expect(engineChangeDiscussText({ ...card, summary: '{"internal":"value"}' })).not.toContain('{"internal"')
    expect(engineChangeDiscussText(null).length).toBeLessThanOrEqual(1500)
  })
})
