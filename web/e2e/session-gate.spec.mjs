import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('session 401 renders one calm gate and disables execution', async ({ page }) => {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/session') {
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { message: 'sign in required' } }) })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
  await page.goto('/try')
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeDisabled()
  await expect(page.getByText(/drawing backend is unavailable/i)).toHaveCount(0)
  await expect(page).toHaveURL(/\/try$/)
})

test('an Auth0 callback aimed at try does not boot the legacy app', async ({ page }) => {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
  await page.goto('/try?code=fixture-code&state=fixture-state')
  await expect(page.getByTestId('operator-surface')).toBeVisible({ timeout: 15_000 })
  await expect(page).toHaveURL(/\/try\?code=fixture-code&state=fixture-state$/)
  await expect(page.getByRole('tablist', { name: 'Workspace panels' })).toBeVisible()
})
