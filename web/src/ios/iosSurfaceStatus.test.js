import { describe, it, expect } from 'vitest'

import { fetchIosSurfaceStatus } from './iosSurfaceStatus.js'

// Minimal Response stand-in for the injected fetchImpl.
function res(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

const CONTRACT = {
  schema: 'leaf.ios-ship-surface.v1',
  project_id: 'p1',
  revision: 'r1',
  reported_at: null,
  readiness: { healthy: true, launchable: true },
  build_stage: 'BUILT',
  receipt_id: 'receipt-1',
}

describe('fetchIosSurfaceStatus', () => {
  it('returns the sanitized contract when the surface is available', async () => {
    let seenUrl = null
    const fetchImpl = async (url) => { seenUrl = url; return res(200, { ok: true, status: 'available', contract: CONTRACT }) }
    const out = await fetchIosSurfaceStatus({ projectId: 'p1', revision: 'r1', fetchImpl })
    expect(out).toEqual(CONTRACT)
    expect(seenUrl).toContain('/api/ios-surface/status?project_id=p1&revision=r1')
  })

  it('returns null when the surface is unavailable (flag on, no upstream)', async () => {
    const fetchImpl = async () => res(200, { ok: true, status: 'unavailable', reason: 'surface_source_unavailable' })
    expect(await fetchIosSurfaceStatus({ projectId: 'p1', revision: 'r1', fetchImpl })).toBeNull()
  })

  it('returns null on a 404 refusal (flag off)', async () => {
    const fetchImpl = async () => res(404, { ok: false, status: 'refused', reason: 'ios_surface_disabled' })
    expect(await fetchIosSurfaceStatus({ projectId: 'p1', revision: 'r1', fetchImpl })).toBeNull()
  })

  it('never throws and returns null when the fetch rejects (unreachable)', async () => {
    const fetchImpl = async () => { throw new Error('network down') }
    await expect(fetchIosSurfaceStatus({ projectId: 'p1', revision: 'r1', fetchImpl })).resolves.toBeNull()
  })

  it('does not fetch and returns null when projectId or revision is missing', async () => {
    let called = false
    const fetchImpl = async () => { called = true; return res(200, {}) }
    expect(await fetchIosSurfaceStatus({ projectId: '', revision: 'r1', fetchImpl })).toBeNull()
    expect(await fetchIosSurfaceStatus({ projectId: 'p1', revision: '', fetchImpl })).toBeNull()
    expect(called).toBe(false)
  })

  it('rejects a contract carrying the wrong schema (defense in depth)', async () => {
    const fetchImpl = async () => res(200, { ok: true, status: 'available', contract: { schema: 'wrong.v1' } })
    expect(await fetchIosSurfaceStatus({ projectId: 'p1', revision: 'r1', fetchImpl })).toBeNull()
  })

  it('url-encodes project and revision ids', async () => {
    let seenUrl = null
    const fetchImpl = async (url) => { seenUrl = url; return res(200, { ok: true, status: 'available', contract: { ...CONTRACT, project_id: 'p/1', revision: 'r 1' } }) }
    await fetchIosSurfaceStatus({ projectId: 'p/1', revision: 'r 1', fetchImpl })
    expect(seenUrl).toContain('project_id=p%2F1')
    expect(seenUrl).toContain('revision=r%201')
  })
})
