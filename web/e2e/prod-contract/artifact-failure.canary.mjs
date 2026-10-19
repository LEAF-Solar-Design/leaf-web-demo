import { expect, test } from '../prod/prodFixtures.mjs'

test('authenticated artifact failure canary', async ({ page, prodConfig, browser }, testInfo) => {
  // No navigation and no resources. If any request occurs the shared offline
  // adapter fulfills or aborts it locally, including popup and frame traffic.
  await page.setContent(`<html><body><h1>${prodConfig.token}</h1></body></html>`)
  // Exercise the same matcher and returned-handle paths as the real proofs.
  await expect(page.getByRole('heading')).toHaveCount(1)
  await expect(page.getByRole('heading')).toBeVisible()
  await expect(page.locator('body')).toContainText(prodConfig.token)
  await expect(page).toHaveURL('about:blank')
  await expect(page).toHaveURL(/^about:blank$/)
  expect(page.mainFrame().page()).toBe(page)
  expect(page.getByRole('heading').page()).toBe(page)
  console.log(prodConfig.token)
  console.error(prodConfig.token)
  try { browser.newContext() } catch {}
  try { await testInfo.attach('raw-secret', { body: Buffer.from(prodConfig.token), contentType: 'text/plain' }) } catch {}
  throw new Error(prodConfig.token)
})
