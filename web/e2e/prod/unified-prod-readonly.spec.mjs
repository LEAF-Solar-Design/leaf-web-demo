import { expect, test } from './prodFixtures.mjs'
import {
  assertProdResponse,
  resolveExpectedSha,
  resolveProdBaseUrl,
} from './prodConfig.mjs'

const USER_AGENT = 'leaf-prod-readonly-smoke/1.0'
const prodUrl = (path) => new URL(path, resolveProdBaseUrl()).href

async function get(request, path, headers = {}) {
  const response = await request.get(prodUrl(path), {
    headers: { 'User-Agent': USER_AGENT, Authorization: '', ...headers },
  })
  assertProdResponse(response.url())
  return response
}

test('production-like unified route is reachable without mutation', async ({ page }) => {
  const response = await page.goto(prodUrl('/try'))
  assertProdResponse(response.url())
  assertProdResponse(page.url())
  await expect(page).toHaveURL(/\/try(?:\?|$)/)
  await expect(page.locator('body')).not.toContainText('Internal Server Error')
})

test('production app health reports its served source', async ({ request }) => {
  const response = await get(request, '/api/health')
  expect(response.ok()).toBeTruthy()
  const health = await response.json()
  expect(health.ok).toBe(true)
  expect(health.source_sha).toMatch(/^[a-f0-9]{40}$/i)
  test.info().annotations.push({ type: 'served-app-sha', description: health.source_sha })
  const expected = resolveExpectedSha()
  if (expected) expect(health.source_sha).toBe(expected)
})

test('production web health reports its served source', async ({ request }) => {
  const response = await get(request, '/health.json')
  expect(response.ok()).toBeTruthy()
  const health = await response.json()
  expect(health.service).toBe('leaf-platform-web')
  expect(health.source_sha).toMatch(/^[a-f0-9]{40}$/i)
  test.info().annotations.push({ type: 'served-web-sha', description: health.source_sha })
  const expected = resolveExpectedSha()
  if (expected) expect(health.source_sha).toBe(expected)
})

test('production deployment identity answers without a bearer', async ({ request }) => {
  const response = await get(request, '/api/deployment-identity')
  test.info().annotations.push({ type: 'deployment-identity-status', description: String(response.status()) })
  expect([401, 200]).toContain(response.status())
})

test('production auth ladder rejects absent and invalid bearers', async ({ request }) => {
  const absent = await get(request, '/api/capabilities')
  expect(absent.status()).toBe(401)
  const invalid = await get(request, '/api/capabilities', { Authorization: 'Bearer invalid' })
  expect([401, 403]).toContain(invalid.status())
})

test('production app displays its served build stamp', async ({ page }) => {
  test.setTimeout(60000)
  const response = await page.goto(prodUrl('/app?demo=1'))
  assertProdResponse(response.url())
  assertProdResponse(page.url())
  await expect(page.locator('text=/build \\d{4}-\\d{2}-\\d{2}/')).toBeVisible({ timeout: 30000 })
  assertProdResponse(page.url())
})

test('production app renders the studio shell', async ({ page }) => {
  test.setTimeout(60000)
  const response = await page.goto(prodUrl('/app?demo=1'))
  assertProdResponse(response.url())
  assertProdResponse(page.url())
  const shell = page.locator('.studio-shell[data-scene="app"]')
  await expect(shell).toHaveCount(1, { timeout: 30000 })
  // Attached, not visible: the region carries aria-hidden until the ground node attaches.
  const drawing = shell.locator('[role="region"][aria-label="Drawing"]')
  await expect(drawing).toHaveCount(1)
  await expect(drawing).toBeAttached()
  assertProdResponse(page.url())
})

test('production app and web serve the expected candidate', async ({ request }) => {
  const expected = resolveExpectedSha()
  expect(expected).toMatch(/^[0-9a-f]{40}$/)
  const app = await get(request, '/api/health')
  expect(app.ok()).toBeTruthy()
  const appHealth = await app.json()
  const web = await get(request, '/health.json')
  expect(web.ok()).toBeTruthy()
  const webHealth = await web.json()
  test.info().annotations.push({ type: 'expected-sha', description: expected })
  expect(appHealth.source_sha).toBe(expected)
  expect(webHealth.source_sha).toBe(expected)
})
