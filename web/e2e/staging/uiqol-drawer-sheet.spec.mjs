import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin } from './stagingConfig.mjs'
import { captureStagingIdentity } from './stagingIdentity.mjs'

test.use({ viewport: { width: 390, height: 844 } })

test('Session details fills the phone bottom edge and closes on Escape', async ({ page, request }, testInfo) => {
  const identity = await captureStagingIdentity(request)
  await testInfo.attach('staging-identity', {
    body: JSON.stringify(identity, null, 2),
    contentType: 'application/json',
  })
  await page.goto('/app?demo=off', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  expect(new URL(page.url()).pathname).toBe('/app')
  await page.getByTitle(/^Session details/).click()
  const sheet = page.locator('.drawer-sheet[role="dialog"]')
  await expect(sheet).toBeVisible()
  await expect(sheet).toHaveAttribute('data-detent', 'medium')
  await expect(sheet).toHaveAttribute('aria-modal', 'false')
  await expect(sheet.getByRole('button', { name: 'Close details' })).toBeFocused()
  const geometry = await sheet.evaluate(element => {
    const rect = element.getBoundingClientRect()
    return { left: rect.left, bottom: rect.bottom, width: rect.width, viewportWidth: innerWidth, viewportHeight: innerHeight, transition: getComputedStyle(element).transitionProperty }
  })
  expect(geometry.left).toBe(0)
  expect(geometry.width).toBe(geometry.viewportWidth)
  expect(geometry.bottom).toBe(geometry.viewportHeight)
  expect(geometry.transition).toBe('opacity')
  const header = await sheet.locator('.drawer-head').boundingBox()
  await page.mouse.move(header.x + 20, header.y + 35)
  await page.mouse.down()
  await page.mouse.move(header.x + 20, header.y - 65)
  await page.mouse.up()
  await expect(sheet).toHaveAttribute('data-detent', 'large')
  await expect(sheet).toHaveAttribute('aria-modal', 'true')
  await page.keyboard.press('Escape')
  await expect(sheet).toHaveCount(0)
})
