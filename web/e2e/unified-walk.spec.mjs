import { expect, test } from '@playwright/test'
import { REQUEST, catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('guided walk drives the real cat flow through the unified operator scene', async ({ page }) => {
  test.setTimeout(90_000)
  const state = makeCatProofState()
  const calls = []
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    calls.push(`${request.method()} ${url.pathname}`)
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try?demo=tour')
  await expect(page).toHaveURL(/\/try\?demo=tour$/)
  await expect(page.getByRole('main', { name: 'Leaf operator workspace' })).toBeVisible()
  const tour = page.getByRole('dialog', { name: 'Guided demo tour' })
  await expect(tour).toContainText('One operator scene')

  await tour.getByRole('button', { name: 'Next' }).click()
  await expect(tour).toContainText('The resident drawing')
  await tour.getByRole('button', { name: 'Next' }).click()
  await expect(tour).toContainText('Ask Claude for the cat edit')
  await expect(page.getByLabel('Command bar')).toHaveValue(REQUEST, { timeout: 10_000 })
  const approval = page.getByTestId('operator-surface').getByRole('button', { name: 'Approve' })
  await expect(approval).toBeVisible({ timeout: 15_000 })
  expect(calls.indexOf('POST /api/nl-prompt')).toBeLessThan(calls.indexOf('POST /api/sessions/cat-session/messages'))

  await tour.getByRole('button', { name: 'Next' }).click()
  await expect(tour).toContainText('Approval owns the write boundary')
  await approval.click()
  await expect(page.getByTestId('operator-phase')).toContainText('Cat version ready', { timeout: 15_000 })
  await expect(page.getByTestId('version-head')).toContainText('Version 2')

  await tour.getByRole('button', { name: 'Next' }).click()
  await expect(tour).toContainText('The cat is a new version')
  await expect(page.getByRole('region', { name: 'Version history' })).toContainText('v2')
  await tour.getByRole('button', { name: 'Next' }).click()
  await expect(tour).toContainText('Operational trust stays visible')
  await expect(page.getByText('Runs today').locator('..')).toContainText('1')
  await tour.getByRole('button', { name: 'Next' }).click()
  await expect(tour).toContainText('Continue in the same scene')
  await tour.locator('.tour-card-actions').getByRole('button', { name: /Exit.*explore freely/ }).click()

  await expect(tour).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Restart walk' })).toBeVisible()
  await expect(page).toHaveURL(/\/try\?demo=tour$/)
  expect(state.head).toBe(2)
})
