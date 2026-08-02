import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('revises an exact custom tool without changing normal create', async ({ page }) => {
  const proofState = makeCatProofState()
  proofState.authorPublished = true
  proofState.independentApproved = true
  const stageBodies = []

  await page.addInitScript(() => localStorage.setItem('leaf.org_id', 'cat-proof-org'))
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    if (url.pathname === '/api/author/stage' && request.method() === 'POST') stageBodies.push(body)
    const result = catProofResponse({
      method: request.method(),
      path: url.pathname,
      body,
      query: Object.fromEntries(url.searchParams),
    }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: {
        'access-control-allow-origin': '*',
        'access-control-allow-headers': '*',
      },
    })
  })

  await page.goto('/app?proof=1')
  const customFamily = page.getByRole('button', { name: /Custom authored tools/ })
  await expect(customFamily).toBeVisible({ timeout: 15_000 })
  await customFamily.click()
  const toolCard = page.locator('.tool-card').filter({ hasText: 'count-panels-near-edge' })
  await toolCard.locator('.tool-head').click()
  await toolCard.getByRole('button', { name: 'Revise' }).click()

  await expect(page.getByLabel('Tool to revise')).toHaveValue('count-panels-near-edge')
  await expect(page.getByLabel('Tool to revise')).toHaveAttribute('readonly', '')
  await expect(page.getByLabel('What should the tool do?')).toBeFocused()
  await page.getByLabel('What should the tool do?').fill('repair the geometry so every polyline has area')
  await page.getByRole('button', { name: 'Generate revision' }).click()
  await expect(page.locator('.authored')).toBeVisible()
  await page.getByRole('button', { name: 'Request publication' }).click()
  await expect(page.getByRole('button', { name: 'Run it now' })).toBeVisible()

  expect(stageBodies).toHaveLength(1)
  expect(stageBodies[0]).toMatchObject({
    description: 'repair the geometry so every polyline has area',
    mode: 'build',
    target_tool_name: 'count-panels-near-edge',
  })
  await expect(page.locator('.tool-card').filter({ hasText: 'count-panels-near-edge' })).toHaveCount(1)

  await page.getByRole('button', { name: 'Cancel revision' }).click()
  await expect(page.getByLabel('Tool to revise')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Generate tool' })).toBeVisible()
  await expect(page.getByLabel('What should the tool do?')).toHaveValue('')

  await page.getByLabel('What should the tool do?').fill('make a new read-only inspection tool')
  await page.getByRole('button', { name: 'Generate tool' }).click()
  await expect.poll(() => stageBodies.length).toBe(2)
  expect(stageBodies[1]).toMatchObject({
    description: 'make a new read-only inspection tool',
    mode: 'build',
  })
  expect(stageBodies[1]).not.toHaveProperty('target_tool_name')
  await expect(page.locator('.tool-card').filter({ hasText: 'count-panels-near-edge' })).toHaveCount(1)
})
