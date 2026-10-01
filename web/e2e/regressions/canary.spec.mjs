import { test, expect } from './_harness.mjs'

test('real stack serves the Studio shell @canary', async ({ page, stack }) => {
  const response = await page.goto('/app')
  expect(response.ok()).toBe(true)
  expect(new URL(page.url()).origin).toBe(stack.baseURL)
  await expect(page.locator('.studio-shell[data-scene="app"][data-mode="console"]')).toBeVisible()
})
