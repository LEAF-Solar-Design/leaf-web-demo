import { expect, test } from '@playwright/test'
import { REQUEST, catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('shared conversation controller reattaches once after a stale session', async ({ page }) => {
  const proofState = makeCatProofState()
  let sessionCreates = 0
  let messagePosts = 0

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const body = request.postData() ? request.postDataJSON() : {}
    if (request.method() === 'POST' && url.pathname === '/api/sessions') sessionCreates += 1
    if (request.method() === 'POST' && /\/api\/sessions\/[^/]+\/messages$/.test(url.pathname)) {
      messagePosts += 1
      if (messagePosts === 1) {
        await route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ error: { error_code: 'session_not_found', message: 'stale session' } }),
          headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
        })
        return
      }
    }
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 10_000 })
  await page.getByLabel('Command bar').fill(REQUEST)
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.locator('.converse-confirm')).toContainText('arrange-panels-as-cat', { timeout: 15_000 })
  expect({ sessionCreates, messagePosts }).toEqual({ sessionCreates: 2, messagePosts: 2 })
})
