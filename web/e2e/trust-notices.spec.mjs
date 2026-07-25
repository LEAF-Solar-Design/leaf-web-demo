import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

async function install(page, override) {
  const state = makeCatProofState()
  const calls = []
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    calls.push({ method: request.method(), path: url.pathname, body })
    if (await override?.({ route, request, url, body, state, calls })) return
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
  return { state, calls }
}

async function runCountPanels(page) {
  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('tab', { name: /Catalog/ }).click()
  await page.getByRole('button', { name: /count-panels/i }).click()
  await page.getByRole('button', { name: 'Review & run' }).click()
  await page.getByRole('button', { name: 'Run count-panels' }).click()
  await page.getByRole('tab', { name: 'Execution' }).click()
}

test('daily run rejection shows the shared quota notice and links to fresh usage', async ({ page }) => {
  const evidence = await install(page, async ({ route, request, url }) => {
    if (url.pathname === '/api/run' && request.method() === 'POST') {
      await route.fulfill({
        status: 429,
        contentType: 'application/json',
        body: JSON.stringify({
          tier: 'starter',
          limit: 10,
          used: 10,
          error: { error_code: 'quota_exceeded', message: 'Daily run limit reached for this plan.', retryable: true },
        }),
      })
      return true
    }
    if (url.pathname === '/api/usage') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ today: { runs: 10, usd_est: 0 }, total: { runs: 10, usd_est: 0 }, cap: { enabled: true, remaining: 10 } }),
      })
      return true
    }
    return false
  })

  await runCountPanels(page)
  const notice = page.locator('.banner.quota').filter({ hasText: 'Daily limit' })
  await expect(notice).toContainText('10/10 runs used')
  await expect(notice).toContainText('Daily run limit reached for this plan.')
  await notice.getByRole('button', { name: 'View usage' }).click()
  await expect(page.getByText('Runs today').locator('..')).toContainText('10')
  expect(evidence.calls.filter((call) => call.path === '/api/run')).toHaveLength(1)
  expect(evidence.calls.some((call) => call.path === '/api/jobs/catalog-job-0001')).toBe(false)
})

test('degraded successful result shows the shared solver notice without hiding the result', async ({ page }) => {
  await install(page, async ({ route, request, url, state }) => {
    if (url.pathname === '/api/jobs/catalog-job-0001' && request.method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          job_id: 'catalog-job-0001',
          status: 'complete',
          tool: 'count-panels',
          elapsed_ms: 120,
          result: {
            ok: true,
            tool: 'count-panels',
            version: '1.0.0',
            timing_ms: 120,
            cost: null,
            error: null,
            degraded_mode: true,
            degraded_reason: 'APS execution was unavailable.',
            overlay: null,
            result: { count: state.count },
          },
        }),
      })
      return true
    }
    return false
  })

  await runCountPanels(page)
  const notice = page.locator('.banner').filter({ hasText: 'Degraded' })
  await expect(notice).toContainText('local solver')
  await expect(page.getByTestId('catalog-run-result')).toContainText('Passed')
  await notice.getByRole('button', { name: 'Details' }).click()
  await expect(notice).toContainText('APS execution was unavailable.')
})

test('degraded backend health is an actionable trust notice', async ({ page }) => {
  let healthReads = 0
  await install(page, async ({ route, url }) => {
    if (url.pathname !== '/api/health') return false
    healthReads += 1
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: false, degraded_mode: true, aps_live: false, da_client_present: true }),
    })
    return true
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('tab', { name: 'Trust' }).click()
  const notice = page.locator('.banner').filter({ hasText: 'Backend degraded' })
  await expect(notice).toContainText('Cloud execution is unavailable.')
  await expect(page.getByText('Backend', { exact: true }).locator('..')).toContainText('degraded')
  const before = healthReads
  await page.getByRole('button', { name: 'Refresh' }).click()
  await expect.poll(() => healthReads).toBeGreaterThan(before)
})
