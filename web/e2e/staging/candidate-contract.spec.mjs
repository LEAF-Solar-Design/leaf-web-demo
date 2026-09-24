import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin, assertResponseOnAllowedOrigin } from './stagingConfig.mjs'
import { resolveExpectedSha } from '../prod/prodConfig.mjs'

// The staging twin of the production candidate checks in
// e2e/prod/unified-prod-readonly.spec.mjs: the staged app and web must both
// serve the candidate being promoted, and the app must render the studio shell
// (one app scene holding one Drawing region). Read-only, no Authorization
// header, no receipt. The host allowlist is enforced by globalSetup and by
// assertResponseOnAllowedOrigin on every response.

test('staging app and web serve the expected candidate', async ({ request }) => {
  const expected = resolveExpectedSha()
  test.skip(!expected, 'LEAF_E2E_EXPECTED_SHA is not set')
  const app = await request.get('/api/health', { timeout: 10_000 })
  assertResponseOnAllowedOrigin(app)
  expect(app.ok()).toBeTruthy()
  const appHealth = await app.json()
  const web = await request.get('/health.json', { timeout: 10_000 })
  assertResponseOnAllowedOrigin(web)
  expect(web.ok()).toBeTruthy()
  const webHealth = await web.json()
  test.info().annotations.push({ type: 'expected-sha', description: expected })
  expect(appHealth.source_sha).toBe(expected)
  expect(webHealth.source_sha).toBe(expected)
})

test('staging app renders the studio shell', async ({ page }) => {
  test.setTimeout(60000)
  const response = await page.goto('/app?demo=1')
  assertResponseOnAllowedOrigin(response)
  assertPageOnAllowedOrigin(page)
  const shell = page.locator('.studio-shell[data-scene="app"]')
  await expect(shell).toHaveCount(1, { timeout: 30000 })
  // Attached, not visible: the region carries aria-hidden until the ground node attaches.
  const drawing = shell.locator('[role="region"][aria-label="Drawing"]')
  await expect(drawing).toHaveCount(1)
  await expect(drawing).toBeAttached()
  assertPageOnAllowedOrigin(page)
})
