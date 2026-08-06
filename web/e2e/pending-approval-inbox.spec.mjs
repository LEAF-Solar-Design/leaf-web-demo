import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('a persistent header badge opens the pending approval inbox', async ({ page }) => {
  const proofState = makeCatProofState()
  const pending = {
    confirmation_id: 'confirm-drape-1',
    session_id: 'cat-session',
    tool: 'drape-onto-spheres',
    capability: 'drawing.write',
    params: { drawing_id: 'cat-panels', spheres: [{ radius: 10 }] },
    rationale: 'creates a new drawing version',
  }

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() === 'GET' && url.pathname === '/api/agent/approvals/pending') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ approvals: [pending] }),
        headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
      })
      return
    }
    const body = request.postData() ? request.postDataJSON() : {}
    const result = catProofResponse({
      method: request.method(), path: url.pathname, body,
      query: Object.fromEntries(url.searchParams),
    }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 10_000 })
  const badge = page.getByRole('button', { name: 'Pending approvals 1' })
  await expect(badge).toBeVisible({ timeout: 10_000 })
  await badge.click()

  const inbox = page.getByRole('region', { name: 'Pending approvals' })
  await expect(inbox).toContainText('drape-onto-spheres')
  await expect(inbox.getByRole('button', { name: 'Approve' })).toBeVisible()
  await expect(inbox.getByRole('button', { name: 'Deny' })).toBeVisible()
})
