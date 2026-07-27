import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('details drawer owns focus, keeps the grid fixed, and restores its opener', async ({ page }) => {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('tab', { name: 'Trust' }).click()
  const opener = page.getByRole('button', { name: 'Account details' })
  const before = await page.locator('.tc-rail-r').boundingBox()
  await opener.click()

  const dialog = page.getByRole('dialog', { name: 'Account details' })
  await expect(dialog.getByRole('button', { name: 'Close details' })).toBeFocused()
  expect(await page.locator('.tc-rail-r').boundingBox()).toEqual(before)
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: 'Close details' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(opener).toBeFocused()
  expect(await page.locator('.tc-rail-r').boundingBox()).toEqual(before)
})
