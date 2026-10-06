import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin } from './stagingConfig.mjs'
import { captureStagingIdentity } from './stagingIdentity.mjs'

test.use({ storageState: { cookies: [], origins: [] } })

test('signed-out try restores a command-bar draft but never a credential-shaped paste', async ({ page, request }, testInfo) => {
  const identity = await captureStagingIdentity(request)
  await testInfo.attach('staging-identity', {
    body: JSON.stringify(identity, null, 2),
    contentType: 'application/json',
  })
  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  expect(new URL(page.url()).pathname).toBe('/try')
  expect(await page.evaluate(() => localStorage.getItem('leaf.jwt'))).toBeNull()
  const bar = page.locator('.tc-bar-input')
  await expect(bar).toBeVisible()
  await bar.fill('draw a 20 ft fence')
  await expect.poll(() => page.evaluate(() => Object.keys(localStorage)
    .filter((key) => key.startsWith('leaf.composerDraft.v1:'))
    .map((key) => localStorage.getItem(key)))).toContain('draw a 20 ft fence')
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(bar).toHaveValue('draw a 20 ft fence')
  await expect(page.getByRole('status').filter({ hasText: /^Draft restored$/ })).toBeVisible()

  const fakeKey = `AKIA${'A'.repeat(16)}`
  await bar.fill(fakeKey)
  await expect.poll(() => page.evaluate(() => Object.keys(localStorage)
    .filter((key) => key.startsWith('leaf.composerDraft.v1:')))).toEqual([])
  await page.reload({ waitUntil: 'networkidle' })
  await expect(bar).toHaveValue('')
  expect(await page.evaluate(() => Object.keys(localStorage)
    .filter((key) => key.startsWith('leaf.composerDraft.v1:'))
    .map((key) => localStorage.getItem(key)))).not.toContain(fakeKey)
})
