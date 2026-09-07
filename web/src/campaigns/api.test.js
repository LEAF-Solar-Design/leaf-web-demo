import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { bindPublication, invokeCapability, listCapabilities } from './api.js'
import { requestEnrollment } from './api.js'
import { submitCampaign, createRelease, getRelease, listReleases, transitionRelease, retryReleaseStage } from './api.js'
import { downloadReleaseArtifact } from './api.js'

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

describe('completion transport', () => {
  const finish = { delivery_profile: 'cad_file', intended_user: 'Project owner', workflow: 'Download the drawing', artifact_refs: ['project-artifact'] }
  it('sends declarative finish fields and strips executable and authoritative extras', async () => {
    await submitCampaign({ projectId: P, title: 'Drawing', prompt: 'Finish the drawing', mode: 'finish',
      finish: { ...finish, commands: ['bad'], status: 'finished', checks: ['passed'], grants: ['bad'] },
      idempotencyKey: 'finish-key', evidence: 'bad' })
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ project_id: P, title: 'Drawing',
      prompt: 'Finish the drawing', mode: 'finish', finish })
    expect(fetcher.mock.calls[0][1].headers['Idempotency-Key']).toBe('finish-key')
    await createRelease(P, C, { finish, idempotencyKey: 'release-key' })
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ project_id: P, finish })
  })
  it('uses the scoped release routes and rejects client finalization', async () => {
    await listReleases(P, C)
    await getRelease(P, C, E)
    await transitionRelease(P, C, E, 'pause')
    await retryReleaseStage(P, C, E, 'delivery')
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      `https://campaign.test/api/campaigns/${C}/releases?project_id=${P}`,
      `https://campaign.test/api/campaigns/${C}/releases/${E}?project_id=${P}`,
      `https://campaign.test/api/campaigns/${C}/releases/${E}/pause`,
      `https://campaign.test/api/campaigns/${C}/releases/${E}/retry`,
    ])
    expect(JSON.parse(fetcher.mock.calls[3][1].body)).toEqual({ project_id: P, stage: 'delivery' })
    await expect(transitionRelease(P, C, E, 'finish')).rejects.toThrow('pause, resume or cancel')
    await expect(retryReleaseStage(P, C, E, 'execute')).rejects.toThrow('release stage')
    expect(fetcher).toHaveBeenCalledTimes(4)
  })
  it('rejects non-reference objects and malformed profiles before sending', async () => {
    await expect(createRelease(P, C, { finish: { ...finish, artifact_refs: [{ command: 'bad' }] }, idempotencyKey: 'key' })).rejects.toThrow('Artifact reference')
    await expect(createRelease(P, C, { finish: { ...finish, delivery_profile: '../execute' }, idempotencyKey: 'key' })).rejects.toThrow('delivery profile')
    expect(fetcher).not.toHaveBeenCalled()
  })
})

describe('verified authenticated output retrieval', () => {
  const bytes = new TextEncoder().encode('name,total\nAlice,42\n')
  let artifact
  beforeEach(async () => {
    vi.stubGlobal('crypto', webcrypto)
    const sha256 = Array.from(new Uint8Array(await webcrypto.subtle.digest('SHA-256', bytes)), n => n.toString(16).padStart(2, '0')).join('')
    artifact = { name: 'records.csv', byte_count: bytes.length, sha256, valid: true, retrieved: true }
    fetcher.mockResolvedValue({ ok: true, headers: new Headers({ 'Content-Type': 'text/csv' }), arrayBuffer: async () => bytes.buffer })
  })
  it('retrieves exact verified bytes from the constructed endpoint using current account headers', async () => {
    localStorage.setItem('leaf.org_id', 'workspace')
    const result = await downloadReleaseArtifact(P, C, E, { ...artifact, access_path: 'https://other.test/steal', download_url: '/foreign' })
    expect(result).toEqual({ bytes: bytes.buffer, mediaType: 'text/csv', name: 'records.csv' })
    expect(fetcher).toHaveBeenCalledExactlyOnceWith(`https://campaign.test/api/campaigns/${C}/releases/${E}/artifacts/records.csv?project_id=${P}`,
      { redirect: 'error', headers: { Authorization: 'Bearer test-token', 'X-Tenant-Id': 'test-tenant', 'X-Org-Id': 'workspace' } })
  })
  it.each(['../secret', '/secret', 'https://other.test/a', 'folder/file', 'folder\\file', 'file%2fsecret', '..', 'a.'])('rejects unsafe file name %s before fetching', async name => {
    await expect(downloadReleaseArtifact(P, C, E, { ...artifact, name })).rejects.toThrow('file name')
    expect(fetcher).not.toHaveBeenCalled()
  })
  it.each([401, 403, 409])('reports server refusal %s without reading output bytes', async status => {
    const read = vi.fn()
    fetcher.mockResolvedValue({ ok: false, status, json: async () => ({}), arrayBuffer: read })
    await expect(downloadReleaseArtifact(P, C, E, artifact)).rejects.toMatchObject({ status })
    expect(read).not.toHaveBeenCalled()
  })
  it.each([{ sha256: '0'.repeat(64) }, { byte_count: bytes.length + 1 }])('refuses wrong digest or byte count', async override => {
    await expect(downloadReleaseArtifact(P, C, E, { ...artifact, ...override })).rejects.toThrow(/output|release/)
  })
  it.each([{ valid: false }, { retrieved: false }, { byte_count: 1048577 }, { byte_count: 0 }, { sha256: 'A'.repeat(64) }])('requires bounded positive metadata: %j', async override => {
    await expect(downloadReleaseArtifact(P, C, E, { ...artifact, ...override })).rejects.toThrow('output details')
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('cannot skip verification when WebCrypto is unavailable', async () => {
    vi.stubGlobal('crypto', {})
    await expect(downloadReleaseArtifact(P, C, E, artifact)).rejects.toThrow('cannot verify')
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('requires current bearer identity to retrieve an output', async () => {
    localStorage.clear()
    await expect(downloadReleaseArtifact(P, C, E, artifact)).rejects.toThrow('Sign in')
    expect(fetcher).not.toHaveBeenCalled()
  })
})

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
