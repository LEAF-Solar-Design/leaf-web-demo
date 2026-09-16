import { expect, test } from '@playwright/test'
import { assertProdResponse, resolveProdBaseUrl } from './prodConfig.mjs'

const USER_AGENT = 'leaf-prod-readonly-smoke/1.0'
test.use({
  userAgent: USER_AGENT,
  extraHTTPHeaders: { 'User-Agent': USER_AGENT, Authorization: '' },
  storageState: { cookies: [], origins: [] },
})

test.beforeEach(() => {
  test.skip(!process.env.LEAF_E2E_PROD_BASE_URL, 'LEAF_E2E_PROD_BASE_URL is not set')
  resolveProdBaseUrl()
})

const prodUrl = (path) => new URL(path, resolveProdBaseUrl()).href

async function get(request, path, headers = {}) {
  const response = await request.get(prodUrl(path), {
    headers: { 'User-Agent': USER_AGENT, Authorization: '', ...headers },
  })
  assertProdResponse(response.url())
  return response
}

test('production-like unified route is reachable without mutation', async ({ page }) => {
  test.skip(!process.env.LEAF_E2E_PROD_BASE_URL, 'LEAF_E2E_PROD_BASE_URL is not set')
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
})

test('production web health reports its served source', async ({ request }) => {
  const response = await get(request, '/health.json')
  expect(response.ok()).toBeTruthy()
  const health = await response.json()
  expect(health.service).toBe('leaf-platform-web')
  expect(health.source_sha).toMatch(/^[a-f0-9]{40}$/i)
  test.info().annotations.push({ type: 'served-web-sha', description: health.source_sha })
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

test('production runtime flags include oneShell', async ({ request }) => {
  const response = await get(request, '/runtime-flags.js')
  expect(response.ok()).toBeTruthy()
  expect(await response.text()).toContain('oneShell')
})
