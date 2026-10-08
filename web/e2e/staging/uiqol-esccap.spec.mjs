import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin } from './stagingConfig.mjs'
import { captureStagingIdentity } from './stagingIdentity.mjs'

test('the shortcut sheet uses an Esc cap named Close and closes on Escape', async ({ page, request }, testInfo) => {
  const identity = await captureStagingIdentity(request)
  await testInfo.attach('staging-identity', {
    body: JSON.stringify(identity, null, 2),
    contentType: 'application/json',
  })
  await page.goto('/app?demo=off', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  expect(new URL(page.url()).pathname).toBe('/app')
  const sheet = page.getByRole('dialog', { name: 'Keyboard shortcuts' })
  await expect(sheet).toHaveCount(0)
  // A click on empty chrome hands the caret back to the command bar, where '?' is typed text, not a
  // shortcut (probed on staging 2026-10-08), so judge the sheet's key with no text field focused.
  await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur() })
  await page.keyboard.press('Shift+?')
  await expect(sheet).toBeVisible()
  const close = sheet.getByRole('button', { name: 'Close', exact: true })
  await expect(close).toHaveText('Esc')
  await expect(close).toHaveClass('key hot')
  await expect(close).toHaveAttribute('type', 'button')
  await page.keyboard.press('Escape')
  await expect(sheet).toHaveCount(0)
})
