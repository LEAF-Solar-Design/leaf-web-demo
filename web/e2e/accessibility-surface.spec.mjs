import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('the unified scene exposes landmarks, named controls, status, and visible keyboard focus', async ({ page }) => {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await expect(page.getByRole('main', { name: 'Leaf operator workspace' })).toHaveCount(1)
  await expect(page.getByRole('complementary', { name: 'Workspace controls' })).toHaveCount(1)
  await expect(page.getByRole('complementary', { name: 'Operations controls' })).toHaveCount(1)
  await expect(page.getByRole('status', { name: 'Run status announcements' })).toHaveCount(1)
  for (const tablistName of ['Workspace panels', 'Operation panels']) {
    const tabs = page.getByRole('tablist', { name: tablistName }).getByRole('tab')
    for (let index = 0; index < await tabs.count(); index += 1) {
      const tab = tabs.nth(index)
      const controls = await tab.getAttribute('aria-controls')
      await expect(page.locator(`#${controls}`)).toHaveAttribute('role', 'tabpanel')
    }
  }
  await expect(page.locator('#workspace-tabpanel')).toHaveAttribute('aria-labelledby', 'workspace-tab-operator')
  await expect(page.locator('#operations-tabpanel')).toHaveAttribute('aria-labelledby', 'operations-tab-execution')

  const unnamedButtons = await page.locator('button:visible').evaluateAll((buttons) => buttons
    .filter((button) => !button.disabled)
    .filter((button) => !(button.getAttribute('aria-label') || button.textContent || '').trim())
    .length)
  expect(unnamedButtons).toBe(0)

  for (const locator of [
    page.getByRole('tab', { name: 'Operator' }),
    page.getByRole('tab', { name: 'Execution' }),
    page.getByLabel('Command bar'),
    page.getByRole('button', { name: 'Run', exact: true }),
    page.locator('#workspace-tabpanel'),
  ]) {
    await locator.focus()
    const visibleFocus = await locator.evaluate((element) => {
      const style = getComputedStyle(element)
      return style.outlineStyle !== 'none' || style.boxShadow !== 'none'
    })
    expect(visibleFocus).toBe(true)
  }
})
