import { expect, test } from '@playwright/test'
import { REQUEST, catProofResponse, makeCatProofState } from './catProofFixture.mjs'

for (const action of ['Approve', 'Deny']) {
test(`a persistent header badge opens the pending approval inbox to ${action}`, async ({ page }) => {
  const proofState = makeCatProofState()
  const approved = action === 'Approve'
  const sessionCreates = []
  const pendingReads = []
  const decisions = []
  const resumes = []
  let resolved = false
  const pending = {
    confirmation_id: 'confirm-drape-1',
    session_id: null,
    tool: 'drape-onto-spheres',
    capability: 'drawing.write',
    params: { drawing_id: 'cat-panels', spheres: [{ radius: 10 }] },
    rationale: 'creates a new drawing version',
  }

  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const body = request.postData() ? request.postDataJSON() : {}
    if (request.method() === 'GET' && url.pathname === '/api/agent/approvals/pending') {
      const sessionId = url.searchParams.get('session_id')
      pendingReads.push(sessionId)
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ approvals: !resolved && sessionId === pending.session_id ? [pending] : [] }),
        headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
      })
      return
    }
    if (request.method() === 'POST' && url.pathname === `/api/agent/approvals/${pending.confirmation_id}`) {
      decisions.push(body)
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ resolved: true, approved: body.approved }),
        headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
      })
      return
    }
    if (request.method() === 'POST' && url.pathname === `/api/sessions/${pending.session_id}/messages` && body.confirm) {
      resumes.push({ sessionId: pending.session_id, body })
      resolved = true
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ turn_id: 'inbox-resolution', status: 'started' }),
        headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
      })
      return
    }
    const result = catProofResponse({
      method: request.method(), path: url.pathname, body,
      query: Object.fromEntries(url.searchParams),
    }, proofState)
    if (request.method() === 'POST' && url.pathname === '/api/sessions') {
      sessionCreates.push(result.body.session_id)
      pending.session_id = result.body.session_id
    }
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 10_000 })
  await expect(page.getByRole('button', { name: /^Pending approvals / })).toHaveCount(0)
  expect(sessionCreates).toEqual([])
  expect(pendingReads).toEqual([])

  await page.getByRole('navigation', { name: 'Workspace controls', exact: true })
    .getByRole('button', { name: 'Studio', exact: true }).click()
  await expect(page.locator('.workspace-environment')).toHaveAttribute('data-entered', 'true')
  await expect(page.getByRole('region', { name: 'Studio sidebar', exact: true })).toBeVisible()
  const command = page.getByLabel('Command bar', { exact: true })
  await command.fill(REQUEST)
  await command.press('Enter')
  await expect(page.locator('.converse-confirm').filter({ hasText: 'arrange-panels-as-cat' })).toBeVisible({ timeout: 15_000 })
  expect(sessionCreates).toEqual(['cat-session'])
  const badge = page.getByRole('button', { name: 'Pending approvals 1' })
  await expect(badge).toBeVisible({ timeout: 10_000 })
  await badge.click()

  const inbox = page.getByRole('region', { name: 'Pending approvals' })
  await expect(inbox).toContainText('drape-onto-spheres')
  await expect(inbox.getByRole('button', { name: 'Approve' })).toBeVisible()
  await expect(inbox.getByRole('button', { name: 'Deny' })).toBeVisible()
  expect(pendingReads.length).toBeGreaterThan(0)
  expect(pendingReads.every((sessionId) => sessionId === pending.session_id)).toBe(true)

  await inbox.getByRole('button', { name: action, exact: true }).click()
  await expect(inbox).toHaveCount(0)
  expect(decisions).toEqual([{ approved }])
  expect(resumes).toEqual([{
    sessionId: pending.session_id,
    body: { confirm: { confirmationId: pending.confirmation_id, approved } },
  }])
  await expect(page.getByRole('button', { name: 'Pending approvals 0', exact: true })).toBeVisible({ timeout: 10_000 })
})
}
