import { expect, test } from '@playwright/test'
import { join, relative } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const OPS_SECRET = process.env.LEAF_E2E_OPS_SECRET
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

test('internal Operations uses real local authority without leaking its credential', async ({ page, request }, testInfo) => {
  test.skip(!OPS_SECRET, 'managed local stack did not supply a disposable ops credential')
  await requireLocalReady(request, test, API_BASE)

  const observed = []
  let opsRequests = 0
  let opsMutations = 0
  let nonOpsCredentialLeaks = 0
  let opsCredentialExpected = true
  page.on('request', (browserRequest) => {
    if (!browserRequest.url().startsWith(API_BASE)) return
    const url = new URL(browserRequest.url())
    const header = browserRequest.headers()['x-ops-secret']
    if (url.pathname.startsWith('/api/ops/')) {
      opsRequests += 1
      if (opsCredentialExpected) expect(header).toBe(OPS_SECRET)
      else expect(header).toBeFalsy()
      if (browserRequest.method() === 'POST') opsMutations += 1
    } else if (header) {
      nonOpsCredentialLeaks += 1
    }
  })
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    if (url.pathname.startsWith('/api/ops/')) {
      observed.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
    }
  })

  await page.addInitScript((secret) => localStorage.setItem('leaf.ops_secret', secret), OPS_SECRET)
  await page.goto('/try?ops=1')
  const drawer = page.getByRole('dialog', { name: 'Internal ops' })
  await expect(drawer.getByRole('button', { name: 'Hide drawer' })).toBeFocused()
  const firstRow = drawer.locator('tbody tr').first()
  await expect(firstRow).toBeVisible({ timeout: 15_000 })
  const tenantId = (await firstRow.locator('.ops-tid').textContent()).trim()
  await expect(firstRow).toContainText('Active')
  await expect(page.locator('body')).not.toContainText(OPS_SECRET)
  await expect(page).not.toHaveURL(new RegExp(OPS_SECRET))

  await firstRow.getByRole('button', { name: 'Disable', exact: true }).click()
  await expect(firstRow).toContainText(`Disable ${tenantId}?`)
  await firstRow.getByRole('button', { name: 'Keep' }).click()
  expect(opsMutations).toBe(0)
  await expect(firstRow).toContainText('Active')

  await firstRow.getByRole('button', { name: 'Disable', exact: true }).click()
  const disableResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST'
      && url.pathname === `/api/ops/tenants/${encodeURIComponent(tenantId)}/disable`
  })
  await firstRow.locator('.chip-danger-confirm').click()
  expect((await disableResponse).status()).toBe(200)
  await expect(firstRow).toContainText('Disabled')

  await firstRow.getByRole('button', { name: 'Enable', exact: true }).click()
  const enableResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST'
      && url.pathname === `/api/ops/tenants/${encodeURIComponent(tenantId)}/enable`
  })
  await firstRow.getByRole('button', { name: 'Enable', exact: true }).click()
  expect((await enableResponse).status()).toBe(200)
  await expect(firstRow).toContainText('Active')
  expect(opsMutations).toBe(2)
  expect(nonOpsCredentialLeaks).toBe(0)

  await page.evaluate(() => localStorage.removeItem('leaf.ops_secret'))
  opsCredentialExpected = false
  const deniedRefresh = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'GET'
      && url.pathname === '/api/ops/tenants'
      && response.status() === 403
  })
  await drawer.getByRole('button', { name: 'Refresh', exact: true }).click()
  await deniedRefresh
  await expect(page.getByText(/ops role required/)).toBeVisible()
  await expect(drawer.locator('tbody tr')).toHaveCount(0)
  expect(nonOpsCredentialLeaks).toBe(0)

  const denied = await request.get(`${API_BASE}/api/ops/tenants`)
  expect(denied.status()).toBe(403)

  writeProofReceipt(join(PROOF_DIR, 'ops-lifecycle-receipt.json'), {
    capability_ids: ['OP-01'],
    evidence_tier: 'local-e2e',
    route: '/try?ops=1',
    runtime: 'real local Vite, FastAPI ops router, broker kill switch, isolated ledger, and disposable credential',
    api_endpoints: observed,
    artifacts: [
      relative(join(process.cwd(), '..'), testInfo.outputPath('video.webm')).replaceAll('\\', '/'),
    ],
    assertions: [
      'the flagged unified scene loaded the real isolated tenant ledger',
      'the destructive first click could be canceled without mutation',
      'explicit confirmation disabled the selected tenant through the real broker authority',
      'explicit confirmation restored the tenant to active before the proof ended',
      'the disposable ops credential appeared only on ops requests and never in page text or URL',
      'removing the credential produced a calm 403 gate with no tenant rows or actions',
    ],
    result: {
      verdict: 'pass',
      selected_tenant: tenantId,
      ops_requests: opsRequests,
      ops_mutations: opsMutations,
      final_state: 'active',
      denied_without_credential: true,
      non_ops_credential_leaks: nonOpsCredentialLeaks,
    },
    limitations: [
      'The proof uses a random disposable local ops credential, not a production operator secret.',
      'The tenant ledger and broker kill-switch state are isolated under the managed run directory.',
      'LEAF_AUTH_LIVE=0 does not prove a production internal-role identity.',
    ],
  })
})
