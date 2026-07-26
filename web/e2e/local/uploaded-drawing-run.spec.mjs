import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const DRAWING = join(process.cwd(), 'e2e', 'fixtures', 'distinctive-panel.dxf')
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

test('an uploaded DXF remains the authorized target of a catalog run', async ({ page, request }) => {
  await requireLocalReady(request, test, API_BASE)

  const observed = []
  const invalidRequests = []
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    observed.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
  })
  page.on('request', (outgoing) => {
    if (outgoing.url().includes('leaf-proof.invalid')) invalidRequests.push(outgoing.url())
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await expect(page.getByRole('button', { name: 'Upload DWG or DXF' })).toBeEnabled()

  const uploadPromise = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST' && url.pathname === '/api/drawings/upload'
  })
  await page.getByLabel('Drawing file').setInputFiles(DRAWING)
  const uploadResponse = await uploadPromise
  expect(uploadResponse.status()).toBe(202)
  const receipt = await uploadResponse.json()
  expect(receipt).toMatchObject({
    status: 'extracting',
    tenant_id: 'demo-tenant',
    tenant_kind: 'account',
    guest_session: null,
  })
  expect(receipt.drawing_id).toMatch(/^u-/)

  await expect(page.locator('.drawing-upload-ready')).toHaveText('Drawing ready', { timeout: 20_000 })
  await expect(page.getByText('Panels preserved').locator('..')).toContainText('1')

  await page.getByRole('tab', { name: /Catalog/ }).click()
  const toolCard = page.locator('.tool-card').filter({ hasText: 'count-by-layer' })
  await toolCard.getByRole('button').first().click()
  await toolCard.getByRole('button', { name: 'Review & run' }).click()
  expect(observed.filter((entry) => entry.startsWith('POST /api/run '))).toHaveLength(0)

  const runPromise = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST' && url.pathname === '/api/run'
  })
  await page.getByRole('button', { name: 'Run count-by-layer' }).click()
  const runResponse = await runPromise
  const runRequest = runResponse.request()
  const submitted = runRequest.postDataJSON()
  expect(submitted.dwg).toBe(receipt.drawing_id)
  expect(runRequest.headers()['x-tenant-id']).toBe(receipt.tenant_id)
  expect(runRequest.headers()['x-guest-session']).toBeUndefined()
  expect(runResponse.status()).toBe(202)

  await page.getByRole('tab', { name: /Execution/ }).click()
  const result = page.getByTestId('catalog-run-result')
  await expect(result).toContainText('Passed', { timeout: 30_000 })
  await expect(result).toContainText('count-by-layer')
  expect(observed.filter((entry) => entry === 'POST /api/run 202')).toHaveLength(1)
  expect(invalidRequests).toEqual([])

  writeProofReceipt(join(PROOF_DIR, 'upload-run-receipt.json'), {
    capability_ids: ['CA-01', 'ID-04', 'RN-01'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI, upload extraction, broker, worker, and SQLite stores',
    api_endpoints: observed,
    assertions: [
      'a real DXF upload returned an account-scoped extraction receipt',
      'the uploaded four-corner drawing replaced the seeded scene and rendered one panel',
      'catalog review preserved the uploaded drawing id without executing early',
      'explicit confirmation submitted the uploaded drawing id under the same tenant authority',
      'the real broker and worker returned a passed count-by-layer result',
      'no request targeted leaf-proof.invalid',
    ],
    result: {
      verdict: 'pass',
      drawing_id: receipt.drawing_id,
      tenant_id: receipt.tenant_id,
      tenant_kind: receipt.tenant_kind,
      panel_count: 1,
    },
    limitations: [
      'APS_LIVE=0 substitutes the local engine for Autodesk APS.',
      'LEAF_AUTH_LIVE=0 proves the account-scoped local tenant path, not signed live-auth guest sessions.',
      'The production guest-session path remains a staging gate.',
    ],
  })
})
