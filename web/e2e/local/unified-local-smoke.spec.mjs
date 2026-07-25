import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

test('real local stack runs a catalog tool and restores its durable receipt', async ({ page, request }) => {
  let ready
  try {
    const response = await request.get(`${API_BASE}/api/ready`, { timeout: 3_000 })
    if (response.ok()) ready = await response.json()
  } catch {
    ready = null
  }
  test.skip(!ready?.ready, `real local stack is not ready at ${API_BASE}`)

  const observed = []
  const runSubmissions = []
  page.on('response', (response) => {
    if (response.url().startsWith(API_BASE)) {
      const url = new URL(response.url())
      observed.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
      if (response.request().method() === 'POST' && url.pathname === '/api/run') {
        runSubmissions.push(response)
      }
    }
  })
  page.on('request', (request) => {
    expect(request.url()).not.toContain('leaf-proof.invalid')
  })

  await page.goto('/try')
  await expect(page).toHaveURL(/\/try$/)
  await expect(page.getByTestId('operator-surface')).toBeVisible()
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await expect(page.getByText('Panels preserved').locator('..')).not.toContainText('pending')
  expect(observed).toContain('GET /api/session 200')

  await page.getByRole('tab', { name: /Catalog/ }).click()
  const toolCard = page.locator('.tool-card').filter({ hasText: 'count-by-layer' })
  await expect(toolCard).toBeVisible()
  await toolCard.getByRole('button').first().click()
  await toolCard.getByRole('button', { name: 'Review & run' }).click()

  const confirmRun = page.getByRole('button', { name: 'Run count-by-layer' })
  await expect(confirmRun).toBeVisible()
  expect(runSubmissions).toHaveLength(0)

  const submissionPromise = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST' && url.pathname === '/api/run'
  })
  await confirmRun.click()
  const submission = await submissionPromise
  expect(submission.status()).toBe(202)
  const submitted = await submission.json()
  expect(submitted.job_id).toBeTruthy()

  await page.getByRole('tab', { name: /Execution/ }).click()
  const result = page.getByTestId('catalog-run-result')
  await expect(result).toContainText('Passed', { timeout: 30_000 })
  await expect(result).toContainText('count-by-layer')

  const durableRecord = await request.get(`${API_BASE}/api/jobs/${submitted.job_id}`, {
    headers: { 'X-Tenant-Id': 'demo-tenant' },
  })
  expect(durableRecord.ok()).toBe(true)
  await expect(durableRecord.json()).resolves.toMatchObject({
    job_id: submitted.job_id,
    tool: 'count-by-layer',
    status: 'complete',
  })

  await page.reload()
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('tab', { name: /Jobs/ }).click()
  const restoredJob = page.locator('.rail-row').filter({ hasText: 'count-by-layer' }).first()
  await expect(restoredJob).toContainText('complete', { timeout: 15_000 })

  writeProofReceipt(join(PROOF_DIR, 'receipt.json'), {
    capability_ids: ['ID-03', 'HL-01', 'JB-01', 'JB-02'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI, broker, harness, SQLite stores, and job workers',
    api_endpoints: observed,
    assertions: [
      'the real local readiness gate returned ready',
      'the real drawing session loaded a non-empty panel count without API interception',
      'catalog review staged an immutable proposal without dispatching a run',
      'explicit confirmation submitted count-by-layer to the real broker and worker',
      'the completed result rendered in the unified scene',
      'the completed job remained available from the durable job API and Jobs rail after reload',
      'no request targeted leaf-proof.invalid',
    ],
    result: { verdict: 'pass', readiness: ready, job_id: submitted.job_id, job_status: 'complete' },
    limitations: [
      'APS_LIVE=0 substitutes the local engine for Autodesk APS.',
      'LEAF_AGENT_MOCK=1 substitutes the fake harness runner for Claude.',
      'LEAF_AUTH_LIVE=0 substitutes local tenant identity for Auth0.',
    ],
  })
})
