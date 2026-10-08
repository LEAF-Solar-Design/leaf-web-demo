import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin } from './stagingConfig.mjs'
import { captureStagingIdentity } from './stagingIdentity.mjs'

test('find scope searches the public demo drawing object index', async ({ page, request }, testInfo) => {
  const identity = await captureStagingIdentity(request)
  await testInfo.attach('staging-identity', { body: JSON.stringify(identity, null, 2), contentType: 'application/json' })
  await page.goto('/try', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  expect(new URL(page.url()).pathname).toBe('/try')
  await page.locator('.bar-scope').click()
  await page.getByRole('option').filter({ hasText: /^find/ }).click()
  await page.getByRole('combobox', { name: 'Command bar' }).fill('layer:')
  const results = page.getByRole('listbox', { name: 'Search results' })
  await expect(results.getByText('Drawing objects', { exact: true })).toBeVisible({ timeout: 30_000 })
  await expect(results.getByRole('option').filter({ hasText: 'drawing-object' }).first()).toBeVisible()
  await expect(page.getByTestId('build-egress')).toHaveCount(0)
})
