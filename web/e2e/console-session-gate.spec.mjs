// The /app twin of session-gate.spec.mjs (convergence W2b).
//
// session-gate.spec.mjs proves the STAGE's session flows against the shared
// controller. The console ran a hand-rolled twin of that state machine until
// this slice; these are the same flows, driven through /app, so the two
// surfaces are held to one behaviour by two suites rather than one surface by
// one suite.
//
// BUILD SHAPE THIS RUNS UNDER (playwright.config.mjs webServer env): VITE_MOCK
// is 0 and no VITE_AUTH0_* is baked, so `authConfigured` is FALSE. That is the
// deployed-demo-link shape, and it is what makes the auto-demo escape hatch
// (demoState.js) the observable behaviour on a 401 rather than the calm gate.

import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const API = 'http://leaf-proof.invalid/api/**'

function fixtureRoute(page, { override } = {}) {
  const state = makeCatProofState()
  const calls = []
  return page.route(API, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    calls.push({
      method: request.method(),
      path: url.pathname,
      authorization: request.headers().authorization,
    })
    const forced = override?.({ method: request.method(), path: url.pathname, request })
    if (forced) {
      await route.fulfill({
        status: forced.status,
        contentType: 'application/json',
        body: JSON.stringify(forced.body || {}),
        headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
      })
      return
    }
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    const result = catProofResponse({
      method: request.method(),
      path: url.pathname,
      body,
      query: Object.fromEntries(url.searchParams),
    }, state)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  }).then(() => calls)
}

const UNAUTHORIZED = { status: 401, body: { error: { message: 'sign in required' } } }

test('a session 401 on an unconfigured build auto-falls back to the demo', async ({ page }) => {
  test.setTimeout(60_000)
  // The documented escape hatch (demoState.js): the deployed VITE_MOCK=0 link
  // cannot sign in, so the console must land zero-click on the demo instead of
  // parking on a gate whose only button does nothing. Adopting the shared
  // controller must not cost this.
  await fixtureRoute(page, {
    override: ({ path }) => (path === '/api/session' ? UNAUTHORIZED : null),
  })

  await page.goto('/app')
  await expect(page.getByRole('note', { name: 'Guided demo' })).toBeVisible({ timeout: 20_000 })
  await expect(page.locator('.viewer-title')).not.toHaveText('—', { timeout: 20_000 })
  await expect(page.locator('footer.foot-bar')).toContainText('mock (no cloud)')
  // The gate is for builds where sign-in is possible; this one has no Auth0.
  await expect(page.getByText('You’re not signed in')).toHaveCount(0)
  await expect(page.locator('.overlay-msg')).toHaveCount(0)
})

test('a signed-in session passes straight through with no gate and no demo', async ({ page }) => {
  test.setTimeout(60_000)
  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))
  const calls = await fixtureRoute(page)

  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 20_000 })
  await expect(page.locator('footer.foot-bar')).not.toContainText('sign-in required')
  await expect(page.locator('footer.foot-bar')).not.toContainText('mock (no cloud)')
  await expect(page.getByRole('note', { name: 'Guided demo' })).toHaveCount(0)
  await expect(page.locator('.overlay-msg')).toHaveCount(0)
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.jwt'))).toBe('fixture-token')
  expect(calls.some((call) => call.path === '/api/session' && call.authorization === 'Bearer fixture-token')).toBe(true)
})

test('a 401 on a live authed request latches the console gate and keeps it latched', async ({ page }) => {
  test.setTimeout(60_000)
  // The console's OTHER refusal channel. /api/session succeeds, so no auto-demo
  // fires; the jobs poll then 401s with the stored token, which api.js proves
  // bad and wipes. Before W2b three observers wrote one shared boolean and a
  // later successful jobs read cleared it; now only a re-verified /api/session
  // 200 (or the bounded token recovery) may leave `required`.
  let expired = false
  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))
  await fixtureRoute(page, {
    override: ({ path }) => (expired && path === '/api/jobs' ? UNAUTHORIZED : null),
  })

  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 20_000 })
  await expect(page.locator('footer.foot-bar')).not.toContainText('sign-in required')

  expired = true
  // The footer flips to the ONE honest unauthenticated signal, and the viewer
  // pane says how to get out. Both read `signedOut`, which now reads the shared
  // controller's `required` status.
  await expect(page.locator('footer.foot-bar')).toContainText('sign-in required', { timeout: 30_000 })
  await expect(page.locator('.overlay-msg')).toContainText('Sign in or explore the demo to load a drawing.')
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.jwt'))).toBeNull()

  // Latched: it does not flap back on its own once the refusal has landed.
  await page.waitForTimeout(3_000)
  await expect(page.locator('footer.foot-bar')).toContainText('sign-in required')
})

test('an Auth0 callback on /app defers every /api call until the exchange resolves', async ({ page }) => {
  test.setTimeout(60_000)
  // ACCEPTANCE route matrix, the row that must never regress: the redirect_uri
  // is the bare ORIGIN, so the callback query can land on ANY path. Nothing
  // that talks to /api may render while the code exchange is in flight — that
  // burst is what latched a valid session into `required` on 2026-08-17.
  //
  // The witness is recorded INSIDE the page, at request time: SiteRoot's
  // deferral is an early return, so a single /api request observed while
  // `.site-auth-callback` is mounted falsifies the contract. Reading it from
  // the page removes every timing race the test would otherwise have.
  await page.addInitScript(() => {
    window.__apiWhileDeferred = []
    const realFetch = window.fetch
    window.fetch = (input, init) => {
      const url = String(typeof input === 'string' ? input : input?.url || '')
      if (url.includes('/api/') && document.querySelector('.site-auth-callback')) {
        window.__apiWhileDeferred.push(url)
      }
      return realFetch(input, init)
    }
  })
  const calls = await fixtureRoute(page)

  await page.goto('/app?code=fixture-code&state=fixture-state')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 20_000 })
  expect(await page.evaluate(() => window.__apiWhileDeferred)).toEqual([])
  // The console did boot, and the deferral is a gate on ordering, not a block.
  expect(calls.some((call) => call.path === '/api/session')).toBe(true)
})
