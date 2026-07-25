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
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  await expect(page).toHaveURL(/\/try$/)
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready')
  await page.waitForTimeout(2_600)

  await page.getByRole('tab', { name: /Catalog/ }).click()
  const countTool = page.getByRole('button', { name: /count-panels/i })
  await expect(countTool).toBeVisible()
  await countTool.click()
  await page.getByRole('button', { name: 'Review & run' }).click()
  await expect(page.getByRole('button', { name: 'Run count-panels' })).toBeVisible()
  await page.getByRole('button', { name: 'Run count-panels' }).click()
  await expect(page.getByText('count-panels').first()).toBeVisible({ timeout: 12_000 })
  await page.getByRole('tab', { name: 'Execution' }).click()
  await expect(page.getByTestId('catalog-run-result')).toContainText('count-panels completed')
  await page.screenshot({ path: join(PROOF_DIR, 'standards-00-catalog-run.png'), fullPage: true })
  await page.getByRole('tab', { name: 'Operator' }).click()

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

  await page.getByRole('tab', { name: /Versions/ }).click()
  const versionHistory = page.getByRole('region', { name: 'Version history' })
  await expect(versionHistory.getByRole('button', { name: /v2/ })).toBeVisible()
  await expect(versionHistory.getByRole('button', { name: /v1/ })).toBeVisible()
  await versionHistory.getByRole('button', { name: /v1/ }).click()
  await expect(page.getByText('Viewing v1 read-only')).toBeVisible()
  await expect(page.getByTestId('version-head')).toContainText('Version 2')
  await page.getByRole('button', { name: 'Back to head' }).click()

  await page.getByRole('tab', { name: 'Trust' }).click()
  await expect(page.getByText('Backend').locator('..')).toContainText('healthy')
  await expect(page.getByText('Claude account').locator('..')).toContainText('linked · oauth')
  await expect(page.getByText('Runs today').locator('..')).toContainText('1')
  await expect(page.getByText('Spend remaining').locator('..')).toContainText('$10.00')
  await expect(page.getByText('Entitlements')).toBeVisible()
  await page.screenshot({ path: join(PROOF_DIR, 'standards-02b-versions-trust.png'), fullPage: true })

  await page.getByRole('tab', { name: 'Execution' }).click()

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
    capability_ids: ['CA-01', 'CV-01', 'CV-02', 'RN-01', 'JB-01', 'JB-02', 'VW-01', 'VR-01', 'VR-02', 'VR-03', 'EN-01', 'HL-01', 'AC-01', 'DS-01'],
    evidence_tier: 'contract',
    route: '/try',
    runtime: 'Vite with deterministic API transport',
    api_endpoints: apiEndpoints,
    assertions: [
      'the request, approval, job, drawing result, version, undo, and redo stay on /try',
      'the registered catalog stages, confirms, and completes count-panels through the shared job controller',
      'the operator scene remains mounted through the complete flow',
      'version history previews version 1 without moving the version 2 head',
      'trust shows backend health, linked account kind, usage cap, and entitlements',
      'undo restores version 1 and redo restores version 2',
    ],
    artifacts: [
      '../../cat-operator-proof/standards-00-catalog-run.png',
      '../../cat-operator-proof/standards-01-approval.png',
      '../../cat-operator-proof/standards-02-cat-complete.png',
      '../../cat-operator-proof/standards-02b-versions-trust.png',
      '../../cat-operator-proof/standards-03-undo.png',
      '../../cat-operator-proof/standards-04-redo.png',
    ],
    result: { verdict: 'pass', final_route: '/try', final_version: 2 },
    limitations: [
      'This proves registered catalog and cat operator contracts with deterministic data.',
      'It does not prove a real local backend, Claude, or APS.',
    ],
  })
})
