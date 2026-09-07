import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'
import { setRail } from './railFlag.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const REGISTRY = '/api/tenant/mcp-servers'
const HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const local = (value) => ['localhost', '127.0.0.1', '[::1]'].includes(new URL(value).hostname)

test.use({ serviceWorkers: 'block' })

test('link service through local OAuth, show connected, then unlink', async ({ page, context, request }) => {
  // This row must never quietly skip because its fake AS was not armed.
  expect(local(API_BASE)).toBe(true)
  expect(process.env.LEAF_E2E_MANAGED, 'use the managed local stack').toBe('1')
  const escaped = []
  await context.route('**/*', async (route) => {
    if (!local(route.request().url())) {
      escaped.push(route.request().url())
      await route.abort('blockedbyclient')
      return
    }
    await route.continue()
  })
  await requireLocalReady(request, test, API_BASE)
  const metadata = await request.get(`${API_BASE}${REGISTRY}/_fake-oauth/.well-known/oauth-protected-resource`)
  expect(metadata.status(), 'boot with TENANT_MCP_FAKE_OAUTH=1').toBe(200)
  const entitlement = await request.get(`${API_BASE}/api/entitlements`, { headers: HEADERS })
  expect((await entitlement.json()).entitlements.link_service, 'managed policy must grant link_service').toBe(true)

  // Isolate only this row's label when reusing the managed stack. Never
  // delete somebody else's services or call their revocation endpoints.
  const label = 'Local link flow fixture'
  const initial = await request.get(`${API_BASE}${REGISTRY}`, { headers: HEADERS })
  expect(initial.status()).toBe(200)
  for (const server of (await initial.json()).servers.filter((item) => item.label === label)) {
    expect((await request.delete(`${API_BASE}${REGISTRY}/${server.id}`, { headers: HEADERS })).status()).toBe(200)
  }
  await setRail(page, '1')
  await page.goto('/app')
  await page.locator('.link-svc-trigger').click()
  const drawer = page.getByRole('dialog', { name: 'Linked services', exact: true })
  await drawer.getByLabel('Service label').fill(label)
  await drawer.getByLabel('Service URL').fill(`${API_BASE}${REGISTRY}/_fake-oauth`)
  await drawer.getByRole('button', { name: 'Link service', exact: true }).click()
  const row = drawer.locator('.ca-account').filter({ hasText: label })
  await expect(row.locator('.state')).toHaveText('Registered')

  // noopener removes the opener relationship in Chromium. Arm both before
  // clicking so the context page event is a race-free fallback for popup.
  const popupEvent = page.waitForEvent('popup', { timeout: 15_000 }).catch(() => null)
  const contextEvent = context.waitForEvent('page', { timeout: 15_000 })
  await row.getByRole('button', { name: 'Connect', exact: true }).click()
  const popup = await Promise.race([popupEvent.then((value) => value || contextEvent), contextEvent])
  await popup.waitForURL((url) => url.pathname === `${REGISTRY}/callback`)
  await expect(popup.locator('body')).toContainText(/"linked"\s*:\s*true/)
  expect(local(popup.url())).toBe(true)
  await popup.close()

  // The drawer refreshes on mount, not via a fabricated callback message.
  // Reload follows the real list path after the server-side callback.
  await page.reload()
  await page.locator('.link-svc-trigger').click()
  await expect(row.locator('.state')).toHaveText('Connected')
  await row.locator('.chip-danger').click()
  await row.locator('.chip-danger-confirm').click()
  await expect(row).toHaveCount(0)
  const final = await request.get(`${API_BASE}${REGISTRY}`, { headers: HEADERS })
  expect(final.status()).toBe(200)
  expect((await final.json()).servers.some((server) => server.label === label)).toBe(false)
  expect(escaped, 'browser requests must remain on loopback').toEqual([])
})
