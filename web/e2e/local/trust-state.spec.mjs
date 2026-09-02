import { expect, test } from '@playwright/test'
import { join, relative } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const DUMMY_TOKEN = 'sk-ant-oat01-local-e2e-nonsecret-placeholder'
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')

test('trust rail reflects real health, plan, usage, and Claude grant state', async ({ page, request }, testInfo) => {
  await requireLocalReady(request, test, API_BASE)

  const [healthResponse, usageResponse, entitlementResponse, grantResponse] = await Promise.all([
    request.get(`${API_BASE}/api/health`),
    request.get(`${API_BASE}/api/usage`, { headers: TENANT_HEADERS }),
    request.get(`${API_BASE}/api/entitlements`, { headers: TENANT_HEADERS }),
    request.get(`${API_BASE}/api/tenant/claude-grant`, { headers: TENANT_HEADERS }),
  ])
  for (const response of [healthResponse, usageResponse, entitlementResponse, grantResponse]) {
    expect(response.status()).toBe(200)
  }
  const health = await healthResponse.json()
  const usage = await usageResponse.json()
  const entitlements = await entitlementResponse.json()
  const initialGrant = await grantResponse.json()
  expect(initialGrant.linked).toBe(false)

  const observed = []
  const endpointReads = new Map()
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    const entry = `${response.request().method()} ${url.pathname} ${response.status()}`
    observed.push(entry)
    if (response.request().method() === 'GET') {
      endpointReads.set(url.pathname, (endpointReads.get(url.pathname) || 0) + 1)
    }
  })

  const surfacedUsageResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'GET' && url.pathname === '/api/usage' && response.status() === 200
  })
  await page.goto('/try?proof=1')
  const surfacedUsage = await (await surfacedUsageResponse).json()
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('tab', { name: 'Trust' }).click()

  const panel = page.locator('.tc-trust-panel')
  const expectedHealth = health.degraded_mode === true || health.ok === false ? 'degraded' : 'healthy'
  await expect(panel.getByText('Backend', { exact: true }).locator('..')).toContainText(expectedHealth)
  await expect(panel.getByText('Claude account', { exact: true }).first().locator('..')).toContainText('not linked')
  // #869 (W0) rewrote the single 'Spend remaining' field into the full
  // usage ledger /api/usage always shipped: today/total runs+spend and a
  // cap row with a real percentage bar. Assert every field the ledger
  // renders, mirroring ToolCast.jsx's own formatting exactly.
  await expect(panel.getByText('Runs today').locator('..')).toContainText(String(surfacedUsage.today?.runs ?? 'unknown'))
  const spendToday = typeof surfacedUsage.today?.usd_est === 'number' ? `$${surfacedUsage.today.usd_est.toFixed(3)}` : 'unknown'
  await expect(panel.getByText('Spend today').locator('..')).toContainText(spendToday)
  const runsTotal = typeof surfacedUsage.total?.runs === 'number' ? surfacedUsage.total.runs.toLocaleString() : 'unknown'
  await expect(panel.getByText('Runs total').locator('..')).toContainText(runsTotal)
  const spendTotal = typeof surfacedUsage.total?.usd_est === 'number' ? `$${surfacedUsage.total.usd_est.toFixed(2)}` : 'unknown'
  await expect(panel.getByText('Spend total').locator('..')).toContainText(spendTotal)
  const capHasBar = surfacedUsage.cap?.enabled
    && typeof surfacedUsage.cap.remaining === 'number'
    && typeof surfacedUsage.cap.usd_cap === 'number'
    && surfacedUsage.cap.usd_cap > 0
  if (capHasBar) {
    await expect(panel.getByText(`Cap $${surfacedUsage.cap.usd_cap.toFixed(2)}`).locator('..'))
      .toContainText(`$${surfacedUsage.cap.remaining.toFixed(2)} left`)
  } else {
    const capText = surfacedUsage.cap?.enabled === false
      ? 'no cap configured'
      : (typeof surfacedUsage.cap?.remaining === 'number' ? `$${surfacedUsage.cap.remaining.toFixed(2)} left` : 'unknown')
    await expect(panel.getByText('Cap', { exact: true }).locator('..')).toContainText(capText)
  }

  const entitlementPanel = panel.getByRole('region', { name: 'Entitlements' })
  await expect(entitlementPanel).toContainText(`tier ${entitlements.tier}`)
  await expect(entitlementPanel).toContainText('enforced server-side')
  for (const [key, label] of [
    ['run_read', 'Run read-only tools'],
    ['run_write', 'Run editing tools'],
    ['build', 'Author new tools'],
    ['converse', 'Chat with the assistant'],
  ]) {
    const row = entitlementPanel.locator('.ent-row').filter({ hasText: label })
    await expect(row).toContainText(entitlements.entitlements?.[key] === false ? 'not in plan' : 'included')
  }

  const beforeRefresh = new Map(endpointReads)
  await panel.getByRole('button', { name: 'Refresh', exact: true }).click()
  for (const path of ['/api/health', '/api/usage', '/api/entitlements', '/api/tenant/claude-grant']) {
    await expect.poll(() => endpointReads.get(path) || 0).toBeGreaterThan(beforeRefresh.get(path) || 0)
  }

  await panel.locator('.claude-trigger').click()
  await page.getByRole('radio', { name: 'Pro', exact: true }).check()
  await page.getByLabel('Claude token').fill(DUMMY_TOKEN)
  const linkResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST' && url.pathname === '/api/tenant/claude-grant'
  })
  await page.getByRole('button', { name: 'Link Claude account' }).click()
  expect((await linkResponse).status()).toBe(200)
  await expect(panel.locator('.claude-trigger')).toContainText('1 mounted')
  const accountDialog = page.getByRole('dialog', { name: 'Claude accounts' })
  await expect(accountDialog).toContainText(/pro subscription/i)

  const linkedRead = await request.get(`${API_BASE}/api/tenant/claude-grant`, { headers: TENANT_HEADERS })
  expect(linkedRead.status()).toBe(200)
  const linkedGrant = await linkedRead.json()
  expect(linkedGrant).toMatchObject({ linked: true, kind: 'oauth' })
  expect(JSON.stringify(linkedGrant)).not.toContain(DUMMY_TOKEN)

  await accountDialog.locator('.chip-danger').click()
  const unlinkResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'DELETE' && url.pathname === '/api/tenant/claude-grant'
  })
  await accountDialog.locator('.chip-danger-confirm').click()
  expect((await unlinkResponse).status()).toBe(200)
  await expect(panel.locator('.claude-trigger')).toContainText('not linked')

  const finalGrantResponse = await request.get(`${API_BASE}/api/tenant/claude-grant`, { headers: TENANT_HEADERS })
  expect(finalGrantResponse.status()).toBe(200)
  await expect(finalGrantResponse.json()).resolves.toMatchObject({ linked: false, linked_at: null })

  writeProofReceipt(join(PROOF_DIR, 'trust-state-receipt.json'), {
    capability_ids: ['AC-01', 'EN-01', 'EN-02', 'HL-01'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI, entitlement policy, usage ledger, and harness grant store',
    api_endpoints: observed,
    artifacts: [
      relative(join(process.cwd(), '..'), testInfo.outputPath('video.webm')).replaceAll('\\', '/'),
    ],
    assertions: [
      'the unified Trust rail rendered the real backend health classification',
      'the Trust rail rendered real usage and entitlement-policy values',
      'Refresh re-read health, usage, entitlements, and Claude grant state',
      'a masked dummy subscription token linked through the real app-to-harness grant path',
      'the grant status exposed kind and time but never echoed the token',
      'the destructive two-step unlink returned the unified surface and server to not linked',
    ],
    result: {
      verdict: 'pass',
      health: expectedHealth,
      tier: entitlements.tier,
      runs_today: surfacedUsage.today?.runs ?? null,
      spend_remaining: surfacedUsage.cap?.remaining ?? null,
      linked_kind: linkedGrant.kind,
      final_grant_linked: false,
    },
    limitations: [
      'LEAF_AGENT_MOCK=1 uses the local harness and does not contact Claude.',
      'The linked value is an explicit non-secret test placeholder, not a usable credential.',
      'LEAF_AUTH_LIVE=0 uses the local tenant header instead of an Auth0 identity.',
      'Usage is backed by the isolated local run ledger, not production billing.',
    ],
  })
})
