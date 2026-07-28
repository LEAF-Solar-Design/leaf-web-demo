import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { captureStagingIdentity } from './stagingIdentity.mjs'
import { writeProofReceipt } from '../proofReceipt.mjs'

const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'staging', 'try-loads')

test('the deployed staging /try surface loads without a server error and the tool catalog renders', async ({ page, request }) => {
  const identity = await captureStagingIdentity(request)

  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
  await expect(page).toHaveURL(/\/try(?:\?|$)/)
  await expect(page.locator('body')).not.toContainText('Internal Server Error')
  await expect(page.locator('body')).not.toContainText('Application error')

  const catalogTab = page.locator('#workspace-tab-catalog')
  await expect(catalogTab).toBeVisible()
  const catalogLabel = await catalogTab.innerText()
  const catalogCountMatch = catalogLabel.match(/(\d+)\s*$/)
  const catalogCount = catalogCountMatch ? Number(catalogCountMatch[1]) : NaN
  expect(Number.isFinite(catalogCount)).toBe(true)
  expect(catalogCount).toBeGreaterThan(0)

  await page.screenshot({ path: join(PROOF_DIR, 'try-loaded.png'), fullPage: true })

  writeProofReceipt(join(PROOF_DIR, 'receipt.json'), {
    capability_ids: ['CA-01'],
    evidence_tier: 'staging',
    route: '/try',
    runtime: 'deployed staging origin, real browser, no request interception',
    api_endpoints: ['GET /api/ready'],
    assertions: [
      'the deployed /try route resolved without a server error banner',
      'the tool catalog badge rendered a positive tool count without requiring a signed-in session',
    ],
    artifacts: ['try-loaded.png'],
    result: {
      verdict: 'pass',
      catalog_tool_count: catalogCount,
      observed_source_revision: identity.source_revision,
      observed_ready: identity.ready,
      observed_degraded_mode: identity.degraded_mode,
    },
    limitations: [
      'This proves the catalog count renders, not family filtering or tool detail, which require an active session on this deployed surface.',
      'No expected source revision is pinned; staging may be mid-reconvergence. The observed revision is recorded, not asserted.',
    ],
  })
})
