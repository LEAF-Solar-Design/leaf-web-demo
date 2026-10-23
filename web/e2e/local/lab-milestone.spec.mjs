import { expect, test } from '@playwright/test'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'

// Source rows stay in browser memory. Do not retain traces, video or automatic
// screenshots of the local tenant; the receipt contains kind and badge names.
test.use({ video: 'off', trace: 'off', screenshot: 'off' })

test('Lab first click: Jobs, Count and Latest wire live values without persistence', async ({ page, request }, testInfo) => {
  test.setTimeout(120_000)
  await requireLocalReady(request, test, API_BASE)
  await page.setViewportSize({ width: 1920, height: 1080 })
  // Supply a real local demo principal when the local stack enables live auth.
  // No sentinel token or mocked Lab/status, source, or evaluation response.
  if (process.env.LEAF_E2E_LAB_JWT) {
    await page.addInitScript(token => localStorage.setItem('leaf.jwt', token), process.env.LEAF_E2E_LAB_JWT)
  }
  await page.setExtraHTTPHeaders({ 'X-Tenant-Id': 'demo-tenant' })
  const admission = page.waitForResponse(response => new URL(response.url()).pathname === '/api/lab/status')
  await page.goto('/app/lab')
  const admitted = await admission
  expect(admitted.status()).toBe(200)
  expect(await admitted.json()).toMatchObject({ ok: true, rail: 'on', posture: 'local', tenant: 'demo-tenant' })
  const lab = page.getByRole('region', { name: 'Lab', exact: true })
  const palette = lab.getByRole('region', { name: 'Strategy palette', exact: true })
  const canvas = lab.getByRole('region', { name: 'Dependency canvas', exact: true })
  const inspector = lab.getByRole('region', { name: 'Block inspector', exact: true })
  await expect(palette).toBeVisible({ timeout: 30_000 })
  await expect(canvas).toBeVisible()
  await expect(canvas.locator('.react-flow__node')).toHaveCount(0)

  async function dragKind(title, x) {
    await palette.getByRole('button', { name: title, exact: true }).dragTo(canvas.locator('.lab-flow'), {
      targetPosition: { x, y: 150 },
    })
    await expect(canvas.getByRole('article', { name: title, exact: true })).toBeVisible()
    await expect(inspector.getByRole('heading', { name: title, exact: true })).toBeVisible()
  }
  await dragKind('Jobs', 120)
  for (const name of ['Setup', 'Configure', 'Test', 'Code']) {
    await expect(inspector.getByRole('tab', { name, exact: true })).toBeVisible()
  }

  async function connectJobs() {
    await inspector.getByRole('tab', { name: 'Configure', exact: true }).click()
    await inspector.getByLabel('Input', { exact: true }).selectOption('value')
    await inspector.getByLabel('Source', { exact: true }).selectOption({ label: 'studio-jobs-1 / rows' })
    await inspector.getByRole('button', { name: 'Connect pipe', exact: true }).click()
    await expect(inspector.getByRole('button', { name: 'Disconnect value', exact: true })).toBeVisible()
  }
  await dragKind('Count', 380)
  await connectJobs()
  await dragKind('Latest', 640)
  await connectJobs()
  await expect(canvas.locator('.react-flow__node')).toHaveCount(3)
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(2)

  const values = lab.getByRole('region', { name: 'Values', exact: true })
  const count = values.locator('article[data-value-kind="value"]').filter({
    has: page.getByRole('heading', { name: 'Count', exact: true }),
  })
  await expect(count).toHaveCount(1, { timeout: 30_000 })
  await expect(count.locator('p')).toHaveText(/^\d[\d,\s.]*$/)
  await expect(count.locator('[data-badge]').filter({ hasText: /^as of / })).toBeVisible()
  await expect(count.locator('[data-badge]').filter({ hasText: /^p50 / })).toBeVisible()
  await expect(lab.getByText('Save and history: not yet available', { exact: true })).toBeVisible()
  await page.reload()
  await expect(palette).toBeVisible({ timeout: 30_000 })
  await expect(canvas.locator('.react-flow__node')).toHaveCount(0)
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(0)
  await expect(canvas.getByText('Add a block from the palette to begin.', { exact: true })).toBeVisible()
  await expect(values.locator('article')).toHaveCount(0)

  writeProofReceipt(testInfo.outputPath('lab-milestone-receipt.json'), {
    capability_ids: ['LB-01'], evidence_tier: 'local-e2e', route: '/app/lab',
    runtime: 'real local stack, demo tenant', result: 'passed',
    api_endpoints: ['/api/ready', '/api/lab/status', '/api/jobs'],
    assertions: ['Kinds: Jobs, Count, Latest', 'Jobs.rows -> Count.value', 'Jobs.rows -> Latest.value',
      'Inspector: Setup, Configure, Test, Code', 'Count: numeric Value card', 'Badges: as of, p50',
      'Save and history: not yet available', 'Reload: zero cards and pipes'],
    limitations: ['Local Studio walk; authenticated dashboard embed is separately operator-attested'],
  })
})
