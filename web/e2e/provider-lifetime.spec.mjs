import { expect, test } from '@playwright/test'
import { REQUEST, catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('the unified drawing controller survives a site recast without reloading version state', async ({ page }) => {
  test.setTimeout(120_000)
  const state = makeCatProofState()
  let sessionReads = 0
  let catSubmissions = 0
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    if (url.pathname === '/api/session') sessionReads += 1
    if (url.pathname === '/api/sessions/cat-session/messages' && body.confirm) catSubmissions += 1
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, state)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  const surface = page.getByTestId('operator-surface')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  const instanceBefore = await surface.getAttribute('data-controller-instance')
  await expect(page.getByRole('textbox', { name: 'Command bar' })).toHaveValue(REQUEST)
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  await expect(surface.getByRole('button', { name: 'Approve' })).toBeVisible({ timeout: 15_000 })
  await surface.getByRole('button', { name: 'Approve' }).click()
  await expect(page.getByTestId('version-head')).toContainText('Version 2', { timeout: 15_000 })
  await expect(page.getByTestId('operator-phase')).toContainText('Cat version ready')
  expect(catSubmissions).toBe(1)
  const readsBeforeRecast = sessionReads

  await page.getByRole('button', { name: 'Back to the site' }).click()
  await expect(page).toHaveURL(/\/$/)
  await page.getByRole('button', { name: /Try Branch/ }).click()
  await expect(page).toHaveURL(/\/try$/)
  await expect(page.getByTestId('version-head')).toContainText('Version 2')
  await expect(page.getByTestId('operator-phase')).toContainText('Cat version ready')
  expect(await surface.getAttribute('data-controller-instance')).toBe(instanceBefore)
  expect(sessionReads).toBe(readsBeforeRecast)
  expect(catSubmissions).toBe(1)
  await page.getByRole('tab', { name: 'View' }).click()
  await expect(page.locator('.viewer-canvas canvas')).toHaveCount(1)
})
