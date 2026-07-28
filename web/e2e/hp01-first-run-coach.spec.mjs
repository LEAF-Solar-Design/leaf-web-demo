import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'
import { shouldOfferCoach } from '../src/demo/tourEntry.js'

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

test('non-tour demo params also suppress the coach (bare and unknown values)', async ({ page }) => {
  const state = makeCatProofState()
  await routeSession401(page, state)

  // Neither of these starts the tour, so a regression that only suppresses
  // the coach for demo=1/demo=tour would fail here.
  await page.goto('/try?demo=')
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()
  await expect(page.getByTestId('first-run-coach')).toHaveCount(0)

  await page.goto('/try?demo=off')
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()
  await expect(page.getByTestId('first-run-coach')).toHaveCount(0)
})

test('shouldOfferCoach predicate: signed-in, dismissed, and demo-param inputs all veto', () => {
  // Pure-module contract, covering inputs the browser tests cannot cheaply
  // reach (a real signed-in session). Mirrors check_tourscript.mjs's headless
  // import guarantee.
  expect(shouldOfferCoach({ search: '', dismissed: false, signedIn: false })).toBe(true)
  expect(shouldOfferCoach({ search: '', dismissed: false, signedIn: true })).toBe(false)
  expect(shouldOfferCoach({ search: '', dismissed: true, signedIn: false })).toBe(false)
  expect(shouldOfferCoach({ search: '?demo=1', dismissed: false, signedIn: false })).toBe(false)
  expect(shouldOfferCoach({ search: '?demo=tour', dismissed: false, signedIn: false })).toBe(false)
  expect(shouldOfferCoach({ search: '?demo=', dismissed: false, signedIn: false })).toBe(false)
  expect(shouldOfferCoach({ search: '?demo=off', dismissed: false, signedIn: false })).toBe(false)
  expect(shouldOfferCoach({ search: '?other=1&demo=x', dismissed: false, signedIn: false })).toBe(false)
  expect(shouldOfferCoach({ search: '?other=1', dismissed: false, signedIn: false })).toBe(true)
})

for (const viewport of [{ width: 1024, height: 768 }, { width: 1440, height: 900 }]) {
  test(`the coach never overlaps the command bar at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    const state = makeCatProofState()
    await routeSession401(page, state)
    await page.setViewportSize(viewport)

    await page.goto('/try')
    const coach = page.getByTestId('first-run-coach').locator('.coach-card')
    await expect(coach).toBeVisible()
    const bar = page.locator('.tc-bar-wrap')
    await expect(bar).toBeVisible()

    const coachBox = await coach.boundingBox()
    const barBox = await bar.boundingBox()
    expect(coachBox).not.toBeNull()
    expect(barBox).not.toBeNull()
    const horizontalOverlap = Math.min(coachBox.x + coachBox.width, barBox.x + barBox.width) - Math.max(coachBox.x, barBox.x)
    const verticalOverlap = Math.min(coachBox.y + coachBox.height, barBox.y + barBox.height) - Math.max(coachBox.y, barBox.y)
    const intersects = horizontalOverlap > 0 && verticalOverlap > 0
    expect(intersects, `coach ${JSON.stringify(coachBox)} must not intersect bar ${JSON.stringify(barBox)}`).toBe(false)
  })
}

for (const viewport of [{ width: 844, height: 390 }, { width: 980, height: 600 }]) {
  test(`the coach does not render in the responsive rail layout at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    // Round-2 pin: at <=980px landing.css hands the whole bottom half to the
    // rails (.tc-rail top:54% width:50%, .tc-rail-r left:50%), so there is no
    // free bottom-right corner; the coach must not render at all rather than
    // sit across live operations tabs and eat their clicks.
    const state = makeCatProofState()
    await routeSession401(page, state)
    await page.setViewportSize(viewport)

    await page.goto('/try')
    await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()
    await expect(page.getByTestId('first-run-coach')).toBeHidden()
  })
}

test('a mid-session 401 flip back to signed-out never resurfaces the coach', async ({ page }) => {
  // Round-2 pin: sessionAuthRequired also becomes true when an ACTIVE
  // session expires (any subscribed API 401). A visitor who was signed in
  // this page view is not a first-run visitor; the coach must stay away.
  const state = makeCatProofState()
  let expired = false
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (expired) {
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { message: 'sign in required' } }) })
      return
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByRole('tab', { name: /Trust/ })).toBeVisible()
  await expect(page.getByTestId('first-run-coach')).toHaveCount(0)

  expired = true
  // Any subscribed API 401 flips platformSession back to 'required'.
  await page.getByRole('tab', { name: /Versions/ }).click()
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('first-run-coach')).toHaveCount(0)
})

test('an active session never shows the coach', async ({ page }) => {
  // Regression pin for the cat-standards collision: with the fixture session
  // ACTIVE (no 401 override), the coach must not mount at all, so it can
  // never cover the operations rail's interactive controls.
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByRole('tab', { name: /Trust/ })).toBeVisible()
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
