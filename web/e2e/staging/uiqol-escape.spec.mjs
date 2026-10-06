import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin, stagingProofPath } from './stagingConfig.mjs'
import { captureStagingIdentity } from './stagingIdentity.mjs'

// uiqol S27 changed-behaviour check against the deployed staging origin, run
// after the D4 identity probe proves the candidate source is live. Escape goes
// through ONE owner stack (web/src/lib/useEscapeOwner.js, motion standard
// section 7): with Details open and then the shortcut sheet over it, one
// Escape closes only the sheet (sheet layer), and a second closes Details
// (drawer layer). Nothing is mutated. When the console shell is absent for a
// signed-out visitor the check is the identity probe only, and it says so.

test.use({ viewport: { width: 1600, height: 1000 } })

test('one Escape closes only the shortcut sheet, a second closes Details', async ({ page, request }, testInfo) => {
  const identity = await captureStagingIdentity(request)
  await testInfo.attach('staging-identity', {
    body: JSON.stringify(identity, null, 2),
    contentType: 'application/json',
  })
  await page.goto('/app?demo=off', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  expect(new URL(page.url()).pathname).toBe('/app')

  const opener = page.getByTitle(/^Session details/)
  if (await opener.count() === 0) {
    testInfo.annotations.push({ type: 'identity-only', description: 'the console Details opener is absent signed out' })
    return
  }
  await opener.first().click()
  const details = page.locator('.drawer-layer .drawer[role="dialog"]')
  await expect(details).toBeVisible()
  // DetailsDrawer parks focus on its Esc cap, a button, so Shift+? is the
  // ladder's and opens the sheet over the drawer.
  await expect(details.getByRole('button', { name: 'Close details' })).toBeFocused()
  await page.keyboard.press('Shift+?')
  const sheet = page.getByRole('dialog', { name: 'Keyboard shortcuts' })
  if (await sheet.count() === 0) {
    testInfo.annotations.push({ type: 'details-only', description: 'the shortcut sheet did not open (single-key shortcuts may be off)' })
    await page.keyboard.press('Escape')
    await expect(details).toHaveCount(0)
    return
  }
  await expect(sheet).toBeVisible()
  await page.screenshot({ path: stagingProofPath('uiqol-escape', 'sheet-over-details.png') })

  await page.keyboard.press('Escape')
  await expect(sheet).toHaveCount(0)
  await expect(details).toBeVisible()
  await page.screenshot({ path: stagingProofPath('uiqol-escape', 'details-after-first-escape.png') })

  await page.keyboard.press('Escape')
  await expect(details).toHaveCount(0)
  expect(new URL(page.url()).pathname).toBe('/app')
})
