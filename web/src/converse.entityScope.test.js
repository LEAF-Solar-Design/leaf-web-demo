import { afterEach, expect, it, vi } from 'vitest'
vi.mock('./api.js', () => ({ config: { apiBase: '', tenant: 'T' }, authHeaders: () => ({}), noteUnauthorized: vi.fn() }))
vi.mock('./telemetry.js', () => ({ trackErrorShown: vi.fn(), trackStreamDown: vi.fn() }))
import { postMessage } from './converse.js'

afterEach(() => vi.unstubAllGlobals())

async function body(args) {
  const fetch = vi.fn().mockResolvedValue({ status: 202, ok: true, json: async () => ({ turn_id: 't1' }) })
  vi.stubGlobal('fetch', fetch)
  await postMessage('S', args)
  return fetch.mock.calls[0][1].body
}

it('test_absent_binding_preserves_legacy_body_and_behavior', async () => {
  expect(await body({ text: 'move A' })).toBe('{"text":"move A"}')
  expect(await body({ confirm: { confirmationId: 'c1', approved: true } })).toBe('{"confirm":{"confirmationId":"c1","approved":true}}')
})

it('serializes explicit binding and preserves explicit null', async () => {
  expect(await body({ text: 'move A', entity_scope: { drawing_id: 'D', handle: 'AB12' } })).toBe('{"text":"move A","entity_scope":{"drawing_id":"D","handle":"AB12"}}')
  expect(await body({ text: 'move A', entity_scope: null })).toBe('{"text":"move A","entity_scope":null}')
})
