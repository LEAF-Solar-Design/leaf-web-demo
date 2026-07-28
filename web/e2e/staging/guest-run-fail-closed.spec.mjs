import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { captureStagingIdentity } from './stagingIdentity.mjs'
import { writeProofReceipt } from '../proofReceipt.mjs'

const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'staging', 'guest-run-fail-closed')

// This follows the assertion shape of e2e/guest/signed-out-upload.spec.mjs but,
// because it runs against the real deployed staging tenant instead of a local
// harness, it proves only the refusal path. It never performs the upload
// itself: that call would mint a real guest session and write real guest
// drawing data on the shared staging origin, which is a mutation this
// read-only suite must not cause.
test('a signed-out visitor on staging sees the sign-in gate and run dispatch fails closed', async ({ page, request }) => {
  const identity = await captureStagingIdentity(request)

  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })

  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible({ timeout: 15_000 })
  const runButton = page.getByRole('button', { name: 'Run', exact: true })
  await expect(runButton).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Upload DWG or DXF' })).toBeEnabled()

  const catalogTab = page.locator('#workspace-tab-catalog')
  const authorTab = page.locator('#workspace-tab-author')
  const versionsTab = page.locator('#operations-tab-versions')
  await expect(catalogTab).toBeDisabled()
  await expect(authorTab).toBeDisabled()
  await expect(versionsTab).toBeDisabled()

  // Direct API-level refusal proof. No Authorization header, no guest session
  // header: this must fail closed and must not dispatch a run.
  const denied = await request.post('/api/run', {
    headers: { 'Content-Type': 'application/json' },
    data: { tool: 'count-by-layer', params: {}, dwg: 'staging-proof-nonexistent-drawing' },
  })
  expect([401, 403]).toContain(denied.status())
  const deniedBody = await denied.json().catch(() => ({}))
  expect(deniedBody?.ok).not.toBe(true)

  await page.screenshot({ path: join(PROOF_DIR, 'signed-out-gate.png'), fullPage: true })

  writeProofReceipt(join(PROOF_DIR, 'receipt.json'), {
    capability_ids: ['ID-01', 'RN-01'],
    evidence_tier: 'staging',
    route: '/try',
    runtime: 'deployed staging origin, real browser and real API, no request interception',
    api_endpoints: [`POST /api/run ${denied.status()}`],
    assertions: [
      'the deployed surface presented the "You are not signed in" gate for an anonymous visitor',
      'the Run control stayed disabled while the drawing-upload control stayed enabled, matching the upload-only guest tier contract',
      'the Catalog, Author, and Versions panels stayed gated behind an active session',
      'a direct unauthenticated POST to /api/run was refused and never dispatched a run',
    ],
    result: {
      verdict: 'pass',
      run_dispatch_status: denied.status(),
      error_code: deniedBody?.error?.error_code || null,
      observed_source_revision: identity.source_revision,
      observed_ready: identity.ready,
    },
    limitations: [
      'This proof intentionally stops short of the real guest upload performed by e2e/guest/signed-out-upload.spec.mjs, because that call mutates real staging state.',
      'The authenticated sign-in and post-upload versions read are covered elsewhere (see e2e/staging/auth-required.spec.mjs, skip-gated) and are not proven here.',
    ],
  })
})
