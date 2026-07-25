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

test('a mid-session 401 latches the whole scene into one gate and stops job polling', async ({ page }) => {
  const state = makeCatProofState()
  let expired = false
  let jobReads = 0
  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/jobs') jobReads += 1
    if (expired && (url.pathname === '/api/jobs' || (url.pathname.endsWith('/checkout') && request.method() === 'POST'))) {
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { message: 'session expired' } }) })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  expired = true
  await page.getByRole('button', { name: 'Take edit lock' }).click()
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeDisabled()
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.jwt'))).toBeNull()
  await page.waitForTimeout(3_500)
  const settledReads = jobReads
  await page.waitForTimeout(3_000)
  expect(jobReads).toBe(settledReads)
})

test('sign out is available for any stored session and returns to the gate', async ({ page }) => {
  const state = makeCatProofState()
  await page.addInitScript(() => {
    if (sessionStorage.getItem('leaf.session.seeded')) return
    sessionStorage.setItem('leaf.session.seeded', '1')
    localStorage.setItem('leaf.jwt', 'fixture-token')
  })
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/session' && !request.headers().authorization) {
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { message: 'sign in required' } }) })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('tab', { name: 'Trust' }).click()
  await page.getByRole('button', { name: 'Account details' }).click()
  await page.getByRole('dialog', { name: 'Account details' }).getByRole('button', { name: 'Sign out' }).click()
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible({ timeout: 15_000 })
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.jwt'))).toBeNull()
})
