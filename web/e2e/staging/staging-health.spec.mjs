import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { captureStagingIdentity } from './stagingIdentity.mjs'
import { writeProofReceipt } from '../proofReceipt.mjs'

const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'staging', 'staging-health')

// HL-01 ("Health and degraded mode"): the deployed staging origin's
// unauthenticated readiness endpoint reports a real, current revision and a
// fully-ready dependency graph. This is independent of the signed-out
// operator UI (see try-loads.spec.mjs), which has its own known display
// issue unrelated to backend health. captureStagingIdentity() throws --
// failing this test -- on any non-2xx response or a missing/malformed
// source_revision, so a passing receipt here is never vacuous.
test('the deployed staging readiness endpoint reports a healthy, current deployment', async ({ request }) => {
  const identity = await captureStagingIdentity(request)
  expect(identity.ready).toBe(true)
  expect(identity.degraded_mode).toBe(false)
  const dependencyStates = Object.values(identity.dependencies)
  expect(dependencyStates.length).toBeGreaterThan(0)
  for (const state of dependencyStates) {
    expect(state).toBe('ready')
  }

  writeProofReceipt(join(PROOF_DIR, 'receipt.json'), {
    capability_ids: ['HL-01'],
    evidence_tier: 'staging',
    route: '/api/ready',
    runtime: 'deployed staging origin, direct unauthenticated API call, no request interception',
    source_commit: identity.source_revision,
    api_endpoints: [identity.endpoint],
    assertions: [
      'GET /api/ready returned 2xx with a real, well-formed source_revision',
      'the reported readiness was true and degraded_mode was false',
      'every reported dependency was in the "ready" state',
    ],
    artifacts: [],
    result: {
      verdict: 'pass',
      observed_source_revision: identity.source_revision,
      observed_dependencies: identity.dependencies,
    },
    limitations: [
      'This proves the "healthy" sub-case of HL-01 only. Degraded, retry, and recovery are not exercised by this read-only call.',
      'No expected source revision is pinned; staging may be mid-reconvergence. The observed revision is recorded, not asserted.',
    ],
  })
})
