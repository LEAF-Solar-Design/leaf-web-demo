import { expect, test } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { REQUEST, catProofResponse, makeCatProofState } from './catProofFixture.mjs'
import { writeProofReceipt } from './proofReceipt.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const PROOF_DIR = join(HERE, '..', '..', 'artifacts', 'cat-operator-proof')
const UNIFIED_PROOF_DIR = join(HERE, '..', '..', 'artifacts', 'unified-surface-proof', 'wave0-route')

test('standards surface keeps the complete cat operator flow in one scene', async ({ page }) => {
  test.setTimeout(60_000)
  mkdirSync(PROOF_DIR, { recursive: true })
  const proofState = makeCatProofState()
  const apiEndpoints = []

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    apiEndpoints.push(`${request.method()} ${url.pathname}`)
    const body = request.postData() ? request.postDataJSON() : {}
    const result = catProofResponse({ method: request.method(), path: url.pathname, body }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  await expect(page).toHaveURL(/\/try$/)
  await expect(page.getByTestId('operator-phase')).toContainText('Backend ready')
  await page.waitForTimeout(2_600)

  const command = page.getByRole('textbox', { name: 'Command bar' })
  await expect(command).toHaveValue(REQUEST)
  await page.getByRole('button', { name: 'Run', exact: true }).click()

  const surface = page.getByTestId('operator-surface')
  await expect(surface).toContainText('Rearrange the existing panels')
  await expect(surface.getByRole('button', { name: 'Approve' })).toBeVisible({ timeout: 12_000 })
  await page.screenshot({ path: join(PROOF_DIR, 'standards-01-approval.png'), fullPage: true })
  await expect(page).toHaveURL(/\/try$/)

  await surface.getByRole('button', { name: 'Approve' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Cat version ready', { timeout: 15_000 })
  await expect(page.getByTestId('version-head')).toContainText('Version 2')
  await expect(page.getByText('Panels preserved')).toBeVisible()
  await expect(page.getByText('3,328')).toBeVisible()
  await page.screenshot({ path: join(PROOF_DIR, 'standards-02-cat-complete.png'), fullPage: true })
  await expect(page).toHaveURL(/\/try$/)

  await page.getByRole('button', { name: 'Undo' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Original restored')
  await expect(page.getByTestId('version-head')).toContainText('Version 1')
  await page.screenshot({ path: join(PROOF_DIR, 'standards-03-undo.png'), fullPage: true })

  await page.getByRole('button', { name: 'Redo' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Cat version ready')
  await expect(page.getByTestId('version-head')).toContainText('Version 2')
  await page.screenshot({ path: join(PROOF_DIR, 'standards-04-redo.png'), fullPage: true })
  await expect(page).toHaveURL(/\/try$/)

  writeProofReceipt(join(UNIFIED_PROOF_DIR, 'receipt.json'), {
    capability_ids: ['CV-01', 'CV-02', 'RN-01', 'JB-01', 'VW-01', 'VR-01', 'VR-03', 'DS-01'],
    evidence_tier: 'contract',
    route: '/try',
    runtime: 'Vite with deterministic API transport',
    api_endpoints: apiEndpoints,
    assertions: [
      'the request, approval, job, drawing result, version, undo, and redo stay on /try',
      'the operator scene remains mounted through the complete flow',
      'undo restores version 1 and redo restores version 2',
    ],
    artifacts: [
      '../../cat-operator-proof/standards-01-approval.png',
      '../../cat-operator-proof/standards-02-cat-complete.png',
      '../../cat-operator-proof/standards-03-undo.png',
      '../../cat-operator-proof/standards-04-redo.png',
    ],
    result: { verdict: 'pass', final_route: '/try', final_version: 2 },
    limitations: [
      'This proves the Wave 0 route and receipt guardrail with deterministic data.',
      'It does not prove shared production controllers, a real local backend, Claude, or APS.',
    ],
  })
})
