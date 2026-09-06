import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { bindPublication, invokeCapability, listCapabilities } from './api.js'
import { requestEnrollment } from './api.js'

vi.mock('../api.js', () => ({
  config: { apiBase: 'https://campaign.test', tenant: 'test-tenant' },
  authHeaders: () => localStorage.getItem('leaf.jwt') ? { Authorization: 'Bearer test-token' } : {},
}))

const P = '11111111-1111-1111-1111-111111111111'
const C = '33333333-3333-3333-3333-333333333333'
const E = '55555555-5555-5555-5555-555555555555'
const digest = 'a'.repeat(64)
let fetcher
beforeEach(() => {
  localStorage.setItem('leaf.jwt', 'test-token')
  fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) })
  vi.stubGlobal('fetch', fetcher)
})
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear() })

describe('native release registration transport', () => {
  it('preserves host omission and sends the selected native capability through enrollment', async () => {
    await requestEnrollment(P, C, 'VM-C')
    await requestEnrollment(P, C, 'VM-C', 'campaign.native-release')
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      `https://campaign.test/api/campaigns/${C}/enrollments`,
      `https://campaign.test/api/campaigns/${C}/enrollments`,
    ])
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ project_id: P, machine_id: 'VM-C' })
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({
      project_id: P, machine_id: 'VM-C', capability: 'campaign.native-release',
    })
  })

  it.each(['unknown', null, { capability: 'campaign.native-release', role: 'admin' }])(
    'rejects unsupported registration input before transport: %s', async capability => {
      await expect(requestEnrollment(P, C, 'VM-C', capability)).rejects.toThrow('supported registration')
      expect(fetcher).not.toHaveBeenCalled()
    },
  )
})

describe('published campaign capability transport', () => {
  it('uses the closed candidate, publication and invocation wires', async () => {
    await listCapabilities(P, C)
    await bindPublication(P, C, E, 'published-change')
    await invokeCapability(P, C, E, { effectiveCatalogDigest: digest, idempotencyKey: 'durable-key',
      source: 'forbidden', claim: 'forbidden', tenant_id: 'forbidden', command: 'forbidden' })
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      `https://campaign.test/api/campaigns/${C}/capabilities?project_id=${P}`,
      `https://campaign.test/api/campaigns/${C}/enrollments/${E}/publication`,
      `https://campaign.test/api/campaigns/${C}/enrollments/${E}/invoke`,
    ])
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ project_id: P, change_set_id: 'published-change' })
    expect(JSON.parse(fetcher.mock.calls[2][1].body)).toEqual({ project_id: P, effective_catalog_digest: digest })
    expect(fetcher.mock.calls[2][1].headers).toMatchObject({ Authorization: 'Bearer test-token', 'Idempotency-Key': 'durable-key' })
  })

  it('sends the exact expected digest and preserves a stale catalog conflict', async () => {
    fetcher.mockResolvedValue({ ok: false, status: 409, json: async () => ({ error: { error_code: 'catalog_drift' } }) })
    await expect(invokeCapability(P, C, E, { effectiveCatalogDigest: digest, idempotencyKey: 'same-key' }))
      .rejects.toMatchObject({ status: 409, code: 'catalog_drift' })
    expect(JSON.parse(fetcher.mock.calls[0][1].body).effective_catalog_digest).toBe(digest)
  })

  it('requires bearer identity for all capability actions', async () => {
    localStorage.clear()
    await expect(listCapabilities(P, C)).rejects.toThrow('Sign in')
    await expect(bindPublication(P, C, E, 'published-change')).rejects.toThrow('Sign in')
    await expect(invokeCapability(P, C, E, { effectiveCatalogDigest: digest, idempotencyKey: 'key' })).rejects.toThrow('Sign in')
    expect(fetcher).not.toHaveBeenCalled()
  })
})
