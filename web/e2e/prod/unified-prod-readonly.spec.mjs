import { expect, test } from '@playwright/test'

test('production-like unified route is reachable without mutation', async ({ page }) => {
  test.skip(!process.env.LEAF_E2E_PROD_BASE_URL, 'LEAF_E2E_PROD_BASE_URL is not set')
  await page.goto('/try')
  await expect(page).toHaveURL(/\/try(?:\?|$)/)
  await expect(page.locator('body')).not.toContainText('Internal Server Error')
})
