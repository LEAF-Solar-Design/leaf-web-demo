import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// HP-01 — first-run coach mark.
//
// The coach is ADDITIVE only: it must never touch the ?demo=tour deep-link
// pin (tourEntry.js's shouldStartTour stays untouched, verified separately by
// scripts/check_tourscript.mjs). These specs cover the coach's own contract:
// a fresh, signed-out visitor sees it exactly once, dismissal persists across
// a reload, an explicit `?demo=` param keeps absolute priority over it, and
// its keycap hints are visible.

function routeSession401(page, state) {
  return page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/session') {
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { message: 'sign in required' } }) })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
}

test('fresh profile lands signed-out on /try and sees the first-run coach exactly once', async ({ page }) => {
  const state = makeCatProofState()
  await routeSession401(page, state)

  await page.goto('/try')
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()

  const coach = page.getByTestId('first-run-coach')
  await expect(coach).toBeVisible()
  await expect(coach).toContainText('command bar')

  await page.getByTestId('first-run-coach-dismiss').click()
  await expect(coach).toHaveCount(0)
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.coach.dismissed.v1'))).toBe('1')

  await page.reload()
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()
  await expect(page.getByTestId('first-run-coach')).toHaveCount(0)
})

test('an explicit demo param keeps absolute priority and suppresses the coach', async ({ page }) => {
  const state = makeCatProofState()
  await routeSession401(page, state)

  await page.goto('/try?demo=1')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await expect(page.getByTestId('first-run-coach')).toHaveCount(0)
})

test('the coach shows the command-bar keycap hints', async ({ page }) => {
  const state = makeCatProofState()
  await routeSession401(page, state)

  await page.goto('/try')
  const coach = page.getByTestId('first-run-coach')
  await expect(coach).toBeVisible()

  const focusKey = coach.locator('.key', { hasText: '⌘K' })
  const backKey = coach.locator('.key', { hasText: 'Esc' })
  await expect(focusKey).toBeVisible()
  await expect(backKey).toBeVisible()
  await expect(focusKey).toHaveAttribute('title', /command bar/i)
  await expect(backKey).toHaveAttribute('title', /dismiss/i)
})
