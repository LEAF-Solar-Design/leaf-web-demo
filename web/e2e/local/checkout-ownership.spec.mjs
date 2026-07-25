import { expect, test } from '@playwright/test'
import { join, relative } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const DRAWING_ID = 'cat-panels'
const OTHER_HOLDER = 'other-editor'
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

test('checkout conflict, expiry, take, and release stay authoritative in the unified scene', async ({ page, request }, testInfo) => {
  const readyResponse = await request.get(`${API_BASE}/api/ready`, { timeout: 3_000 })
  test.skip(!readyResponse.ok(), `real local stack is not ready at ${API_BASE}`)
  test.skip(!(await readyResponse.json())?.ready, `real local stack is not ready at ${API_BASE}`)

  const seed = await request.post(`${API_BASE}/api/drawings/${DRAWING_ID}/checkout`, {
    headers: TENANT_HEADERS,
    data: { holder: OTHER_HOLDER, ttl_s: 30 },
  })
  expect(seed.status()).toBe(200)
  await expect(seed.json()).resolves.toMatchObject({
    acquired: true,
    checkout: { holder: OTHER_HOLDER },
  })

  const observed = []
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    observed.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await expect(page.getByText(`Editing locked by ${OTHER_HOLDER}`)).toBeVisible()

  await page.getByRole('tab', { name: /Catalog/ }).click()
  const writeTool = page.locator('.tool-card').filter({ hasText: 'delete-marked-panel' })
  await writeTool.getByRole('button').first().click()
  await expect(writeTool.getByRole('button', { name: 'Review & run' })).toBeDisabled()
  expect(observed.filter((entry) => entry.startsWith('POST /api/run '))).toHaveLength(0)

  const deniedRelease = await request.delete(
    `${API_BASE}/api/drawings/${DRAWING_ID}/checkout?holder=not-the-holder`,
    { headers: TENANT_HEADERS },
  )
  expect(deniedRelease.status()).toBe(403)

  const expiringLease = await request.post(`${API_BASE}/api/drawings/${DRAWING_ID}/checkout`, {
    headers: TENANT_HEADERS,
    data: { holder: OTHER_HOLDER, ttl_s: 0.5 },
  })
  expect(expiringLease.status()).toBe(200)
  await page.waitForTimeout(600)
  await page.reload()
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  const take = page.getByRole('button', { name: 'Take edit lock' })
  await expect(take).toBeVisible()

  const takeResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST' && url.pathname === `/api/drawings/${DRAWING_ID}/checkout`
  })
  await take.click()
  expect((await takeResponse).status()).toBe(200)
  await expect(page.getByText('You hold the edit lock')).toBeVisible()

  const releaseResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'DELETE' && url.pathname === `/api/drawings/${DRAWING_ID}/checkout`
  })
  await page.getByRole('button', { name: 'Release' }).click()
  expect((await releaseResponse).status()).toBe(200)
  await expect(page.getByRole('button', { name: 'Take edit lock' })).toBeVisible()

  const versions = await request.get(`${API_BASE}/api/drawings/${DRAWING_ID}/versions`, {
    headers: TENANT_HEADERS,
  })
  expect(versions.status()).toBe(200)
  await expect(versions.json()).resolves.toMatchObject({ drawing_id: DRAWING_ID, checkout: null })
  expect(observed.filter((entry) => entry.startsWith('POST /api/run '))).toHaveLength(0)

  writeProofReceipt(join(PROOF_DIR, 'checkout-ownership-receipt.json'), {
    capability_ids: ['VR-02'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI, drawing manifest, and checkout controller',
    api_endpoints: observed,
    artifacts: [
      relative(join(process.cwd(), '..'), testInfo.outputPath('video.webm')).replaceAll('\\', '/'),
    ],
    assertions: [
      'a real active checkout held by another editor rendered in the unified execution rail',
      'the conflicting checkout disabled the drawing.write review action and sent no run request',
      'a non-holder could not release the active checkout',
      'the expired checkout became available after authoritative refresh',
      'the operator took and released the real checkout through the unified surface',
      'the authoritative versions response ended with no checkout',
    ],
    result: {
      verdict: 'pass',
      drawing_id: DRAWING_ID,
      conflicting_holder: OTHER_HOLDER,
      non_holder_release_status: deniedRelease.status(),
      product_run_count: 0,
      checkout_after_release: null,
    },
    limitations: [
      'LEAF_DRAWING_STORE=legacy uses the local manifest lock, not PostgreSQL compare-and-swap authority.',
      'LEAF_AUTH_LIVE=0 uses the local tenant header and generated holder identity.',
      'APS_LIVE=0 substitutes the local drawing engine for Autodesk APS.',
    ],
  })
})
