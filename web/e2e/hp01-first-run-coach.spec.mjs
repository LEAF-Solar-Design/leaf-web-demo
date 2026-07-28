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

function boxesIntersect(a, b) {
  const horizontalOverlap = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)
  const verticalOverlap = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y)
  return horizontalOverlap > 0 && verticalOverlap > 0
}

// Round-3 pin: the card must be clear of the command bar AND BOTH RAILS and
// the caption at every desktop layout class, not just clear of the bar at two
// viewports (round 1's mistake: bottom-right is inside .tc-rail-r at every
// width, and only the bar was asserted).
for (const viewport of [
  { width: 1024, height: 768 },
  { width: 1280, height: 800 },
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
]) {
  test(`the coach clears the bar, both rails, and the caption at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    const state = makeCatProofState()
    await routeSession401(page, state)
    await page.setViewportSize(viewport)

    await page.goto('/try')
    const coach = page.getByTestId('first-run-coach').locator('.coach-card')
    await expect(coach).toBeVisible()
    const coachBox = await coach.boundingBox()
    expect(coachBox).not.toBeNull()

    for (const selector of ['.tc-bar-wrap', '.tc-rail-l', '.tc-rail-r', '.tc-caption']) {
      const element = page.locator(selector).first()
      if (!(await element.isVisible())) continue
      const box = await element.boundingBox()
      expect(box, `${selector} should have a box when visible`).not.toBeNull()
      expect(
        boxesIntersect(coachBox, box),
        `coach ${JSON.stringify(coachBox)} must not intersect ${selector} ${JSON.stringify(box)}`,
      ).toBe(false)
    }
  })
}

test('leaving the tool scene via Back never strands the coach over the landing page', async ({ page }) => {
  // Round-3 pin: enter /try from / via SPA navigation, then browser Back.
  // ToolCast stays mounted with sessionAuthRequired true; the coach must
  // disappear with the scene (the `active` prop gate + data-cast="tool").
  const state = makeCatProofState()
  await routeSession401(page, state)

  await page.goto('/')
  await page.getByRole('button', { name: 'Try Branch — no install' }).click()
  await expect(page.getByTestId('first-run-coach')).toBeVisible()

  await page.goBack()
  await expect(page.getByTestId('first-run-coach')).toHaveCount(0)
})

for (const viewport of [{ width: 844, height: 390 }, { width: 980, height: 600 }]) {
  test(`the coach does not MOUNT in the responsive rail layout at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    // Round-2/3 pin: at <=980px there is no placement for the card, and the
    // matchMedia gate must render null (count 0, not merely CSS-hidden), so
    // no document listeners exist to swallow Escape or pointerdown.
    const state = makeCatProofState()
    await routeSession401(page, state)
    await page.setViewportSize(viewport)

    await page.goto('/try')
    await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()
    await expect(page.getByTestId('first-run-coach')).toHaveCount(0)
  })
}

test('keys pressed while the viewport is small never dismiss the coach the user has not seen', async ({ page }) => {
  // Round-3 pin for the hidden-listener hazard: at a small viewport the
  // component must install NO listeners, so an outside pointerdown (which
  // would hide for the page view) and Escape (which would write the
  // PERMANENT dismissal) must not touch coach state. Escape at /try is also
  // the app's own back-out-of-the-scene key, so the flow follows real
  // behavior: Escape leaves the tool scene; re-entering it at a desktop
  // width must still OFFER the coach, and no dismissal may have been
  // recorded.
  const state = makeCatProofState()
  await routeSession401(page, state)
  await page.setViewportSize({ width: 844, height: 390 })

  await page.goto('/try')
  await expect(page.getByRole('heading', { name: 'You are not signed in' })).toBeVisible()
  await expect(page.getByTestId('first-run-coach')).toHaveCount(0)

  // Pointerdown on a neutral element: no listener may swallow or act on it.
  await page.getByRole('heading', { name: 'You are not signed in' }).click()
  await expect(page).toHaveURL(/\/try/)
  await expect(page.getByTestId('first-run-coach')).toHaveCount(0)

  // Escape backs out to the landing scene (app behavior) -- but it must NOT
  // have written the permanent dismissal, because the user never saw a coach.
  await page.keyboard.press('Escape')
  expect(await page.evaluate(() => localStorage.getItem('leaf.coach.dismissed.v1'))).toBeNull()

  // Re-enter the tool scene at a desktop width: the coach is still offered.
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.getByRole('button', { name: 'Try Branch — no install' }).click()
  await expect(page.getByTestId('first-run-coach')).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('leaf.coach.dismissed.v1'))).toBeNull()
})

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
