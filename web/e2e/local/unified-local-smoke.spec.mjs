import { expect, test } from '@playwright/test'
import { join, relative } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

test('real local stack runs a catalog tool and restores its durable receipt', async ({ page, request }, testInfo) => {
  const ready = await requireLocalReady(request, test, API_BASE)
  const capabilityResponse = await request.get(`${API_BASE}/api/capabilities`, {
    headers: { 'X-Tenant-Id': 'demo-tenant' },
  })
  expect(capabilityResponse.status()).toBe(200)
  const capabilityCatalog = await capabilityResponse.json()
  const targetFamily = capabilityCatalog.families.find((family) => (
    family.capabilities?.some((capability) => capability.name === 'count-by-layer')
  ))
  expect(targetFamily, 'the real capability catalog must classify count-by-layer').toBeTruthy()
  const capabilityCount = capabilityCatalog.families.reduce(
    (count, family) => count + (family.capabilities?.length || 0),
    0,
  )

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

  await page.goto('/try?proof=1')
  await expect(page).toHaveURL(/\/try\?proof=1$/)
  await expect(page.getByTestId('operator-surface')).toBeVisible()
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await expect(page.getByText('Panels preserved').locator('..')).not.toContainText('pending', { timeout: 15_000 })
  expect(observed).toContain('GET /api/session 200')

  await page.getByRole('tab', { name: /Catalog/ }).click()
  await expect(page.locator('.catalog-summary')).toContainText(`${capabilityCatalog.families.length}`)
  await expect(page.locator('.catalog-summary')).toContainText(`${capabilityCount} capabilities`)
  const family = page.locator('.catalog-family').filter({ hasText: targetFamily.label })
  const familyToggle = family.locator('.catalog-family-head')
  await expect(familyToggle).toHaveAttribute('aria-expanded', 'true')
  await familyToggle.click()
  await expect(familyToggle).toHaveAttribute('aria-expanded', 'false')
  await expect(family.locator('.tool-card')).toHaveCount(0)
  await familyToggle.click()
  await expect(familyToggle).toHaveAttribute('aria-expanded', 'true')
  const toolCard = page.locator('.tool-card').filter({ hasText: 'count-by-layer' })
  await expect(toolCard).toBeVisible()
  await toolCard.getByRole('button').first().click()
  await expect(toolCard).toContainText('drawing.read')
  await expect(toolCard).toContainText('No parameters.')
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
    capability_ids: ['ID-03', 'CA-01', 'HL-01', 'JB-01', 'JB-02'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI, broker, harness, SQLite stores, and job workers',
    api_endpoints: observed,
    assertions: [
      'the real local readiness gate returned ready',
      'the real drawing session loaded a non-empty panel count without API interception',
      'the unified Catalog tab rendered the real capability family and capability counts',
      'the real family could be collapsed and reopened without leaving the unified scene',
      'the count-by-layer detail rendered its capability and parameter contract',
      'catalog review staged an immutable proposal without dispatching a run',
      'explicit confirmation submitted count-by-layer to the real broker and worker',
      'the completed result rendered in the unified scene',
      'the completed job remained available from the durable job API and Jobs rail after reload',
      'no request targeted leaf-proof.invalid',
    ],
    artifacts: [
      relative(join(process.cwd(), '..'), testInfo.outputPath('video.webm')).replaceAll('\\', '/'),
    ],
    result: {
      verdict: 'pass',
      readiness: ready,
      catalog_families: capabilityCatalog.families.length,
      catalog_capabilities: capabilityCount,
      selected_family: targetFamily.family_id,
      job_id: submitted.job_id,
      job_status: 'complete',
    },
    limitations: [
      'APS_LIVE=0 substitutes the local engine for Autodesk APS.',
      'LEAF_AGENT_MOCK=1 substitutes the fake harness runner for Claude.',
      'LEAF_AUTH_LIVE=0 substitutes local tenant identity for Auth0.',
    ],
  })
})
