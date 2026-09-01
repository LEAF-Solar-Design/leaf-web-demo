import { expect, test } from '@playwright/test'
import { join, relative } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const OPERATOR_JWT = process.env.LEAF_E2E_OPERATOR_JWT
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

test('internal Operations uses a real operator principal without exposing a shared credential', async ({ page, request }, testInfo) => {
  test.skip(!OPERATOR_JWT, 'local stack did not supply a granted operator bearer')
  await requireLocalReady(request, test, API_BASE)

  const observed = []
  let opsRequests = 0
  let opsMutations = 0
  let sharedCredentialLeaks = 0
  page.on('request', (browserRequest) => {
    if (!browserRequest.url().startsWith(API_BASE)) return
    const url = new URL(browserRequest.url())
    const sharedSecret = browserRequest.headers()['x-ops-secret']
    if (sharedSecret) sharedCredentialLeaks += 1
    if (url.pathname.startsWith('/api/operator/')) {
      opsRequests += 1
      expect(browserRequest.headers().authorization).toBe(`Bearer ${OPERATOR_JWT}`)
      if (browserRequest.method() === 'POST') opsMutations += 1
    }
  })
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    if (url.pathname.startsWith('/api/operator/')) {
      observed.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
    }
  })

  await page.addInitScript((jwt) => localStorage.setItem('leaf.jwt', jwt), OPERATOR_JWT)
  await page.goto('/try?ops=1')
  const drawer = page.getByRole('dialog', { name: 'Internal ops' })
  await expect(drawer.getByRole('button', { name: 'Hide drawer' })).toBeFocused()
  const firstRow = drawer.locator('tbody tr').first()
  await expect(firstRow).toBeVisible({ timeout: 15_000 })
  const tenantId = (await firstRow.locator('.ops-tid').textContent()).trim()
  await expect(firstRow).toContainText('Active')
  await expect(page.locator('body')).not.toContainText(OPERATOR_JWT)
  await expect(page).not.toHaveURL(new RegExp(OPERATOR_JWT))

  await firstRow.getByRole('button', { name: 'Disable', exact: true }).click()
  await expect(firstRow).toContainText(`Disable ${tenantId}?`)
  await firstRow.getByRole('button', { name: 'Keep' }).click()
  expect(opsMutations).toBe(0)
  await expect(firstRow).toContainText('Active')

  await firstRow.getByRole('button', { name: 'Disable', exact: true }).click()
  const disableResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST'
      && url.pathname === `/api/operator/tenants/${encodeURIComponent(tenantId)}/disable`
  })
  await firstRow.locator('.chip-danger-confirm').click()
  expect((await disableResponse).status()).toBe(200)
  await expect(firstRow).toContainText('Disabled')

  await firstRow.getByRole('button', { name: 'Enable', exact: true }).click()
  const enableResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST'
      && url.pathname === `/api/operator/tenants/${encodeURIComponent(tenantId)}/enable`
  })
  await firstRow.getByRole('button', { name: 'Enable', exact: true }).click()
  expect((await enableResponse).status()).toBe(200)
  await expect(firstRow).toContainText('Active')
  expect(opsMutations).toBe(2)
  expect(sharedCredentialLeaks).toBe(0)

  await page.evaluate(() => localStorage.removeItem('leaf.jwt'))
  const deniedRefresh = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'GET'
      && url.pathname === '/api/operator/tenants'
      && [401, 403, 404].includes(response.status())
  })
  await drawer.getByRole('button', { name: 'Refresh', exact: true }).click()
  await deniedRefresh
  await expect(page.getByText(/operator grant is required/)).toBeVisible()
  await expect(drawer.locator('tbody tr')).toHaveCount(0)
  expect(sharedCredentialLeaks).toBe(0)

  const denied = await request.get(`${API_BASE}/api/operator/tenants`)
  expect([401, 403, 404]).toContain(denied.status())

  writeProofReceipt(join(PROOF_DIR, 'ops-lifecycle-receipt.json'), {
    capability_ids: ['OP-01'],
    evidence_tier: 'local-e2e',
    route: '/try?ops=1',
    runtime: 'real local Vite, FastAPI operator router, broker kill switch, isolated ledger, and granted operator bearer',
    api_endpoints: observed,
    artifacts: [
      relative(join(process.cwd(), '..'), testInfo.outputPath('video.webm')).replaceAll('\\', '/'),
    ],
    assertions: [
      'the flagged unified scene loaded the real isolated tenant ledger',
      'the destructive first click could be canceled without mutation',
      'explicit confirmation disabled the selected tenant through the real broker authority',
      'explicit confirmation restored the tenant to active before the proof ended',
      'the browser sent its bearer and no shared ops credential',
      'removing the bearer produced a calm operator gate with no tenant rows or actions',
    ],
    result: {
      verdict: 'pass',
      selected_tenant: tenantId,
      ops_requests: opsRequests,
      ops_mutations: opsMutations,
      final_state: 'active',
      denied_without_bearer: true,
      shared_credential_leaks: sharedCredentialLeaks,
    },
    limitations: [
      'The proof uses a configured test operator grant, not a production operator principal.',
      'The tenant ledger and broker kill-switch state are isolated under the managed run directory.',
      'The test bearer and principal are isolated from production.',
    ],
  })
})
