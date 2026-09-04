import { expect, test } from '@playwright/test'
import { join } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const DRAWING = join(process.cwd(), 'e2e', 'fixtures', 'distinctive-panel.dxf')
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'guest')

test('a signed-out guest can upload and inspect but cannot dispatch a run', async ({ page, request }) => {
  const readyResponse = await request.get(`${API_BASE}/api/ready`, { timeout: 3_000 })
  test.skip(!readyResponse.ok(), `real guest stack is not ready at ${API_BASE}`)
  const ready = await readyResponse.json()
  test.skip(!ready?.ready, `real guest stack is not ready at ${API_BASE}`)

  const observed = []
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    observed.push(`${response.request().method()} ${url.pathname} ${response.status()}`)
  })

  await page.goto('/try')
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible({ timeout: 15_000 })
  const runButton = page.getByRole('button', { name: 'Run', exact: true })
  await expect(runButton).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Upload DWG or DXF' })).toBeEnabled()

  const uploadPromise = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST' && url.pathname === '/api/drawings/upload'
  })
  await page.getByLabel('Drawing file').setInputFiles(DRAWING)
  const uploadResponse = await uploadPromise
  expect(uploadResponse.status()).toBe(202)
  const receipt = await uploadResponse.json()
  expect(receipt.tenant_kind).toBe('guest')
  expect(receipt.tenant_id).toMatch(/^guest-/)
  expect(receipt.guest_session).toBeTruthy()

  await expect(page.locator('.drawing-upload-ready')).toHaveText('Drawing ready', { timeout: 20_000 })
  await expect(page.getByText('Panels preserved').locator('..')).toContainText('1')
  await expect(page.getByTestId('guest-view-only')).toContainText('Guest uploads are view-only.')
  // Guest contract: signed out, no platform session, so transportMock is true
  // and ProjectSwitcher.jsx's `mock` branch renders the static, buttonless
  // chip (proj-chip static) -- tagged "Drawing", never "Project" over a
  // drawing that was never one (the HONEST TAG fix, 94e96cdd / #888).
  await expect(page.locator('.proj-chip.static .tag')).toHaveText('Drawing')
  await expect(page.locator('.proj-chip.static .name')).toHaveText(receipt.drawing_id)
  await expect(page.locator('button.proj-chip')).toHaveCount(0)  // the chip itself is the button when interactive; signed out it is a static span
  await expect(page.locator('.tc-bar-proj')).toHaveText(receipt.drawing_id)
  await expect(runButton).toBeDisabled()
  expect(observed.some((entry) => entry.startsWith('POST /api/run '))).toBe(false)

  await page.getByRole('tab', { name: /Versions/ }).click()
  await expect(page.getByRole('region', { name: 'Version history' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Version history' }).getByRole('button', { name: /v1/ })).toBeVisible({ timeout: 10_000 })
  await expect.poll(() => observed).toContain(`GET /api/drawings/${receipt.drawing_id}/versions 200`)
  await expect(page.locator('.tc-version-panel .tc-panel-error')).toHaveCount(0)
  expect(observed.some((entry) => entry.startsWith('POST /api/run '))).toBe(false)

  const denied = await request.post(`${API_BASE}/api/run`, {
    headers: {
      'Content-Type': 'application/json',
      'X-Guest-Session': receipt.guest_session,
    },
    data: { tool: 'count-by-layer', params: {}, dwg: receipt.drawing_id },
  })
  expect(denied.status()).toBe(403)
  const deniedBody = await denied.json()
  expect(JSON.stringify(deniedBody)).toContain('guest sessions are upload-only')

  writeProofReceipt(join(PROOF_DIR, 'receipt.json'), {
    capability_ids: ['EN-01', 'ID-01', 'ID-04', 'RN-01'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite and FastAPI with LEAF_AUTH_LIVE=1 and signed guest-session authority',
    api_endpoints: observed,
    assertions: [
      'the signed-out surface exposed drawing upload while keeping Run disabled',
      'the live-auth upload minted a signed guest session and extracted the real DXF',
      'the unified viewer replaced the seeded scene with the one-panel guest drawing',
      'the surface labeled the guest drawing as view-only and showed its real drawing id',
      'the Versions panel reused guest authority for its allowed drawing read and loaded v1',
      'no browser action dispatched POST /api/run',
      'a direct run attempt with the valid guest credential failed closed with 403',
    ],
    result: {
      verdict: 'pass',
      drawing_id: receipt.drawing_id,
      tenant_kind: receipt.tenant_kind,
      run_status: denied.status(),
    },
    limitations: [
      'The local harness uses a generated test guest secret, not a deployed staging secret.',
      'APS_LIVE=0 substitutes local DXF extraction for Autodesk APS.',
      'No Auth0 user signs in during this signed-out guest proof.',
    ],
  })
})
