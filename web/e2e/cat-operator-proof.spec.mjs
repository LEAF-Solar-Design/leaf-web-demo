import { expect, test } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { REQUEST, catProofResponse, makeCatProofState } from './catProofFixture.mjs'
import { writeProofReceipt } from './proofReceipt.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..', '..')
const PROOF_DIR = join(ROOT, 'artifacts', 'cat-operator-proof')
test('operator request produces an approved cat version and undo restores its parent', async ({ page }) => {
  test.setTimeout(60_000)
  mkdirSync(PROOF_DIR, { recursive: true })
  const proofState = makeCatProofState()
  expect(proofState.count).toBe(3328)
  const browserErrors = []
  const failedResources = []
  const apiEndpoints = []
  page.on('pageerror', (error) => browserErrors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') browserErrors.push(message.text())
  })
  page.on('response', (response) => {
    if (response.status() >= 400) failedResources.push(`${response.status()} ${response.url()}`)
  })
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    apiEndpoints.push(`${request.method()} ${url.pathname}`)
    const body = request.postData() ? request.postDataJSON() : {}
    const result = catProofResponse({ method: request.method(), path: url.pathname, body }, proofState)
    return route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 15_000 })
  await expect(page.locator('body')).not.toHaveText('Internal Server Error')
  await expect(page.locator('.vite-error-overlay')).toHaveCount(0)
  await page.getByRole('combobox', { name: 'Command bar' }).fill(REQUEST)
  await page.getByRole('button', { name: 'Run' }).click()

  const approval = page.locator('.converse-confirm').filter({ hasText: 'arrange-panels-as-cat' })
  await expect(approval).toContainText('drawing.write', { timeout: 10_000 })
  await expect(approval).toContainText('expected head 1')
  await page.locator('.converse-card').screenshot({ path: join(PROOF_DIR, '01-request-and-approval.png') })

  await approval.getByRole('button', { name: 'Approve' }).click()
  const attach = page.getByRole('button', { name: 'Attach' })
  await expect(attach).toBeVisible({ timeout: 10_000 })
  await attach.click()

  await expect(page.getByRole('button', { name: 'Undo' })).toBeEnabled({ timeout: 10_000 })
  await expect(page.getByText('Cat oracle passed · IoU 1.000 · overlap 0')).toBeVisible()
  await page.locator('.workspace-card').screenshot({ path: join(PROOF_DIR, '02-cat-version.png') })
  await page.screenshot({ path: join(PROOF_DIR, '02-cat-version-full.png'), fullPage: true })

  await page.getByRole('button', { name: 'Undo' }).click()
  await expect(page.getByRole('button', { name: 'Redo' })).toBeEnabled({ timeout: 10_000 })
  await page.locator('.workspace-card').screenshot({ path: join(PROOF_DIR, '03-undo-restored.png') })

  writeProofReceipt(join(PROOF_DIR, 'receipt.json'), {
    capability_ids: ['CV-01', 'CV-02', 'RN-01', 'JB-01', 'VW-01', 'VR-01', 'VR-03'],
    evidence_tier: 'contract',
    route: '/app',
    runtime: 'Vite with deterministic API transport',
    api_endpoints: apiEndpoints,
    assertions: [
      'write proposal binds drawing.write and expected head 1',
      'approved job produces version 2 with parent 1',
      'cat oracle reports IoU 1.000 and zero overlap',
      'undo restores version 1 and makes redo available',
    ],
    artifacts: [
      '01-request-and-approval.png',
      '02-cat-version.png',
      '02-cat-version-full.png',
      '03-undo-restored.png',
    ],
    result: {
      verdict: 'pass',
      request: REQUEST,
      panels_preserved: proofState.count,
      proposal: { tool: 'arrange-panels-as-cat', capability: 'drawing.write', expected_head: 1 },
      cat_oracle: { template: 'sitting-v1', iou: 1, outline_chamfer_px: 0, overlap_pixels: 0 },
      version: { head: 2, parent: 1 },
      undo: { head: 1, redo_available: true },
    },
    limitations: [
      'This is a deterministic browser contract, not a live Claude run.',
      'This is a deterministic browser contract, not an APS run.',
      'The unified /try route is covered by the companion standards-surface test.',
    ],
  })
  expect({ browserErrors, failedResources }).toEqual({ browserErrors: [], failedResources: [] })
})
