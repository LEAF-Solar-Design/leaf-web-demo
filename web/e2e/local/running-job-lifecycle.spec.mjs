import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

async function requireReady(request) {
  const response = await request.get(`${API_BASE}/api/ready`, { timeout: 3_000 })
  test.skip(!response.ok(), `real local stack is not ready at ${API_BASE}`)
  const ready = await response.json()
  test.skip(!ready?.ready, `real local stack is not ready at ${API_BASE}`)
}

async function submitSlowJob(request, seconds = 6) {
  const toolsResponse = await request.get(`${API_BASE}/api/tools`, { headers: TENANT_HEADERS })
  expect(toolsResponse.ok()).toBe(true)
  const tool = (await toolsResponse.json()).tools.find((candidate) => candidate.name === 'count-by-layer')
  expect(tool?.catalog_digest).toBeTruthy()
  const response = await request.post(`${API_BASE}/api/run`, {
    headers: { ...TENANT_HEADERS, 'Content-Type': 'application/json' },
    data: {
      tool: 'count-by-layer',
      params: { _qa_sleep_s: seconds },
      dwg: 'cat-panels',
      catalog_digest: tool.catalog_digest,
    },
  })
  const body = await response.json()
  expect(response.status(), JSON.stringify(body)).toBe(202)
  expect(body.job_id).toBeTruthy()
  return body.job_id
}

async function openWithInflightPointer(page, jobId) {
  await page.addInitScript(({ id }) => {
    localStorage.setItem('leaf.inflightJob', JSON.stringify({
      job_id: id,
      tool: 'count-by-layer',
      ts: Date.now(),
    }))
  }, { id: jobId })
  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
}

async function waitForComplete(request, jobId) {
  await expect.poll(async () => {
    const response = await request.get(`${API_BASE}/api/jobs/${jobId}`, { headers: TENANT_HEADERS })
    return response.ok() ? (await response.json()).status : response.status()
  }, { timeout: 20_000 }).toBe('complete')
}

test('the unified surface reattaches to one real running job and renders its result', async ({ page, request }) => {
  await requireReady(request)
  const jobId = await submitSlowJob(request)
  const browserRuns = []
  page.on('request', (next) => {
    const url = new URL(next.url())
    if (next.method() === 'POST' && url.pathname === '/api/run') browserRuns.push(next)
  })

  await openWithInflightPointer(page, jobId)
  await page.getByRole('tab', { name: /Jobs/ }).click()
  await expect(page.getByText(/Re-attaching to in-flight job/)).toBeVisible({ timeout: 10_000 })
  await page.getByRole('tab', { name: /Execution/ }).click()
  await expect(page.locator('.tc-running')).toBeVisible()
  await expect(page.getByTestId('catalog-run-result')).toContainText('Passed', { timeout: 20_000 })
  expect(browserRuns).toHaveLength(0)
  await waitForComplete(request, jobId)
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightJob'))).toBeNull()

  writeProofReceipt(join(PROOF_DIR, 'running-reattach-receipt.json'), {
    capability_ids: ['JB-01', 'JB-02', 'JB-05'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI, broker, SQLite job store, and job worker with guarded QA latency',
    api_endpoints: ['GET /api/tools', 'POST /api/run', `GET /api/jobs/${jobId}`, `GET /api/jobs/${jobId}/stream`],
    assertions: [
      'a durable in-flight pointer reattached to the exact running job',
      'the unified surface rendered live progress and the terminal result',
      'reattach submitted no browser POST /api/run',
      'the durable pointer cleared after terminal completion',
    ],
    result: { verdict: 'pass', job_id: jobId, job_status: 'complete', browser_run_count: browserRuns.length },
    limitations: [
      'The guarded local _qa_sleep_s hook makes the running state observable.',
      'APS_LIVE=0 substitutes the local engine for Autodesk APS.',
    ],
  })
})

test('Escape detaches from a real running job without closing or duplicating it', async ({ page, request }) => {
  await requireReady(request)
  const jobId = await submitSlowJob(request, 8)
  const closeRequests = []
  const browserRuns = []
  page.on('request', (next) => {
    const url = new URL(next.url())
    if (next.method() === 'POST' && url.pathname === '/api/run') browserRuns.push(next)
    if (next.method() === 'POST' && url.pathname === `/api/jobs/${jobId}/close`) closeRequests.push(next)
  })

  await openWithInflightPointer(page, jobId)
  await expect(page.locator('.tc-running')).toBeVisible({ timeout: 10_000 })
  await page.keyboard.press('Escape')
  await expect(page.locator('.tc-running')).toHaveCount(0)
  await expect(page.locator('.toast')).toContainText('The job keeps running in Jobs.')
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.inflightJob'))).toBeNull()
  expect(closeRequests).toHaveLength(0)
  expect(browserRuns).toHaveLength(0)

  await waitForComplete(request, jobId)
  const jobsResponse = await request.get(`${API_BASE}/api/jobs?limit=20`, { headers: TENANT_HEADERS })
  expect(jobsResponse.ok()).toBe(true)
  const jobs = (await jobsResponse.json()).jobs || []
  expect(jobs.filter((job) => job.job_id === jobId)).toHaveLength(1)
  expect(closeRequests).toHaveLength(0)
  expect(browserRuns).toHaveLength(0)

  writeProofReceipt(join(PROOF_DIR, 'escape-detach-receipt.json'), {
    capability_ids: ['JB-01', 'JB-04'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI, broker, SQLite job store, and job worker with guarded QA latency',
    api_endpoints: ['GET /api/tools', 'POST /api/run', `GET /api/jobs/${jobId}`, 'GET /api/jobs'],
    assertions: [
      'Escape removed the current running state from the unified scene',
      'Escape emitted no POST /api/jobs/{job_id}/close reap beacon',
      'Escape submitted no duplicate POST /api/run',
      'the original durable job completed and appeared exactly once',
    ],
    result: {
      verdict: 'pass',
      job_id: jobId,
      job_status: 'complete',
      matching_job_records: jobs.filter((job) => job.job_id === jobId).length,
      close_request_count: closeRequests.length,
      browser_run_count: browserRuns.length,
    },
    limitations: [
      'The guarded local _qa_sleep_s hook makes the running state observable.',
      'APS_LIVE=0 substitutes the local engine for Autodesk APS.',
    ],
  })
})
