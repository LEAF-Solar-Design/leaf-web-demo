import { describe, expect, it } from 'vitest'

import { REQUIRED_FIELDS, normalizeApproval } from './approvalDigest.js'

const COMPLETE_RESPONSE = {
  authority_id: 'opauth-1', action: 'operator.worker_credential_rotate',
  credential_handle: 'staging-broker-token', scope: 'staging:broker',
  environment: 'staging', expires_at: '2026-08-17T12:05:00Z',
  reversal_action: 'operator.worker_credential_rotate', // stand-in reversal string
  cost: 'none', args_hash: 'a'.repeat(64),
}

describe('normalizeApproval: complete server response', () => {
  it('renders all seven canonical fields for a high-impact approval', () => {
    const { digest, missing } = normalizeApproval(COMPLETE_RESPONSE, 'staging')
    expect(missing).toEqual([])
    expect(digest.target).toBe('credential:staging-broker-token')
    expect(digest.environment).toBe('staging')
    expect(digest.cost).toBe('none')
    expect(digest.scope).toBe('staging:broker')
    expect(digest.expiry).toBe('2026-08-17T12:05:00Z')
    expect(digest.reversal).toBe('operator.worker_credential_rotate')
    expect(digest.argsDigest).toBe('a'.repeat(64))
  })

  it('falls back to the session environment only when the response omits it', () => {
    const { digest } = normalizeApproval({ ...COMPLETE_RESPONSE, environment: undefined }, 'production')
    expect(digest.environment).toBe('production')
  })

  it('prefers the response environment over the session fallback', () => {
    const { digest } = normalizeApproval(COMPLETE_RESPONSE, 'production')
    expect(digest.environment).toBe('staging')
  })
})

describe('normalizeApproval: missing fields, per current real backend shapes', () => {
  it('tenant-agent pause propose is missing cost, scope, and args digest', () => {
    // Real server shape from operator_runbooks.py propose().
    const response = {
      authority_id: 'opauth-2', action: 'operator.tenant_agent_pause',
      tenant_id: 'acme-solar', target_revision: 3,
      before: { tenant_id: 'acme-solar', agent_disabled: false, revision: 3 },
      reversal_action: 'operator.tenant_agent_resume',
      expires_at: '2026-08-17T12:05:00Z',
    }
    const { digest, missing } = normalizeApproval(response, 'staging')
    expect(missing).toEqual(expect.arrayContaining(['cost', 'scope', 'argsDigest']))
    expect(digest.target).toBe('tenant:acme-solar')
    expect(digest.reversal).toBe('operator.tenant_agent_resume')
    expect(digest.environment).toBe('staging') // sourced from the session, not the response
  })

  it('stage-release propose is missing cost, scope, reversal, and args digest', () => {
    // Real server shape from operator_stage_release_runbook.py propose().
    const response = {
      authority_id: 'opauth-3', action: 'operator.stage_release_candidate',
      source_sha: 'deadbeef', target: 'staging', expires_at: '2026-08-17T13:00:00Z',
    }
    const { digest, missing } = normalizeApproval(response, 'staging')
    expect(missing).toEqual(expect.arrayContaining(['cost', 'scope', 'reversal', 'argsDigest']))
    expect(digest.target).toBe('staging:deadbeef')
  })

  it('a null/undefined response is fully missing', () => {
    expect(normalizeApproval(null).missing).toEqual(REQUIRED_FIELDS)
    expect(normalizeApproval(undefined).missing).toEqual(REQUIRED_FIELDS)
  })
})
