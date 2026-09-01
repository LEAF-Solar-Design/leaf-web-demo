import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test('Escape dismisses a proposal before a second Escape leaves the unified scene', async ({ page }) => {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.getByRole('tab', { name: /Catalog/ }).click()
  await page.getByRole('button', { name: /count-panels/i }).click()
  await page.getByRole('button', { name: 'Review & run' }).click()
  await expect(page.getByRole('button', { name: 'Run count-panels' })).toBeVisible()

  await page.keyboard.press('Escape')
  await expect(page).toHaveURL(/\/try$/)
  await expect(page.getByRole('button', { name: 'Run count-panels' })).toHaveCount(0)
  await expect(page.locator('.route')).toHaveCount(0)
  const command = page.getByLabel('Command bar')
  await expect(command).toBeFocused()

  await command.evaluate((element) => element.blur())
  await page.keyboard.press('Escape')
  await expect(page).toHaveURL(/\/$/)
})

test('data-instant is stamped only when the global ladder handles a hotkey', async ({ page }) => {
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const result = catProofResponse({ method: request.method(), path: url.pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })

  const unhandledRetryStamped = await page.evaluate(() => {
    delete document.documentElement.dataset.instant
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'r' }))
    return document.documentElement.dataset.instant === '1'
  })
  expect(unhandledRetryStamped).toBe(false)

  const focusHotkeyStamped = await page.evaluate(() => {
    delete document.documentElement.dataset.instant
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }))
    return document.documentElement.dataset.instant === '1'
  })
  expect(focusHotkeyStamped).toBe(true)
})
