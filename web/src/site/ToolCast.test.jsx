import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  IOS_SHIP_READINESS_KIND,
  emptyIosShipReadiness,
  fetchIosShipReadiness,
  getIosShipReceipt,
  hasSecretShapedField,
  iosShipLaunchAffordance,
  makeIosShipLaunchKey,
  requestIosShipLaunch,
  validateIosShipReadiness,
} from './iosShipReadiness.js'
import { productSurfaceStates } from './productSurfaces.js'

const APPROVED = {
  approval_id: 'a-1',
  revision: 'r1',
  source_revision: '83bbde1',
  source_sha256: 'a'.repeat(64),
  bundle_identifier: 'com.leaf.soundbeam',
  marketing_version: '1.2',
  build_number: '19',
}
const READY = {
  record_kind: IOS_SHIP_READINESS_KIND,
  project_id: 'p-1',
  launchable: true,
  healthy: true,
  reported_at: '2026-08-13T12:00:00+00:00',
  grant_status: 'healthy',
  dispatch_available: true,
  approved_launch: APPROVED,
}

afterEach(() => {
  try { localStorage.removeItem('leaf.jwt') } catch { /* node environment */ }
})

describe('project-bound readiness', () => {
  it('accepts only the exact server-approved project and revision', () => {
    const ready = validateIosShipReadiness(READY, { projectId: 'p-1', revision: 'r1' })
    expect(ready.launchable).toBe(true)
    expect(ready.approvedLaunch).toEqual(APPROVED)
    expect(validateIosShipReadiness(READY, { projectId: 'p-2', revision: 'r1' }).launchable).toBe(false)
    expect(validateIosShipReadiness(READY, { projectId: 'p-1', revision: 'r2' }).launchable).toBe(false)
  })

  it('fails closed on missing, unhealthy, malformed, or secret-shaped records', () => {
    expect(validateIosShipReadiness(undefined, { projectId: 'p-1' }).launchable).toBe(false)
    expect(validateIosShipReadiness({ ...READY, launchable: false }, { projectId: 'p-1' }).launchable).toBe(false)
    expect(validateIosShipReadiness({ ...READY, approved_launch: { ...APPROVED, source_sha256: '' } }, { projectId: 'p-1' }).launchable).toBe(false)
    expect(validateIosShipReadiness({ ...READY, apple_password: 'bad' }, { projectId: 'p-1' }).launchable).toBe(false)
    expect(hasSecretShapedField({ nested: { provisioning_profile: 'bad' } })).toBe('$.nested.provisioning_profile')
  })

  it('requires the same project and revision before rendering a launch affordance', () => {
    const ready = validateIosShipReadiness(READY, { projectId: 'p-1', revision: 'r1' })
    expect(iosShipLaunchAffordance(ready, { projectId: 'p-1', revision: 'r1', sessionActive: true })).toBe(true)
    expect(iosShipLaunchAffordance(ready, { projectId: 'p-2', revision: 'r1', sessionActive: true })).toBe(false)
    expect(iosShipLaunchAffordance(ready, { projectId: 'p-1', revision: 'r2', sessionActive: true })).toBe(false)
    expect(iosShipLaunchAffordance(emptyIosShipReadiness(), { projectId: 'p-1', revision: 'r1', sessionActive: true })).toBe(false)
  })
})

describe('authenticated browser transport', () => {
  it('fetches revision-scoped readiness and rejects stale-project responses', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ readiness: READY }) }))
    const good = await fetchIosShipReadiness({ projectId: 'p-1', revision: 'r1', fetchImpl })
    expect(good.launchable).toBe(true)
    expect(fetchImpl.mock.calls[0][0]).toContain('project_id=p-1&revision=r1')
    expect(fetchImpl.mock.calls[0][1].headers['X-Tenant-Id']).toBeTruthy()
    const stale = await fetchIosShipReadiness({ projectId: 'p-2', revision: 'r1', fetchImpl })
    expect(stale.launchable).toBe(false)
    expect(stale.reason).toBe('project_mismatch')
  })

  it('launches with only the seven approved identifiers and auth headers', async () => {
    try { localStorage.setItem('leaf.jwt', 'signed-test-token') } catch { /* node environment */ }
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      status: 202,
      json: async () => ({ ok: true, execution: { execution_id: 'e-1', status: 'dispatched' } }),
    }))
    await requestIosShipLaunch({
      projectId: 'p-1', approvedLaunch: APPROVED,
      idempotencyKey: makeIosShipLaunchKey('p-1', APPROVED), fetchImpl,
    })
    const [, init] = fetchImpl.mock.calls[0]
    expect(init.headers['Idempotency-Key']).toBe('ios-ship:p-1:a-1')
    expect(init.headers.Authorization).toBe('Bearer signed-test-token')
    expect(JSON.parse(init.body)).toEqual({ project_id: 'p-1', ...APPROVED })
    expect(hasSecretShapedField(JSON.parse(init.body))).toBeNull()
  })

  it('refuses a secret-bearing receipt before component state can consume it', async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, receipt: { kind: 'leaf.ios-testflight-receipt.v1', p8_content: 'bad' } }),
    }))
    await expect(getIosShipReceipt({ projectId: 'p-1', receiptId: 'r-1', fetchImpl })).rejects.toThrow()
  })
})

describe('product navigation state', () => {
  it('shows iOS available only from launchable readiness', () => {
    expect(productSurfaceStates({ sessionActive: true, iosReady: true }).ios.state).toBe('available')
    expect(productSurfaceStates({ sessionActive: true, iosReady: false }).ios.state).toBe('setup')
  })
})
