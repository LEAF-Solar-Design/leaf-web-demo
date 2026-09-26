import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { setIdentityDisplayName } from './api.js'

const ORG_ID = '11111111-2222-4333-8444-555555555555'
const BINDING_ID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true, status: 200,
    json: async () => ({ identity: { binding_id: BINDING_ID, label: 'Ada Lovelace' } }),
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

it('B5 setIdentityDisplayName PUTs the trimmed name to the org identity label route', async () => {
  const result = await setIdentityDisplayName(ORG_ID, BINDING_ID, '  Ada Lovelace  ')
  expect(globalThis.fetch).toHaveBeenCalledTimes(1)
  const [url, options] = globalThis.fetch.mock.calls[0]
  expect(url).toContain(`/api/orgs/${ORG_ID}/identities/${BINDING_ID}/label`)
  expect(options.method).toBe('PUT')
  expect(JSON.parse(options.body)).toEqual({ display_name: 'Ada Lovelace' })
  expect(options.headers['X-Org-Id']).toBe(ORG_ID)
  expect(result.identity.label).toBe('Ada Lovelace')
  await expect(setIdentityDisplayName(ORG_ID, BINDING_ID, 'a'.repeat(101)))
    .rejects.toThrow('A display name can be at most 100 characters.')
  expect(globalThis.fetch).toHaveBeenCalledTimes(1)
})

it('B5 setIdentityDisplayName sends null to clear a blank name', async () => {
  await setIdentityDisplayName(ORG_ID, BINDING_ID, '   ')
  expect(JSON.parse(globalThis.fetch.mock.calls[0][1].body)).toEqual({ display_name: null })
})

it('B5 setIdentityDisplayName refuses a malformed binding id without fetching', async () => {
  await expect(setIdentityDisplayName(ORG_ID, 'not-a-binding', 'Ada')).rejects.toThrow('not a valid id')
  await expect(setIdentityDisplayName('not-an-org', BINDING_ID, 'Ada')).rejects.toThrow('not a valid id')
  expect(globalThis.fetch).not.toHaveBeenCalled()
})
