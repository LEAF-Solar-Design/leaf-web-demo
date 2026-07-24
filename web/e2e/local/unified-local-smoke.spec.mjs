import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

test('real local stack opens the unified surface without fixture interception', async ({ page, request }) => {
  let ready
  try {
    const response = await request.get(`${API_BASE}/api/ready`, { timeout: 3_000 })
    if (response.ok()) ready = await response.json()
  } catch {
    ready = null
  }
  test.skip(!ready?.ready, `real local stack is not ready at ${API_BASE}`)

  const observed = []
  page.on('response', (response) => {
    if (response.url().startsWith(API_BASE)) {
      const url = new URL(response.url())
      observed.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
    }
  })
  page.on('request', (request) => {
    expect(request.url()).not.toContain('leaf-proof.invalid')
  })

  await page.goto('/try')
  await expect(page).toHaveURL(/\/try$/)
  await expect(page.getByTestId('operator-surface')).toBeVisible()
  await expect(page.getByTestId('operator-phase')).toContainText('Backend ready', { timeout: 15_000 })
  await expect(page.getByText('Panels preserved').locator('..')).not.toContainText('pending')
  expect(observed).toContain('GET /api/session 200')

  writeProofReceipt(join(PROOF_DIR, 'receipt.json'), {
    capability_ids: ['ID-03', 'HL-01'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI, broker, harness, SQLite stores, and job workers',
    api_endpoints: observed,
    assertions: [
      'the real local readiness gate returned ready',
      'the real drawing session loaded a non-empty panel count without API interception',
      'no request targeted leaf-proof.invalid',
    ],
    result: { verdict: 'pass', readiness: ready },
    limitations: [
      'APS_LIVE=0 substitutes the local engine for Autodesk APS.',
      'LEAF_AGENT_MOCK=1 substitutes the fake harness runner for Claude.',
      'LEAF_AUTH_LIVE=0 substitutes local tenant identity for Auth0.',
      'This first local smoke does not yet dispatch a run.',
    ],
  })
})
