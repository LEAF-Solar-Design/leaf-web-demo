import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

async function runWrite(page, toolCard) {
  await toolCard.getByRole('button', { name: 'Review & run' }).click()
  const confirm = page.getByRole('button', { name: 'Run delete-marked-panel' })
  await expect(confirm).toBeVisible()
  const submission = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST' && url.pathname === '/api/run'
  })
  await confirm.click()
  expect((await submission).status()).toBe(202)
}

test('two real writes support two undos and two redos in the unified scene', async ({ page, request }) => {
  await requireLocalReady(request, test, API_BASE)

  const observed = []
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    observed.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
  })

  await page.addInitScript(() => {
    sessionStorage.setItem('leaf.cat.workbench.id.v1', 'demo')
  })
  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText(/ready/i, { timeout: 15_000 })
  await page.getByRole('tab', { name: /Catalog/ }).click()
  const toolCard = page.locator('.tool-card').filter({ hasText: 'delete-marked-panel' })
  await expect(toolCard).toBeVisible()
  await toolCard.getByRole('button').first().click()

  await runWrite(page, toolCard)
  await expect(page.getByTestId('version-head')).toHaveText('Version 2', { timeout: 30_000 })
  await runWrite(page, toolCard)
  await expect(page.getByTestId('version-head')).toHaveText('Version 3', { timeout: 30_000 })

  await page.getByRole('tab', { name: /Execution/ }).click()
  const undo = page.getByRole('button', { name: 'Undo', exact: true })
  const redo = page.getByRole('button', { name: 'Redo', exact: true })
  await expect(undo).toBeEnabled()
  await expect(redo).toBeDisabled()

  await undo.click()
  await expect(page.getByTestId('version-head')).toHaveText('Version 2')
  await undo.click()
  await expect(page.getByTestId('version-head')).toHaveText('Version 1')
  await expect(undo).toBeDisabled()
  await expect(redo).toBeEnabled()

  await redo.click()
  await expect(page.getByTestId('version-head')).toHaveText('Version 2')
  await redo.click()
  await expect(page.getByTestId('version-head')).toHaveText('Version 3')
  await expect(undo).toBeEnabled()
  await expect(redo).toBeDisabled()

  await page.getByRole('tab', { name: /Versions/ }).click()
  const history = page.getByRole('region', { name: 'Version history' })
  await expect(history.getByRole('button', { name: /v1/ })).toBeVisible()
  await expect(history.getByRole('button', { name: /v2/ })).toBeVisible()
  await expect(history.getByRole('button', { name: /v3/ })).toBeVisible()

  const versionsResponse = await request.get(`${API_BASE}/api/drawings/demo/versions`, { headers: TENANT_HEADERS })
  expect(versionsResponse.ok()).toBe(true)
  await expect(versionsResponse.json()).resolves.toMatchObject({ drawing_id: 'demo', head: 3, latest: 3 })
  expect(observed.filter((entry) => entry === 'POST /api/run 202')).toHaveLength(2)
  expect(observed.filter((entry) => entry === 'POST /api/drawings/demo/undo 200')).toHaveLength(2)
  expect(observed.filter((entry) => entry === 'POST /api/drawings/demo/redo 200')).toHaveLength(2)

  writeProofReceipt(join(PROOF_DIR, 'version-depth-receipt.json'), {
    capability_ids: ['RN-01', 'VR-01', 'VR-03', 'VR-04'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI, broker, worker, version store, and drawing controller',
    api_endpoints: observed,
    assertions: [
      'two explicitly confirmed drawing.write runs created versions 2 and 3',
      'two Undo actions walked the real head from version 3 to version 1',
      'two Redo actions walked the real head from version 1 to version 3',
      'the unified header, action availability, and version history tracked the authoritative head',
      'exactly two run, two undo, and two redo requests were sent',
    ],
    result: {
      verdict: 'pass',
      drawing_id: 'demo',
      head: 3,
      latest: 3,
      run_count: 2,
      undo_count: 2,
      redo_count: 2,
    },
    limitations: ['APS_LIVE=0 substitutes the local write engine for Autodesk APS.'],
  })
})
