import { afterEach, expect, it, vi } from 'vitest'
import { fetchInlineAuthoringFlags } from './api.js'

afterEach(() => vi.unstubAllGlobals())

it('reads the tenant inline authoring flags', async () => {
  const flags = { plan_mode: true, inline_review: false, self_approval: false }
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(flags)))
  vi.stubGlobal('fetch', fetch)
  await expect(fetchInlineAuthoringFlags()).resolves.toEqual(flags)
  expect(fetch.mock.calls[0][0]).toMatch(/\/api\/author\/inline-authoring$/)
})

it.each([
  ['network failure', () => Promise.reject(new TypeError('offline'))],
  ['HTTP error', () => Promise.resolve(new Response(null, { status: 503 }))],
  ['invalid JSON', () => Promise.resolve(new Response('invalid'))],
  ['incomplete flags', () => Promise.resolve(new Response('{"plan_mode":true}'))],
])('disables all flags on %s', async (_, fetch) => {
  vi.stubGlobal('fetch', fetch)
  await expect(fetchInlineAuthoringFlags()).resolves.toEqual({
    plan_mode: false, inline_review: false, self_approval: false,
  })
})
