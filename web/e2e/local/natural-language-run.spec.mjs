import { expect, test } from '@playwright/test'
import { join, relative } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')
const REQUEST = 'Count the panels by layer in this drawing'

test('natural language routes through real catalog review, dispatch, and result', async ({ page, request }, testInfo) => {
  await requireLocalReady(request, test, API_BASE)

  const routeProbe = await request.post(`${API_BASE}/api/nl-prompt`, {
    headers: TENANT_HEADERS,
    data: { text: REQUEST },
  })
  expect(routeProbe.status()).toBe(200)
  await expect(routeProbe.json()).resolves.toMatchObject({
    lane: 'run',
    tool: 'count-by-layer',
  })

  const observed = []
  const sessionCreates = []
  const runSubmissions = []
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    const entry = `${response.request().method()} ${url.pathname} ${response.status()}`
    observed.push(entry)
    if (response.request().method() === 'POST' && url.pathname === '/api/sessions') {
      sessionCreates.push(response)
    }
    if (response.request().method() === 'POST' && url.pathname === '/api/run') {
      runSubmissions.push(response)
    }
  })

  await page.goto('/try?proof=1')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  const command = page.getByLabel('Command bar')
  await command.fill(REQUEST)
  const routeResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST' && url.pathname === '/api/nl-prompt'
  })
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  expect((await routeResponse).status()).toBe(200)

  const confirm = page.getByRole('button', { name: 'Run count-by-layer' })
  await expect(confirm).toBeVisible()
  await expect(page.locator('.tc-bar')).toContainText('count-by-layer')
  expect(runSubmissions).toHaveLength(0)
  expect(sessionCreates).toHaveLength(0)

  const runResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST' && url.pathname === '/api/run'
  })
  await confirm.click()
  const submission = await runResponse
  expect(submission.status()).toBe(202)
  const accepted = await submission.json()
  expect(accepted.job_id).toBeTruthy()
  expect(runSubmissions).toHaveLength(1)
  expect(sessionCreates).toHaveLength(0)

  await page.getByRole('tab', { name: /Execution/ }).click()
  const result = page.getByTestId('catalog-run-result')
  await expect(result).toContainText('count-by-layer', { timeout: 30_000 })
  await expect(result).toContainText('Passed')

  const durable = await request.get(`${API_BASE}/api/jobs/${accepted.job_id}`, {
    headers: TENANT_HEADERS,
  })
  expect(durable.status()).toBe(200)
  await expect(durable.json()).resolves.toMatchObject({
    job_id: accepted.job_id,
    tool: 'count-by-layer',
    status: 'complete',
  })

  writeProofReceipt(join(PROOF_DIR, 'natural-language-run-receipt.json'), {
    capability_ids: ['CA-02', 'RN-01', 'JB-01'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI natural-language router, catalog, broker, and job worker',
    api_endpoints: observed,
    artifacts: [
      relative(join(process.cwd(), '..'), testInfo.outputPath('video.webm')).replaceAll('\\', '/'),
    ],
    assertions: [
      'the real local natural-language router classified the operator request as count-by-layer',
      'the command bar displayed an immutable review before any run submission',
      'the confident catalog match did not create a Claude conversation session',
      'explicit confirmation submitted exactly one real broker job',
      'the completed count-by-layer result rendered in the unified execution rail',
      'the same terminal result was authoritative in the durable job API',
    ],
    result: {
      verdict: 'pass',
      request: REQUEST,
      routed_tool: 'count-by-layer',
      session_creates: 0,
      run_submissions: 1,
      job_id: accepted.job_id,
      job_status: 'complete',
    },
    limitations: [
      'The v1 natural-language router is deterministic and does not use Claude for confident catalog matches.',
      'APS_LIVE=0 substitutes the local engine for Autodesk Platform Services.',
      'LEAF_AUTH_LIVE=0 uses the local tenant header instead of an Auth0 identity.',
    ],
  })
})
